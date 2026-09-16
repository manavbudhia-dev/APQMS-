from utils.http_utils import esc
from utils.http_utils import fmt_rupee
from utils.http_utils import redirect
from database import get_db
from datetime import datetime
import calendar


def _month_days(month):
    """Number of calendar days in a 'YYYY-MM' month string."""
    year, mo = (int(p) for p in month.split('-'))
    return calendar.monthrange(year, mo)[1]


def _employee_active_range(emp, month):
    """Return (first_day, last_day) as 1-based day-of-month ints marking
    the days within `month` (a 'YYYY-MM' string) that this employee was
    actually employed, based on their joining/leaving dates. Returns
    None if they weren't employed at all during that month.

    An employee with no joining_date/leaving_date set (e.g. records
    created before this feature existed) is treated as having no
    bounds, so nothing changes for them."""
    days_in_month = _month_days(month)
    join = emp['joining_date'] if 'joining_date' in emp.keys() else None
    leave = emp['leaving_date'] if 'leaving_date' in emp.keys() else None
    join_month = join[:7] if join else None
    leave_month = leave[:7] if leave else None
    if join_month and join_month > month:
        return None
    if leave_month and leave_month < month:
        return None
    first_day = int(join[8:10]) if join_month == month else 1
    last_day = int(leave[8:10]) if leave_month == month else days_in_month
    if first_day > last_day:
        return None
    return first_day, last_day



ATT_STYLE = """
.att-breadcrumb { font-size: 13px; color: #64748B; margin-bottom: 10px; }
.att-breadcrumb a { color: #64748B; text-decoration: none; }
.att-breadcrumb a:hover { color: var(--primary); }
.att-breadcrumb .sep { margin: 0 6px; color: #CBD5E1; }
.att-breadcrumb .current { color: #0F172A; font-weight: 600; }

.att-page-head { display: flex; justify-content: space-between; align-items: flex-start; gap: 16px; margin-bottom: 20px; flex-wrap: wrap; }
.att-page-head h1 { font-size: 24px; font-weight: 700; color: #0F172A; margin: 0; }
.att-page-head p { font-size: 13px; color: #64748B; margin: 4px 0 0; }
.att-page-head-actions { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
.att-month-input { position: relative; display: flex; align-items: center; }
.att-month-input input[type=month] { padding-left: 36px; border-radius: 10px; border: 1px solid #E2E8F0; font-weight: 600; color: #334155; background: #fff; min-width: 170px; }
.att-month-input i { position: absolute; left: 12px; color: #64748B; font-size: 14px; pointer-events: none; z-index: 2; }

.att-summary-card {
  background: #fff; border: 1px solid #E2E8F0; border-radius: var(--radius);
  padding: 20px 24px; margin-bottom: 20px; display: flex;
  justify-content: space-between; align-items: center; gap: 20px; flex-wrap: wrap;
}
.att-emp-info { display: flex; gap: 14px; align-items: center; }
.att-emp-avatar {
  width: 54px; height: 54px; border-radius: 50%; background: #EFF6FF; color: #2563EB;
  display: flex; align-items: center; justify-content: center; font-size: 22px; font-weight: 700; flex-shrink: 0;
}
.att-emp-info h4 { font-size: 17px; font-weight: 700; color: #0F172A; margin: 0; }
.att-emp-info .att-emp-meta { font-size: 13px; color: #64748B; margin-top: 2px; }
.att-mini-stats { display: flex; gap: 26px; flex-wrap: wrap; }
.att-mini-stat { display: flex; align-items: center; gap: 10px; }
.att-mini-stat-icon {
  width: 38px; height: 38px; border-radius: 10px; display: flex; align-items: center;
  justify-content: center; font-size: 16px; flex-shrink: 0;
}
.att-mini-stat-value { font-size: 18px; font-weight: 700; color: #0F172A; line-height: 1.1; }
.att-mini-stat-label { font-size: 12px; color: #64748B; white-space: nowrap; }

.att-card-header { display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 10px; }
.att-card-header h5 { margin: 0; font-size: 15.5px; font-weight: 700; color: #0F172A; display: flex; align-items: center; gap: 8px; }
.att-card-header h5 i { color: var(--primary); }

.att-day-tables { border-top: 1px solid #E2E8F0; }
.att-day-table { width: 100%; border-collapse: collapse; }
.att-day-table th {
  background: #F8FAFC; font-size: 11px; font-weight: 600; color: #64748B; text-transform: uppercase;
  letter-spacing: 0.4px; padding: 10px 12px; text-align: left; border-bottom: 1px solid #E2E8F0; white-space: nowrap;
}
.att-day-table td { padding: 7px 12px; border-bottom: 1px solid #F1F5F9; font-size: 13.5px; vertical-align: middle; }
.att-day-table .att-day-num { font-weight: 700; color: #0F172A; width: 34px; }
.att-day-table .att-day-date { color: #475569; white-space: nowrap; }
.att-day-table tr.att-day-off { background: #FEF2F2; }

.att-switch { position: relative; display: inline-block; width: 40px; height: 22px; }
.att-switch input { opacity: 0; width: 0; height: 0; position: absolute; }
.att-switch .slider {
  position: absolute; inset: 0; background: #E2E8F0; border-radius: 999px; cursor: pointer; transition: 0.2s;
}
.att-switch .slider::before {
  content: ""; position: absolute; width: 16px; height: 16px; left: 3px; top: 3px;
  background: #fff; border-radius: 50%; transition: 0.2s; box-shadow: 0 1px 2px rgba(0,0,0,0.2);
}
.att-switch input:checked + .slider { background: var(--primary); }
.att-switch input:checked + .slider::before { transform: translateX(18px); }
.att-switch input:disabled + .slider { opacity: 0.5; cursor: not-allowed; }

.att-co-input { width: 100%; min-width: 112px; padding: 6px 10px; font-size: 13px; border-radius: 8px; border: 1px solid #E2E8F0; }
.att-co-input:disabled { background: #F8FAFC; color: #CBD5E1; }
.att-ot-cell { display: flex; align-items: center; justify-content: center; gap: 6px; }
.att-ot-input { width: 56px; padding: 6px 8px; font-size: 13px; border-radius: 8px; border: 1px solid #E2E8F0; text-align: center; }
.att-ot-input:disabled { background: #F8FAFC; color: #CBD5E1; }
.att-ot-suffix { font-size: 12px; color: #94A3B8; }

.att-card-footer {
  display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap;
  gap: 12px; padding: 16px 24px; border-top: 1px solid #E2E8F0;
}
.att-totals { font-size: 13px; color: #475569; display: flex; gap: 18px; flex-wrap: wrap; }
.att-totals b { color: #0F172A; }

.att-tip {
  display: flex; gap: 12px; align-items: flex-start; background: #FEF2F2; border: 1px solid #FECACA;
  border-radius: var(--radius); padding: 14px 18px; margin-top: 18px; font-size: 13px; color: #7F1D1D;
}
.att-tip i { color: var(--primary); font-size: 18px; margin-top: 1px; }

@media (max-width: 767px) {
  .att-day-tables .col-md-6 { border-right: none !important; }
}
"""

class SalaryPagesMixin:
    def handle_employee_add(self, form):
        db = get_db()
        db.execute(
            "INSERT INTO employees (name, designation, phone, basic_salary, overtime_rate, working_hours_per_day, advance, joining_date, leaving_date) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (form.get('name', ''), form.get('designation', ''), form.get('phone', ''),
             float(form.get('basic_salary', 0) or 0),
             float(form.get('overtime_rate', 1.5) or 1.5),
             float(form.get('working_hours_per_day', 8) or 8),
             float(form.get('advance', 0) or 0),
             form.get('joining_date', '') or None,
             form.get('leaving_date', '') or None))
        db.commit()
        db.close()
        month = form.get('month', '')
        return self.send(*redirect(f'/salary?month={month}' if month else '/salary'))


    def handle_employee_edit(self, eid, form):
        db = get_db()
        db.execute(
            "UPDATE employees SET name=?, designation=?, phone=?, basic_salary=?, "
            "overtime_rate=?, working_hours_per_day=?, advance=?, joining_date=?, leaving_date=? WHERE id=?",
            (form.get('name', ''), form.get('designation', ''), form.get('phone', ''),
             float(form.get('basic_salary', 0) or 0),
             float(form.get('overtime_rate', 1.5) or 1.5),
             float(form.get('working_hours_per_day', 8) or 8),
             float(form.get('advance', 0) or 0),
             form.get('joining_date', '') or None,
             form.get('leaving_date', '') or None, eid))
        db.commit()
        db.close()
        month = form.get('month', '')
        return self.send(*redirect(f'/salary/{eid}?month={month}' if month else f'/salary/{eid}'))


    def handle_attendance_save(self, eid, form):
        db = get_db()
        month = form.get('month', datetime.now().strftime('%Y-%m'))
        days_in_month = int(form.get('days_in_month', 30))
        emp = db.execute("SELECT * FROM employees WHERE id=?", (eid,)).fetchone()
        work_hrs = emp['working_hours_per_day'] if emp else 8
        active = _employee_active_range(emp, month) if emp else (1, days_in_month)
        first_day, last_day = active if active else (1, 0)  # (1,0) saves nothing
        for day in range(first_day, last_day + 1):
            date_str = f"{month}-{day:02d}"
            is_present = 1 if form.get(f'present_{day}') == 'on' else 0
            overtime = float(form.get(f'ot_{day}', 0) or 0)
            checkout = form.get(f'checkout_{day}', '')
            db.execute(
                "INSERT OR REPLACE INTO attendance (employee_id, date, is_present, check_in, check_out, overtime_hours) "
                "VALUES (?,?,?,?,?,?)",
                (eid, date_str, is_present, '', checkout, round(overtime, 2)))
        db.commit()
        db.close()
        return self.send(*redirect(f'/salary/{eid}?month={month}'))


    def handle_attendance_clear(self, eid, form):
        """Wipe all attendance rows for one employee/month so it can be
        re-entered from scratch (e.g. after entering a month wrong, or
        to start a fresh blank month)."""
        db = get_db()
        month = form.get('month', datetime.now().strftime('%Y-%m'))
        db.execute(
            "DELETE FROM attendance WHERE employee_id=? AND date LIKE ?",
            (eid, f"{month}%"))
        db.commit()
        db.close()
        return self.send(*redirect(f'/salary/{eid}/attendance?month={month}'))


    # ── PAGE: SALARY LIST ─────────────────────────────────────
    def pg_salary_list(self, month=None):
        db = get_db()
        employees = db.execute("SELECT * FROM employees ORDER BY name").fetchall()
        current_month = month or datetime.now().strftime('%Y-%m')
        rows = ''
        for e in employees:
            active = _employee_active_range(e, current_month)
            if not active:
                continue  # not employed during this month — leave off the list
            first_day, last_day = active
            days_in_month = _month_days(current_month)
            active_days = last_day - first_day + 1
            start_date = f"{current_month}-{first_day:02d}"
            end_date = f"{current_month}-{last_day:02d}"
            att = db.execute(
                "SELECT COUNT(*) as total, SUM(CASE WHEN is_present=1 THEN 1 ELSE 0 END) as present, "
                "SUM(overtime_hours) as ot_hours FROM attendance WHERE employee_id=? AND date BETWEEN ? AND ?",
                (e['id'], start_date, end_date)).fetchone()
            att_details = db.execute(
                "SELECT * FROM attendance WHERE employee_id=? AND date BETWEEN ? AND ? AND is_present=1",
                (e['id'], start_date, end_date)).fetchall()
            total_days = att['total'] or 0
            present_days = att['present'] or 0
            absent_days = total_days - present_days
            ot_hours = att['ot_hours'] or 0
            basic = e['basic_salary'] or 0
            advance = e['advance'] or 0
            work_hrs = e['working_hours_per_day'] or 8
            per_day = basic / 30
            hourly = per_day / work_hrs
            ot_pay = ot_hours * hourly * (e['overtime_rate'] or 1.5)
            # If this employee only joined/left partway through the
            # month, pay only for the days they were actually employed
            # instead of a full month's basic salary.
            basic_for_month = basic if active_days >= days_in_month else per_day * active_days
            # Partial day calculation
            partial_ded = 0
            for a in att_details:
                if a['check_out']:
                    try:
                        from datetime import datetime as dt
                        co = dt.strptime(a['check_out'], '%H:%M')
                        hw = (co - dt.strptime('09:00', '%H:%M')).total_seconds() / 3600
                        if hw < work_hrs:
                            partial_ded += per_day * (1 - hw / work_hrs)
                    except Exception:
                        pass
            absent_ded = per_day * absent_days if total_days > 0 else 0
            net = basic_for_month + ot_pay - absent_ded - advance - partial_ded
            period_note = ''
            if active_days < days_in_month:
                period_note = f' <small class="text-muted d-block">({active_days} days this month)</small>'
            rows += (f'<tr><td><strong>{esc(e["name"])}</strong>{period_note}</td>'
                     f'<td>{esc(e["designation"] or "-")}</td>'
                     f'<td>Rs. {fmt_rupee(basic_for_month)}</td>'
                     f'<td class="text-center">{present_days}/{total_days}</td>'
                     f'<td class="text-center">{absent_days}</td>'
                     f'<td class="text-center">{ot_hours:.1f} hrs</td>'
                     f'<td><strong style="color:#DC2626">Rs. {fmt_rupee(net)}</strong></td>'
                     f'<td class="text-end">'
                     f'<a href="/salary/{e["id"]}?month={current_month}" class="btn btn-sm btn-outline-primary me-1" title="View"><i class="bi bi-eye"></i></a>'
                     f'<a href="/salary/{e["id"]}/attendance?month={current_month}" class="btn btn-sm btn-outline-success me-1" title="Attendance"><i class="bi bi-calendar-check"></i></a>'
                     f'<form method="POST" action="/salary/{e["id"]}/delete" class="d-inline" onsubmit="return confirm(\'Delete this employee?\')">'
                     f'<button class="btn btn-sm btn-outline-danger"><i class="bi bi-trash"></i></button></form>'
                     f'</td></tr>')
        if not rows:
            rows = ('<tr><td colspan="8" class="text-center text-muted py-4">'
                    'No employees yet. <a href="/salary/new">Add your first employee</a></td></tr>')
        db.close()
        content = f'''
<div class="d-flex justify-content-between align-items-center mb-4 flex-wrap gap-2">
  <h5 class="mb-0"><i class="bi bi-cash-stack"></i> Salary Management <small class="text-muted" style="font-size:14px">({current_month})</small></h5>
  <div class="d-flex gap-2 align-items-center">
    <form method="GET" action="/salary" class="d-flex gap-1 align-items-center">
      <label class="text-muted small mb-0 me-1">Month</label>
      <input type="month" name="month" value="{current_month}" class="form-control form-control-sm" onchange="this.form.submit()">
    </form>
    <a href="/salary/new?month={current_month}" class="btn btn-primary btn-sm"><i class="bi bi-plus-circle"></i> Add Employee</a>
  </div>
</div>
<div class="table-card">
  <div class="table-responsive-wrapper">
    <table class="table table-hover mb-0">
      <thead><tr>
        <th style="padding:14px 16px">Name</th>
        <th style="padding:14px 16px">Designation</th>
        <th style="padding:14px 16px">Basic Salary</th>
        <th class="text-center" style="padding:14px 16px">Present</th>
        <th class="text-center" style="padding:14px 16px">Absent</th>
        <th class="text-center" style="padding:14px 16px">Overtime</th>
        <th style="padding:14px 16px">Net Salary</th>
        <th class="text-end" style="padding:14px 16px">Actions</th>
      </tr></thead>
      <tbody>{rows}</tbody>
    </table>
  </div>
</div>'''
        return self._layout('Salary', 'Salary Management', content, 'salary')


    # ── PAGE: NEW EMPLOYEE ────────────────────────────────────
    def pg_salary_new(self, month=None):
        back_href = f'/salary?month={month}' if month else '/salary'
        content = f'''
<div class="row g-4">
  <div class="col-lg-7">
    <div class="form-section">
      <div class="d-flex justify-content-between align-items-center mb-4">
        <h5 class="mb-0"><i class="bi bi-person-plus"></i> Add Employee</h5>
        <a href="{back_href}" class="btn btn-outline-primary btn-sm"><i class="bi bi-arrow-left"></i> Back</a>
      </div>
      <form method="POST" action="/salary/new">
        <input type="hidden" name="month" value="{month or ''}">
        <div class="mb-3">
          <label class="form-label">Employee Name *</label>
          <input name="name" class="form-control" required placeholder="Enter full name">
        </div>
        <div class="row g-3 mb-3">
          <div class="col-md-6">
            <label class="form-label">Designation</label>
            <input name="designation" class="form-control" placeholder="e.g. Supervisor, Worker">
          </div>
          <div class="col-md-6">
            <label class="form-label">Phone</label>
            <input name="phone" class="form-control" placeholder="Contact number">
          </div>
        </div>
        <hr>
        <h6 class="mb-3" style="color:#64748B"><i class="bi bi-cash"></i> Salary Details</h6>
        <div class="row g-3 mb-3">
          <div class="col-md-4">
            <label class="form-label">Basic Salary (Rs.) *</label>
            <input name="basic_salary" type="number" class="form-control" step="0.01" required placeholder="e.g. 15000">
          </div>
          <div class="col-md-4">
            <label class="form-label">Overtime Rate (x)</label>
            <input name="overtime_rate" type="number" class="form-control" value="1.5" step="0.1" min="1" placeholder="1.5">
            <small class="text-muted">Multiplier for OT hours</small>
          </div>
          <div class="col-md-4">
            <label class="form-label">Working Hours/Day</label>
            <input name="working_hours_per_day" type="number" class="form-control" value="8" step="0.5" min="1" placeholder="8">
            <small class="text-muted">Standard hours per day</small>
          </div>
        </div>
        <div class="row g-3 mb-3">
          <div class="col-md-4">
            <label class="form-label">Advance (Rs.)</label>
            <input name="advance" type="number" class="form-control" step="0.01" value="0" min="0" placeholder="0">
            <small class="text-muted">Monthly advance deduction</small>
          </div>
        </div>
        <hr>
        <h6 class="mb-3" style="color:#64748B"><i class="bi bi-calendar-range"></i> Employment Period</h6>
        <div class="row g-3 mb-3">
          <div class="col-md-6">
            <label class="form-label">Date of Joining</label>
            <input name="joining_date" type="date" class="form-control" value="''' + datetime.now().strftime('%Y-%m-%d') + '''">
            <small class="text-muted">Salary/attendance only counted from this date onward</small>
          </div>
          <div class="col-md-6">
            <label class="form-label">Date of Leaving</label>
            <input name="leaving_date" type="date" class="form-control">
            <small class="text-muted">Leave blank if still working</small>
          </div>
        </div>
        <div class="mt-4">
          <button type="submit" class="btn btn-primary btn-lg"><i class="bi bi-check-circle"></i> Save Employee</button>
        </div>
      </form>
    </div>
  </div>
  <div class="col-lg-5">
    <div class="form-section">
      <h5><i class="bi bi-info-circle"></i> How it works</h5>
      <div style="font-size:13px;color:#64748B;line-height:1.8">
        <p><b>Basic Salary:</b> Monthly fixed salary for 30 days.</p>
        <p><b>Overtime Rate:</b> If set to 1.5x, an employee earning Rs. 15,000 gets Rs. 865/hr for overtime.</p>
        <p><b>Working Hours:</b> Hours beyond this in a day count as overtime.</p>
        <hr>
        <p><b>Salary Formula (30 days):</b></p>
        <p>Per Day = Basic / 30</p>
        <p>Hourly Rate = Per Day / Working Hours</p>
        <p>OT Pay = OT Hours x Hourly Rate x OT Rate</p>
        <p>Absent Deduction = Per Day x Absent Days</p>
        <p><b>Net = Basic + OT Pay - Absent Deduction</b></p>
      </div>
    </div>
  </div>
</div>'''
        return self._layout('New Employee', 'Add Employee', content, 'salary')


    # ── PAGE: EDIT EMPLOYEE ───────────────────────────────────
    def pg_salary_edit(self, eid, month=None):
        db = get_db()
        emp = db.execute("SELECT * FROM employees WHERE id=?", (eid,)).fetchone()
        if not emp:
            db.close()
            return self.pg_404()
        db.close()

        back_href = f'/salary/{eid}?month={month}' if month else f'/salary/{eid}'

        content = f'''
<div class="row g-4 justify-content-center">
  <div class="col-lg-8">
    <div class="form-section">
      <div class="d-flex justify-content-between align-items-center mb-4">
        <h5 class="mb-0"><i class="bi bi-pencil"></i> Edit Employee - {esc(emp["name"])}</h5>
        <a href="{back_href}" class="btn btn-outline-primary btn-sm"><i class="bi bi-arrow-left"></i> Back</a>
      </div>
      <form method="POST" action="/salary/{eid}/edit">
        <input type="hidden" name="month" value="{month or ''}">
        <div class="mb-4">
          <label class="form-label">Employee Name *</label>
          <input name="name" class="form-control" required value="{esc(emp["name"])}">
        </div>
        <div class="row g-3 mb-4">
          <div class="col-md-6">
            <label class="form-label">Designation</label>
            <input name="designation" class="form-control" value="{esc(emp["designation"] or "")}">
          </div>
          <div class="col-md-6">
            <label class="form-label">Phone</label>
            <input name="phone" class="form-control" value="{esc(emp["phone"] or "")}">
          </div>
        </div>
        <hr class="my-4">
        <h6 class="mb-3" style="color:#64748B;font-weight:700"><i class="bi bi-cash"></i> Salary Details</h6>
        <div class="row g-3 mb-3">
          <div class="col-md-4">
            <label class="form-label">Basic Salary (Rs.) *</label>
            <input name="basic_salary" type="number" class="form-control" step="0.01" required value="{emp["basic_salary"]}">
          </div>
          <div class="col-md-4">
            <label class="form-label">Overtime Rate (x)</label>
            <input name="overtime_rate" type="number" class="form-control" value="{emp["overtime_rate"]}" step="0.1" min="1">
            <small class="text-muted">Multiplier for OT hours</small>
          </div>
          <div class="col-md-4">
            <label class="form-label">Working Hours/Day</label>
            <input name="working_hours_per_day" type="number" class="form-control" value="{emp["working_hours_per_day"]}" step="0.5" min="1">
            <small class="text-muted">Standard hours per day</small>
          </div>
        </div>
        <div class="row g-3 mb-4">
          <div class="col-md-4">
            <label class="form-label">Advance (Rs.)</label>
            <input name="advance" type="number" class="form-control" step="0.01" value="{emp["advance"]}" min="0">
            <small class="text-muted">Monthly advance deduction</small>
          </div>
        </div>
        <hr class="my-4">
        <h6 class="mb-3" style="color:#64748B;font-weight:700"><i class="bi bi-calendar-range"></i> Employment Period</h6>
        <div class="row g-3 mb-4">
          <div class="col-md-6">
            <label class="form-label">Date of Joining</label>
            <input name="joining_date" type="date" class="form-control" value="{emp["joining_date"] or ""}">
            <small class="text-muted">Salary/attendance only counted from this date onward</small>
          </div>
          <div class="col-md-6">
            <label class="form-label">Date of Leaving</label>
            <input name="leaving_date" type="date" class="form-control" value="{emp["leaving_date"] or ""}">
            <small class="text-muted">Leave blank if still working</small>
          </div>
        </div>
        <div class="d-flex gap-2">
          <button type="submit" class="btn btn-primary btn-lg"><i class="bi bi-check-circle"></i> Update Employee</button>
          <a href="{back_href}" class="btn btn-outline-secondary btn-lg">Cancel</a>
        </div>
      </form>
    </div>
  </div>
</div>'''
        return self._layout(f'Edit - {emp["name"]}', f'Edit Employee', content, 'salary')


    # ── PAGE: EMPLOYEE DETAIL ─────────────────────────────────
    def pg_salary_detail(self, eid, month=None):
        db = get_db()
        emp = db.execute("SELECT * FROM employees WHERE id=?", (eid,)).fetchone()
        if not emp:
            db.close()
            return self.pg_404()
        current_month = month or datetime.now().strftime('%Y-%m')
        active = _employee_active_range(emp, current_month)
        if not active:
            db.close()
            not_employed_notice = (
                '<div class="form-section text-center py-5 text-muted">'
                f'<i class="bi bi-calendar-x" style="font-size:28px"></i>'
                f'<p class="mt-2 mb-0">{esc(emp["name"])} wasn\'t employed during {current_month}.</p>'
                '</div>')
            content = f'''
<div class="d-flex justify-content-between align-items-center mb-4 flex-wrap gap-2">
  <h5 class="mb-0"><i class="bi bi-person"></i> {esc(emp["name"])}</h5>
  <div class="d-flex gap-2 align-items-center flex-wrap">
    <form method="GET" action="/salary/{eid}" class="d-flex gap-1">
      <input type="month" name="month" value="{current_month}" class="form-control form-control-sm" onchange="this.form.submit()">
    </form>
    <a href="/salary/{eid}/edit?month={current_month}" class="btn btn-primary btn-sm"><i class="bi bi-pencil"></i> Edit</a>
    <a href="/salary?month={current_month}" class="btn btn-outline-primary btn-sm"><i class="bi bi-arrow-left"></i> Back</a>
  </div>
</div>
{not_employed_notice}'''
            return self._layout(emp['name'], f'Salary - {emp["name"]}', content, 'salary')

        first_day, last_day = active
        days_in_month = _month_days(current_month)
        active_days = last_day - first_day + 1
        start_date = f"{current_month}-{first_day:02d}"
        end_date = f"{current_month}-{last_day:02d}"
        att = db.execute(
            "SELECT * FROM attendance WHERE employee_id=? AND date BETWEEN ? AND ? ORDER BY date",
            (eid, start_date, end_date)).fetchall()
        db.close()

        present_days = sum(1 for a in att if a['is_present'])
        absent_days = sum(1 for a in att if not a['is_present'])
        total_ot = sum(a['overtime_hours'] or 0 for a in att)
        basic = emp['basic_salary'] or 0
        advance = emp['advance'] or 0
        ot_rate = emp['overtime_rate'] or 1.5
        work_hrs = emp['working_hours_per_day'] or 8
        per_day = basic / 30
        hourly = per_day / work_hrs
        ot_pay = total_ot * hourly * ot_rate
        basic_for_month = basic if active_days >= days_in_month else per_day * active_days

        # Calculate partial day deductions
        # If checkout time is before standard end, calculate hours worked and deduct partial day pay
        partial_ded = 0
        partial_days = 0
        for a in att:
            if a['is_present'] and a['check_out']:
                try:
                    from datetime import datetime as dt
                    # Standard start is 9:00 AM
                    checkout_time = dt.strptime(a['check_out'], '%H:%M')
                    standard_end = dt.strptime('17:00', '%H:%M')  # 5 PM = 8 hrs from 9 AM
                    hours_worked = (checkout_time - dt.strptime('09:00', '%H:%M')).total_seconds() / 3600
                    if hours_worked < work_hrs:
                        # Partial day - deduct proportional amount
                        deduction = per_day * (1 - hours_worked / work_hrs)
                        partial_ded += deduction
                        partial_days += 1
                except Exception:
                    pass

        absent_ded = per_day * absent_days
        net = basic_for_month + ot_pay - absent_ded - advance - partial_ded
        basic_label = f"Basic Salary ({active_days}/{days_in_month} days)" if active_days < days_in_month else "Basic Salary"

        att_rows = ''
        for a in att:
            status = '<span style="color:#10B981">Present</span>' if a['is_present'] else '<span style="color:#DC2626">Absent</span>'
            att_rows += (f'<tr><td>{esc(a["date"])}</td><td class="text-center">{status}</td>'
                         f'<td class="text-center">{a["overtime_hours"]:.1f} hrs</td></tr>')

        content = f'''
<div class="d-flex justify-content-between align-items-center mb-4 flex-wrap gap-2">
  <h5 class="mb-0"><i class="bi bi-person"></i> {esc(emp["name"])}</h5>
  <div class="d-flex gap-2 align-items-center flex-wrap">
    <form method="GET" action="/salary/{eid}" class="d-flex gap-1">
      <input type="month" name="month" value="{current_month}" class="form-control form-control-sm" onchange="this.form.submit()">
    </form>
    <a href="/salary/{eid}/edit?month={current_month}" class="btn btn-primary btn-sm"><i class="bi bi-pencil"></i> Edit</a>
    <a href="/salary/{eid}/attendance?month={current_month}" class="btn btn-success btn-sm"><i class="bi bi-calendar-check"></i> Mark Attendance</a>
    <a href="/salary?month={current_month}" class="btn btn-outline-primary btn-sm"><i class="bi bi-arrow-left"></i> Back</a>
  </div>
</div>
<div class="row g-4">
  <div class="col-lg-4">
    <div class="form-section mb-4">
      <h5 class="text-nowrap"><i class="bi bi-person-badge"></i> Employee Details</h5>
      <table class="table table-sm mb-0">
        <tr><td class="text-muted text-nowrap" style="width:40%">Name</td><td><b>{esc(emp["name"])}</b></td></tr>
        <tr><td class="text-muted text-nowrap">Designation</td><td>{esc(emp["designation"] or "-")}</td></tr>
        <tr><td class="text-muted text-nowrap">Phone</td><td>{esc(emp["phone"] or "-")}</td></tr>
        <tr><td class="text-muted text-nowrap">Basic Salary</td><td><b>Rs. {fmt_rupee(basic)}</b></td></tr>
        <tr><td class="text-muted text-nowrap">OT Rate</td><td>{ot_rate}x</td></tr>
        <tr><td class="text-muted text-nowrap">Work Hours/Day</td><td>{work_hrs} hrs</td></tr>
        <tr><td class="text-muted text-nowrap">Advance</td><td>Rs. {fmt_rupee(advance)}</td></tr>
        <tr><td class="text-muted text-nowrap">Joined</td><td>{esc(emp["joining_date"]) if emp["joining_date"] else "-"}</td></tr>
        <tr><td class="text-muted text-nowrap">Left</td><td>{esc(emp["leaving_date"]) if emp["leaving_date"] else "Still working"}</td></tr>
      </table>
    </div>
    <div class="form-section">
      <h5 class="text-nowrap"><i class="bi bi-calculator"></i> {current_month} Salary</h5>
      <div class="summary-box">
        <div class="total-line"><span>Present Days</span><b>{present_days}</b></div>
        <div class="total-line"><span>Absent Days</span><b style="color:#DC2626">{absent_days}</b></div>
        <div class="total-line"><span>Overtime</span><b>{total_ot:.1f} hrs</b></div>
        <hr>
        <div class="total-line"><span>{basic_label}</span><b>Rs. {fmt_rupee(basic_for_month)}</b></div>
        <div class="total-line"><span>OT Pay</span><b style="color:#10B981">+ Rs. {fmt_rupee(ot_pay)}</b></div>
        <div class="total-line"><span>Absent Deduction</span><b style="color:#DC2626">- Rs. {fmt_rupee(absent_ded)}</b></div>
        <div class="total-line"><span>Partial Day ({partial_days} days)</span><b style="color:#DC2626">- Rs. {fmt_rupee(partial_ded)}</b></div>
        <div class="total-line"><span>Advance</span><b style="color:#DC2626">- Rs. {fmt_rupee(advance)}</b></div>
        <div class="total-line grand-total"><span>Net Salary</span><b>Rs. {fmt_rupee(net)}</b></div>
      </div>
    </div>
  </div>
  <div class="col-lg-8">
    <div class="form-section">
      <h5 class="text-nowrap"><i class="bi bi-calendar3"></i> Attendance - {current_month}</h5>
      <div class="table-responsive-wrapper">
        <table class="table table-hover mb-0">
          <thead><tr>
            <th style="padding:14px 16px">Date</th>
            <th class="text-center" style="padding:14px 16px">Status</th>
            <th class="text-center" style="padding:14px 16px">OT Hours</th>
          </tr></thead>
          <tbody>{att_rows if att_rows else '<tr><td colspan="3" class="text-center text-muted py-4">No attendance recorded yet</td></tr>'}</tbody>
        </table>
      </div>
    </div>
  </div>
</div>'''
        return self._layout(emp['name'], f'Salary - {emp["name"]}', content, 'salary')


    # ── PAGE: ATTENDANCE MARKING ──────────────────────────────
    def pg_salary_attendance(self, eid, month=None):
        db = get_db()
        emp = db.execute("SELECT * FROM employees WHERE id=?", (eid,)).fetchone()
        if not emp:
            db.close()
            return self.pg_404()
        current_month = month or datetime.now().strftime('%Y-%m')
        year, mo = (int(p) for p in current_month.split('-'))
        days_in_month = _month_days(current_month)
        active = _employee_active_range(emp, current_month)
        emp_code = f'EMP-{eid:03d}'
        initial = esc((emp['name'] or '?').strip()[:1].upper() or '?')
        crumb = f'''
<div class="att-breadcrumb">
  <a href="/salary?month={current_month}">Salary</a><span class="sep">/</span>
  <a href="/salary/{eid}/attendance?month={current_month}">Mark Attendance</a><span class="sep">/</span>
  <span class="current">{esc(emp["name"])}</span>
</div>'''
        if not active:
            db.close()
            content = f'''
<style>{ATT_STYLE}</style>
{crumb}
<div class="att-page-head">
  <div>
    <h1>Mark Attendance - {esc(emp["name"])}</h1>
    <p>Set daily attendance, checkout time and overtime for salary calculation.</p>
  </div>
  <div class="att-page-head-actions">
    <form method="GET" action="/salary/{eid}/attendance" class="att-month-input">
      <i class="bi bi-calendar3"></i>
      <input type="month" name="month" value="{current_month}" class="form-control form-control-sm" onchange="this.form.submit()">
    </form>
    <a href="/salary/{eid}?month={current_month}" class="btn btn-outline-secondary btn-sm"><i class="bi bi-arrow-left"></i> Back</a>
  </div>
</div>
<div class="form-section text-center py-5 text-muted">
  <i class="bi bi-calendar-x" style="font-size:28px"></i>
  <p class="mt-2 mb-0">{esc(emp["name"])} wasn&#39;t employed during {current_month}.</p>
</div>'''
            return self._layout(f'Attendance - {emp["name"]}', f'Mark Attendance - {emp["name"]}', content, 'salary')

        first_day, last_day = active
        total_days = last_day - first_day + 1
        range_note = ''
        if first_day > 1 or last_day < days_in_month:
            range_note = (f'<p class="text-muted small mb-3"><i class="bi bi-info-circle"></i> '
                          f'Showing days {first_day}-{last_day} only, based on this employee&#39;s joining/leaving date.</p>')

        # Load existing attendance
        existing = {}
        for row in db.execute(
            "SELECT * FROM attendance WHERE employee_id=? AND date LIKE ?",
            (eid, f"{current_month}%")).fetchall():
            day = int(row['date'].split('-')[2])
            existing[day] = row
        db.close()

        present_count = sum(1 for r in existing.values() if r['is_present'])
        absent_count = sum(1 for r in existing.values() if not r['is_present'])
        total_ot = sum((r['overtime_hours'] or 0) for r in existing.values())

        def day_row(day):
            att = existing.get(day)
            checked = 'checked' if (att and att['is_present']) or (not att) else ''
            ot_val = att['overtime_hours'] if att and att['overtime_hours'] else 0
            co_val = att['check_out'] if att and att['check_out'] else ''
            day_name = datetime(year, mo, day).strftime('%a')
            mon_name = datetime(year, mo, day).strftime('%b')
            off_cls = '' if (att and att['is_present']) or (not att) else ' class="att-day-off"'
            return (
                f'<tr id="row{day}"{off_cls}>'
                f'<td class="att-day-num">{day}</td>'
                f'<td class="att-day-date">{day_name}, {mon_name} {day}</td>'
                f'<td class="text-center"><label class="att-switch">'
                f'<input type="checkbox" name="present_{day}" {checked} onchange="toggleDay(this,{day})">'
                f'<span class="slider"></span></label></td>'
                f'<td><input type="time" name="checkout_{day}" value="{co_val}" class="att-co-input" id="co{day}"></td>'
                f'<td class="text-center"><div class="att-ot-cell">'
                f'<input type="number" name="ot_{day}" value="{ot_val}" class="att-ot-input" id="ot{day}" min="0" max="24" step="0.5" placeholder="0">'
                f'<span class="att-ot-suffix">hrs</span></div></td>'
                f'</tr>')

        full_range = list(range(first_day, last_day + 1))
        split = (len(full_range) + 1) // 2
        left_days, right_days = full_range[:split], full_range[split:]
        left_rows = ''.join(day_row(d) for d in left_days)
        right_rows = ''.join(day_row(d) for d in right_days)

        def side_table(rows_html):
            if not rows_html:
                return ''
            return f'''
        <div class="table-responsive-wrapper">
          <table class="att-day-table">
            <thead><tr><th>Day</th><th>Date</th><th class="text-center">Present</th><th>Checkout Time</th><th class="text-center">OT Hours</th></tr></thead>
            <tbody>{rows_html}</tbody>
          </table>
        </div>'''

        right_col = f'''
      <div class="col-md-6">{side_table(right_rows)}</div>''' if right_rows else ''

        content = f'''
<style>{ATT_STYLE}</style>
{crumb}
<div class="att-page-head">
  <div>
    <h1>Mark Attendance - {esc(emp["name"])}</h1>
    <p>Set daily attendance, checkout time and overtime for salary calculation.</p>
  </div>
  <div class="att-page-head-actions">
    <form method="GET" action="/salary/{eid}/attendance" class="att-month-input">
      <i class="bi bi-calendar3"></i>
      <input type="month" name="month" value="{current_month}" class="form-control form-control-sm" onchange="this.form.submit()">
    </form>
    <a href="/salary/{eid}?month={current_month}" class="btn btn-outline-secondary btn-sm"><i class="bi bi-arrow-left"></i> Back</a>
  </div>
</div>
{range_note}
<div class="att-summary-card">
  <div class="att-emp-info">
    <div class="att-emp-avatar">{initial}</div>
    <div>
      <h4>{esc(emp["name"])}</h4>
      <div class="att-emp-meta">Employee ID: {emp_code}</div>
      <div class="att-emp-meta">Designation: {esc(emp["designation"]) if emp["designation"] else "-"}</div>
    </div>
  </div>
  <div class="att-mini-stats">
    <div class="att-mini-stat">
      <div class="att-mini-stat-icon" style="background:#EFF6FF;color:#3B82F6"><i class="bi bi-people-fill"></i></div>
      <div><div class="att-mini-stat-value">{total_days}</div><div class="att-mini-stat-label">Total Days</div></div>
    </div>
    <div class="att-mini-stat">
      <div class="att-mini-stat-icon" style="background:#ECFDF5;color:#10B981"><i class="bi bi-check-lg"></i></div>
      <div><div class="att-mini-stat-value">{present_count}</div><div class="att-mini-stat-label">Present</div></div>
    </div>
    <div class="att-mini-stat">
      <div class="att-mini-stat-icon" style="background:#FEF2F2;color:#DC2626"><i class="bi bi-x-lg"></i></div>
      <div><div class="att-mini-stat-value">{absent_count}</div><div class="att-mini-stat-label">Absent</div></div>
    </div>
    <div class="att-mini-stat">
      <div class="att-mini-stat-icon" style="background:#FFFBEB;color:#F59E0B"><i class="bi bi-clock-history"></i></div>
      <div><div class="att-mini-stat-value">{total_ot:.1f} hrs</div><div class="att-mini-stat-label">Total OT Hours</div></div>
    </div>
  </div>
</div>

<div class="table-card">
  <div class="card-header att-card-header">
    <h5><i class="bi bi-calendar-check"></i> Daily Attendance</h5>
    <div class="d-flex gap-2 align-items-center flex-wrap">
      <div class="dropdown">
        <button class="btn btn-outline-secondary btn-sm dropdown-toggle" type="button" data-bs-toggle="dropdown" aria-expanded="false">
          <i class="bi bi-lightning-charge"></i> Quick Actions
        </button>
        <ul class="dropdown-menu dropdown-menu-end">
          <li><a class="dropdown-item" href="#" onclick="attQuick('present');return false;"><i class="bi bi-check2-all"></i> Mark All Present</a></li>
          <li><a class="dropdown-item" href="#" onclick="attQuick('absent');return false;"><i class="bi bi-x-octagon"></i> Mark All Absent</a></li>
          <li><hr class="dropdown-divider"></li>
          <li><a class="dropdown-item" href="#" onclick="attQuick('clear_checkout');return false;"><i class="bi bi-eraser"></i> Clear Checkout Times</a></li>
          <li><a class="dropdown-item" href="#" onclick="attQuick('clear_ot');return false;"><i class="bi bi-eraser"></i> Clear OT Hours</a></li>
        </ul>
      </div>
      <form method="POST" action="/salary/{eid}/attendance/clear" onsubmit="return confirm('Clear all attendance entered for {current_month}? This cannot be undone.')">
        <input type="hidden" name="month" value="{current_month}">
        <button type="submit" class="btn btn-outline-danger btn-sm"><i class="bi bi-x-circle"></i> Clear Month</button>
      </form>
    </div>
  </div>
  <form method="POST" action="/salary/{eid}/attendance" id="attForm">
    <input type="hidden" name="month" value="{current_month}">
    <input type="hidden" name="days_in_month" value="{days_in_month}">
    <div class="row g-0 att-day-tables">
      <div class="col-md-6" style="border-right:1px solid #E2E8F0">{side_table(left_rows)}</div>{right_col}
    </div>
    <div class="att-card-footer">
      <div class="att-totals">
        <span>Total Present: <b>{present_count}</b></span>
        <span>Total Absent: <b>{absent_count}</b></span>
        <span>Total OT Hours: <b>{total_ot:.1f}</b></span>
      </div>
      <div class="d-flex gap-2">
        <a href="/salary/{eid}?month={current_month}" class="btn btn-outline-secondary">Cancel</a>
        <button type="submit" class="btn btn-primary"><i class="bi bi-save"></i> Save Attendance</button>
      </div>
    </div>
  </form>
</div>

<div class="att-tip">
  <i class="bi bi-lightbulb-fill"></i>
  <div><b>Tip:</b> Mark attendance and add checkout time for days when the employee was present. Overtime hours will be calculated in the salary.</div>
</div>

<script>
function toggleDay(el, day) {{
  var ot = document.getElementById('ot' + day);
  var co = document.getElementById('co' + day);
  var row = document.getElementById('row' + day);
  if (!el.checked) {{
    ot.value = 0; ot.disabled = true;
    co.value = ''; co.disabled = true;
    if (row) row.classList.add('att-day-off');
  }} else {{
    ot.disabled = false; co.disabled = false;
    if (row) row.classList.remove('att-day-off');
  }}
}}
document.querySelectorAll('input[type=checkbox][name^="present_"]').forEach(function(el) {{
  if (!el.checked) {{
    var day = el.name.replace('present_', '');
    var ot = document.getElementById('ot' + day);
    var co = document.getElementById('co' + day);
    if (ot) {{ ot.disabled = true; }}
    if (co) {{ co.disabled = true; }}
  }}
}});
function attQuick(action) {{
  document.querySelectorAll('input[type=checkbox][name^="present_"]').forEach(function(el) {{
    if (action === 'present' && !el.checked) {{ el.checked = true; el.dispatchEvent(new Event('change')); }}
    if (action === 'absent' && el.checked) {{ el.checked = false; el.dispatchEvent(new Event('change')); }}
  }});
  if (action === 'clear_checkout') {{
    document.querySelectorAll('input[id^="co"]').forEach(function(el) {{ if (!el.disabled) el.value = ''; }});
  }}
  if (action === 'clear_ot') {{
    document.querySelectorAll('input[id^="ot"]').forEach(function(el) {{ if (!el.disabled) el.value = 0; }});
  }}
}}
</script>'''
        return self._layout(f'Attendance - {emp["name"]}', f'Mark Attendance - {emp["name"]}', content, 'salary')
