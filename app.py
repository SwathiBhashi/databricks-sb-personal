import os
import pandas as pd
from flask import Flask, render_template, request, redirect, url_for, flash
from databricks import sql
# IMPORT THE OFFICIAL SDK AUTHENTICATION LAYER
from databricks.sdk.core import Config

# Get the exact absolute directory path of the active script
base_dir = os.path.abspath(os.path.dirname(__file__))

app = Flask(
    __name__,
    template_folder=os.path.join(base_dir, 'templates'),
    static_folder=os.path.join(base_dir, 'static')
)
app.secret_key = os.urandom(24)

# Automatically pulls workspace variables (HOST, CLIENT_ID, CLIENT_SECRET) from Databricks Apps runtime environment
cfg = Config()

# 1. Databricks SQL Helper Functions (OFFICIAL DATABRICKS APPS AUTH PATTERN)
def get_db_connection():
    """Establishes a connection to the SQL Warehouse using the App's native Service Principal context."""
    # Pull the HTTP path from your environment variables or hardcode your SQL Warehouse HTTP path string here
    http_path = os.environ.get("DATABRICKS_SQL_HTTP_PATH", "/sql/1.0/warehouses/your-warehouse-id-here")
    
    # Strip any accidental 'https://' prefix if appended by the system environment wrapper
    server_host = cfg.host
    if server_host and server_host.startswith("https://"):
        server_host = server_host.replace("https://", "", 1)

    return sql.connect(
        server_hostname=server_host,
        http_path=http_path,
        # This securely signs the connection request using the background service identity credentials
        credentials_provider=lambda: cfg.authenticate
    )

def query_as_dataframe(query, params=None):
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(query, params or ())
            if cursor.description is None:
                return pd.DataFrame()
            columns = [desc[0] for desc in cursor.description]
            data = cursor.fetchall()
            return pd.DataFrame(data, columns=columns)


# 2. Capacity Business Logic Engine
def fetch_capacity_metrics(department_filter=None):
    """
    Calculates active, under, over, and optimal resource metrics 
    by checking total active project allocations against their custom Employee Capex target.
    """
    where_clause = ""
    params = []
    if department_filter:
        where_clause = "WHERE e.DEPARTMENT = %s"
        params.append(department_filter)

    query = f"""
        WITH date_windows AS (
            SELECT 
                current_date() as cur_wk_start, date_add(current_date(), 7) as cur_wk_end,
                date_add(current_date(), 7) as nxt_wk_start, date_add(current_date(), 14) as nxt_wk_end,
                date_add(current_date(), 30) as nxt_mn_start, date_add(current_date(), 60) as nxt_mn_end
        ),
        emp_allocations AS (
            SELECT 
                e.employee_id,
                e.DEPARTMENT,
                COALESCE(e.CAPEX, 100.00) as capex_target,
                
                -- Current Week Allocations
                SUM(CASE WHEN a.is_active = true AND a.start_date <= dw.cur_wk_end AND (a.end_date IS NULL OR a.end_date >= dw.cur_wk_start) 
                         THEN a.allocation_percentage ELSE 0 END) as cur_wk_alloc,
                         
                -- Next Week Allocations
                SUM(CASE WHEN a.is_active = true AND a.start_date <= dw.nxt_wk_end AND (a.end_date IS NULL OR a.end_date >= dw.nxt_wk_start) 
                         THEN a.allocation_percentage ELSE 0 END) as nxt_wk_alloc,
                         
                -- Next Month Allocations
                SUM(CASE WHEN a.is_active = true AND a.start_date <= dw.nxt_mn_end AND (a.end_date IS NULL OR a.end_date >= dw.nxt_mn_start) 
                         THEN a.allocation_percentage ELSE 0 END) as nxt_mn_alloc
            FROM EMPLOYEE e
            CROSS JOIN date_windows dw
            LEFT JOIN allocation a ON e.employee_id = a.employee_id
            {where_clause}
            GROUP BY e.employee_id, e.DEPARTMENT, e.CAPEX, dw.cur_wk_start, dw.cur_wk_end, dw.nxt_wk_start, dw.nxt_wk_end, dw.nxt_mn_start, dw.nxt_mn_end
        )
        SELECT * FROM emp_allocations
    """
    df = query_as_dataframe(query, params)
    if df.empty:
        return {p: {'active': 0, 'under': 0, 'over': 0, 'optimal': 0} for p in ['current_week', 'next_week', 'next_month']}

    periods = ['cur_wk_alloc', 'nxt_wk_alloc', 'nxt_mn_alloc']
    period_keys = ['current_week', 'next_week', 'next_month']
    metrics = {}

    for col, key in zip(periods, period_keys):
        metrics[key] = {
            'active': int((df[col] > 0).sum()),
            'under': int((df[col] < df['capex_target']).sum()),
            'over': int((df[col] > df['capex_target']).sum()),
            'optimal': int((df[col] == df['capex_target']).sum())
        }
    return metrics

# 3. HTTP Routes

@app.route('/')
def index():
    return redirect(url_for('portfolio_summary'))

@app.route('/summary')
def portfolio_summary():
    selected_dept = request.args.get('department', '')
    
    depts_df = query_as_dataframe("SELECT DISTINCT DEPARTMENT FROM EMPLOYEE WHERE DEPARTMENT IS NOT NULL ORDER BY DEPARTMENT")
    departments = depts_df['DEPARTMENT'].tolist() if not depts_df.empty else []
    
    metrics = fetch_capacity_metrics(selected_dept if selected_dept else None)
    return render_template('summary.html', metrics=metrics, departments=departments, selected_dept=selected_dept)

@app.route('/manage', methods=['GET', 'POST'])
def manage_allocations():
    if request.method == 'POST':
        emp_id = request.form.get('employee_id')
        project_id = int(request.form.get('project_id'))
        new_alloc = float(request.form.get('allocation_percentage'))
        start_date = request.form.get('start_date')
        end_date = request.form.get('end_date') or None

        # --- VALIDATION 1: Project count restriction (< 5) ---
        proj_query = "SELECT project_id FROM allocation WHERE employee_id = %s AND is_active = true"
        existing_projs = query_as_dataframe(proj_query, (emp_id,))
        unique_projects = set(existing_projs['project_id'].tolist() if not existing_projs.empty else [])
        unique_projects.add(project_id)
        
        if len(unique_projects) > 5:
            flash("❌ Validation Failed: A resource cannot be allocated to more than 5 projects simultaneously.", "danger")
            return redirect(url_for('manage_allocations'))

        # --- VALIDATION 2: Check limit with dynamic employee profile target ---
        emp_target_df = query_as_dataframe("SELECT COALESCE(CAPEX, 100) as target_limit FROM EMPLOYEE WHERE employee_id = %s", (emp_id,))
        # FIX: Added explicitly scoped element selector index [0]
        capex_target = float(emp_target_df['target_limit'].iloc[0] if not emp_target_df.empty else 100.00)

        alloc_query = "SELECT SUM(allocation_percentage) as total FROM allocation WHERE employee_id = %s AND is_active = true"
        total_alloc_df = query_as_dataframe(alloc_query, (emp_id,))
        # FIX: Added explicitly scoped element selector index [0]
        current_total = float(total_alloc_df['total'].fillna(0).iloc[0] if not total_alloc_df.empty else 0)
        
        existing_match = query_as_dataframe("SELECT allocation_percentage FROM allocation WHERE employee_id=%s AND project_id=%s AND is_active=true", (emp_id, project_id))
        old_alloc = float(existing_match['allocation_percentage'].iloc[0] if not existing_match.empty else 0)
        
        projected_total = current_total - old_alloc + new_alloc

        if projected_total > capex_target:
            flash(f"❌ Validation Failed: Resource would become over-utilised ({projected_total}% exceeds this employee's custom Capex target of {capex_target}%).", "danger")
            return redirect(url_for('manage_allocations'))

        # --- WRITE BACK TO LAKEBASE ---
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                if old_alloc > 0:
                    cursor.execute("""
                        UPDATE allocation 
                        SET allocation_percentage = %s, start_date = %s, end_date = %s, updated_at = current_timestamp()
                        WHERE employee_id = %s AND project_id = %s AND is_active = true
                    """, (new_alloc, start_date, end_date, emp_id, project_id))
                else:
                    cursor.execute("""
                        INSERT INTO allocation (employee_id, project_id, allocation_percentage, start_date, end_date, effective_from_date, is_active)
                        VALUES (%s, %s, %s, %s, %s, current_date(), true)
                    """, (new_alloc, start_date, end_date, emp_id, project_id))
        
        flash("💪 Allocation successfully saved!", "success")
        return redirect(url_for('manage_allocations'))

    # GET workflow metrics display logic
    f_emp = request.args.get('employee_name', '')
    f_mgr = request.args.get('line_manager', '')
    f_dept = request.args.get('department', '')

    projects = query_as_dataframe("SELECT project_id, project_name FROM project WHERE is_active=true ORDER BY project_name").to_dict(orient='records')
    employees_lookup = query_as_dataframe("SELECT employee_id, first_name, last_name FROM EMPLOYEE ORDER BY last_name").to_dict(orient='records')

    alloc_master_query = """
        SELECT 
            a.allocation_id, e.employee_id, concat(e.first_name, ' ', e.last_name) as emp_name,
            e.LINE_MANAGER_NAME, e.DEPARTMENT, p.project_name, p.project_id,
            a.allocation_percentage, a.start_date, a.end_date
        FROM allocation a
        JOIN EMPLOYEE e ON a.employee_id = e.employee_id
        JOIN project p ON a.project_id = p.project_id
        WHERE a.is_active = true
    """
    
    conditions = []
    params = []
    if f_emp:
        conditions.append("concat(e.first_name, ' ', e.last_name) ILIKE %s")
        params.append(f"%{f_emp}%")
    if f_mgr:
        conditions.append("e.LINE_MANAGER_NAME ILIKE %s")
        params.append(f"%{f_mgr}%")
    if f_dept:
        conditions.append("e.DEPARTMENT ILIKE %s")
        params.append(f"%{f_dept}%")
        
    if conditions:
        alloc_master_query += " AND " + " AND ".join(conditions)
    
    alloc_master_query += " ORDER BY emp_name"
    allocations = query_as_dataframe(alloc_master_query, params).to_dict(orient='records')

    return render_template('manage.html', allocations=allocations, projects=projects, 
                           employees=employees_lookup, f_emp=f_emp, f_mgr=f_mgr, f_dept=f_dept)

if __name__ == '__main__':
    # Safely extract the dynamic system integer provided by Databricks
    port_str = os.environ.get("DATABRICKS_APP_PORT")
    
    if port_str and port_str.isdigit():
        port = int(port_str)
    else:
        port = 8000 # Local testing fallback
        
    print(f"🚀 Starting Capacity Planner App on target port: {port}")
    app.run(host='0.0.0.0', port=port, debug=False)
