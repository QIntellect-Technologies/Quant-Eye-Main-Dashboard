"""
QuantEye Phase 2.4 — Verification Suite: Existing Pages Connected to Real Cloud Data
Validates:
1. Existing Attendance Report page loads & displays real cloud data with exact columns
2. Existing Staff Directory loads & displays real employee fields
3. Existing Leave Management displays real leave requests
4. Existing Overtime Management displays real overtime records
5. Existing Shift Management displays real shift schedules
6. Existing Payroll displays real payroll runs & employee lines
7. Main Dashboard KPIs load real PostgreSQL aggregates
8. Strict Tenant & Branch isolation across all reporting endpoints
9. Module entitlement enforcement (unlicensed modules denied)
10. End-to-end sync outbox -> cloud batch -> existing attendance reporting
"""

import unittest
import json
import os
import sys
from datetime import datetime, timezone, timedelta

# Ensure QuantEye-Main-Dashboard is in path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app import app
import cloud_database as cloud_db
import database as db

class TestPhase24CloudData(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app.config['TESTING'] = True
        cls.client = app.test_client()

        # Seed test organizations, branches, and sample real data in PostgreSQL
        cls.org1_id = "ORG-PHASE24-ALPHA"
        cls.branch1_id = "BR-P24-A1"
        cls.org2_id = "ORG-PHASE24-BETA"
        cls.branch2_id = "BR-P24-B1"

        # Initialize cloud pool
        cloud_db.init_cloud_pool()

        with cloud_db.tenant_context(is_platform_admin=True, enforce_rls=False) as cur:
            # Create Org 1
            cur.execute("""
                INSERT INTO organizations (id, name, industry, status)
                VALUES (%s, %s, 'Software', 'Active')
                ON CONFLICT (id) DO NOTHING;
            """, (cls.org1_id, "Alpha Innovations"))

            cur.execute("""
                INSERT INTO branches (id, organization_id, name, city, access)
                VALUES (%s, %s, 'Alpha Headquarters', 'Metropolis', 'Active')
                ON CONFLICT (organization_id, id) DO NOTHING;
            """, (cls.branch1_id, cls.org1_id))

            # Enable modules for Org 1 (all licensed)
            for mod_id in ("attendance", "shifts", "leave", "overtime", "payroll"):
                cur.execute("""
                    INSERT INTO organization_modules (id, organization_id, module_id, is_enabled)
                    VALUES (%s, %s, %s, TRUE)
                    ON CONFLICT (organization_id, module_id) DO UPDATE SET is_enabled = TRUE;
                """, (f"OM-{cls.org1_id}-{mod_id}", cls.org1_id, mod_id))

            # Create Org 2 (only attendance and staff licensed, NO payroll)
            cur.execute("""
                INSERT INTO organizations (id, name, industry, status)
                VALUES (%s, %s, 'Logistics', 'Active')
                ON CONFLICT (id) DO NOTHING;
            """, (cls.org2_id, "Beta Logistics"))

            cur.execute("""
                INSERT INTO branches (id, organization_id, name, city, access)
                VALUES (%s, %s, 'Beta Port Branch', 'Coast City', 'Active')
                ON CONFLICT (organization_id, id) DO NOTHING;
            """, (cls.branch2_id, cls.org2_id))

            cur.execute("""
                INSERT INTO organization_modules (id, organization_id, module_id, is_enabled)
                VALUES (%s, %s, 'attendance', TRUE)
                ON CONFLICT (organization_id, module_id) DO UPDATE SET is_enabled = TRUE;
            """, (f"OM-{cls.org2_id}-attendance", cls.org2_id))

            cur.execute("""
                INSERT INTO organization_modules (id, organization_id, module_id, is_enabled)
                VALUES (%s, %s, 'payroll', FALSE)
                ON CONFLICT (organization_id, module_id) DO UPDATE SET is_enabled = FALSE;
            """, (f"OM-{cls.org2_id}-payroll", cls.org2_id))

            # Seed Real Employees for Org 1
            cls.emp1_id = f"EMP-{cls.org1_id}-001"
            cls.emp2_id = f"EMP-{cls.org1_id}-002"
            cur.execute("""
                INSERT INTO employees (id, organization_id, branch_id, employee_code, name, email, phone, department, position, salary, is_active)
                VALUES (%s, %s, %s, 'EMP-101', 'Zainab Qasim', 'zainab@alpha.com', '+923001234567', 'Engineering', 'Lead Architect', 150000.00, TRUE)
                ON CONFLICT (organization_id, id) DO NOTHING;
            """, (cls.emp1_id, cls.org1_id, cls.branch1_id))

            cur.execute("""
                INSERT INTO employees (id, organization_id, branch_id, employee_code, name, email, phone, department, position, salary, is_active)
                VALUES (%s, %s, %s, 'EMP-102', 'Hamza Tariq', 'hamza@alpha.com', '+923007654321', 'Operations', 'DevOps Specialist', 120000.00, TRUE)
                ON CONFLICT (organization_id, id) DO NOTHING;
            """, (cls.emp2_id, cls.org1_id, cls.branch1_id))

            # Seed Real Attendance Events for Org 1
            now_utc = datetime.now(timezone.utc)
            cls.today_str = now_utc.strftime("%Y-%m-%d")

            cur.execute("""
                INSERT INTO attendance_events (id, organization_id, branch_id, employee_id, event_type, confidence, timestamp, device_id, location, source)
                VALUES (%s, %s, %s, %s, 'CHECK_IN', 0.98, %s, 'Main Entrance Cam', 'Front Gate', 'auto')
                ON CONFLICT (organization_id, id) DO NOTHING;
            """, (f"ATT-{cls.org1_id}-001", cls.org1_id, cls.branch1_id, cls.emp1_id, now_utc))

            # Seed Shifts for Org 1
            cls.shift1_id = f"SHIFT-{cls.org1_id}-01"
            cur.execute("""
                INSERT INTO shifts (id, organization_id, branch_id, name, start_time, end_time, grace_minutes, working_days, is_default, is_active)
                VALUES (%s, %s, %s, 'Morning Standard', '09:00:00', '18:00:00', 15, '[0,1,2,3,4,5]', TRUE, TRUE)
                ON CONFLICT (organization_id, id) DO NOTHING;
            """, (cls.shift1_id, cls.org1_id, cls.branch1_id))

            # Seed Leave Requests for Org 1
            cls.leave1_id = f"LV-{cls.org1_id}-01"
            cur.execute("""
                INSERT INTO leave_requests (id, organization_id, branch_id, employee_id, leave_type, start_date, end_date, status, reason, approved_by)
                VALUES (%s, %s, %s, %s, 'annual', %s, %s, 'approved', 'Family vacation', 'Director HR')
                ON CONFLICT (organization_id, id) DO NOTHING;
            """, (cls.leave1_id, cls.org1_id, cls.branch1_id, cls.emp2_id, cls.today_str, cls.today_str))

            # Seed Overtime for Org 1
            cls.ot1_id = f"OT-{cls.org1_id}-01"
            cur.execute("""
                INSERT INTO overtime_records (id, organization_id, branch_id, employee_id, work_date, hours, reason, status, approved_by)
                VALUES (%s, %s, %s, %s, %s, 3.5, 'Deployment support', 'approved', 'Tech Lead')
                ON CONFLICT (organization_id, id) DO NOTHING;
            """, (cls.ot1_id, cls.org1_id, cls.branch1_id, cls.emp1_id, cls.today_str))

            # Seed Payroll Run & Records for Org 1
            cls.run1_id = f"PAY-{cls.org1_id}-2026-10"
            cur.execute("""
                INSERT INTO payroll_runs (id, organization_id, branch_id, payroll_month, period_start, period_end, employee_count, total_basic_salary, total_deduction, total_amount, status)
                VALUES (%s, %s, %s, '2026-10', '2026-10-01', '2026-10-31', 2, 270000.00, 5000.00, 265000.00, 'approved')
                ON CONFLICT (organization_id, id) DO NOTHING;
            """, (cls.run1_id, cls.org1_id, cls.branch1_id))

            cls.rec1_id = f"PREC-{cls.org1_id}-001"
            cur.execute("""
                INSERT INTO payroll_records (id, organization_id, branch_id, payroll_run_id, employee_id, basic_salary, allowances, deductions, net_salary, scheduled_workdays, present_days, payable_days)
                VALUES (%s, %s, %s, %s, %s, 150000.00, 10000.00, 2000.00, 158000.00, 22, 22, 22.0)
                ON CONFLICT (organization_id, id) DO NOTHING;
            """, (cls.rec1_id, cls.org1_id, cls.branch1_id, cls.run1_id, cls.emp1_id))

    def test_01_attendance_report_page_and_cloud_data(self):
        """1. Existing Attendance Report page loads and serves real cloud data."""
        # A. Page route returns 200
        resp = self.client.get('/attendance-report')
        self.assertEqual(resp.status_code, 200)
        page_html = resp.data.decode('utf-8')
        # Verify page is the real Attendance Report (contains existing column headers)
        self.assertIn("Attendance Report", page_html)
        self.assertIn("Employee", page_html)
        self.assertIn("Department", page_html)

        # B. Real Cloud Attendance API returns real records
        api_resp = self.client.get(f'/api/cloud/reports/attendance?organization_id={self.org1_id}&branch_id={self.branch1_id}&date={self.today_str}')
        self.assertEqual(api_resp.status_code, 200)
        data = api_resp.get_json()
        self.assertIn('summary', data)
        self.assertIn('records', data)
        self.assertGreaterEqual(data['summary']['total_enrolled'], 2)

        # Check existing columns exist in returned records
        rec = next((r for r in data['records'] if r['name'] == 'Zainab Qasim'), None)
        self.assertIsNotNone(rec)
        self.assertIn(rec['status'], ('PRESENT', 'CHECKED_IN', 'CHECKED_OUT'))
        self.assertEqual(rec['department'], 'Engineering')
        self.assertEqual(rec['checkin_camera'], 'Main Entrance Cam')

    def test_02_staff_directory_cloud_data(self):
        """2. Existing Staff Directory loads real employee fields."""
        # A. Portal route returns 200
        resp = self.client.get('/client/portal')
        self.assertEqual(resp.status_code, 200)

        # B. Real Cloud Staff API returns real employee records
        api_resp = self.client.get(f'/api/cloud/staff?organization_id={self.org1_id}&branch_id={self.branch1_id}')
        self.assertEqual(api_resp.status_code, 200)
        staff_list = api_resp.get_json()
        self.assertGreaterEqual(len(staff_list), 2)

        zainab = next((s for s in staff_list if s['name'] == 'Zainab Qasim'), None)
        self.assertIsNotNone(zainab)
        self.assertEqual(zainab['department'], 'Engineering')
        self.assertEqual(zainab['position'], 'Lead Architect')
        self.assertEqual(zainab['salary'], 150000.0)
        self.assertEqual(zainab['phone'], '+923001234567')
        self.assertEqual(zainab['active'], 1)

    def test_03_leave_management_cloud_data(self):
        """3. Existing Leave Management displays real leave requests."""
        api_resp = self.client.get(f'/api/cloud/leaves?organization_id={self.org1_id}&branch_id={self.branch1_id}')
        self.assertEqual(api_resp.status_code, 200)
        leaves = api_resp.get_json()
        self.assertGreaterEqual(len(leaves), 1)

        leave_item = leaves[0]
        self.assertEqual(leave_item['user_name'], 'Hamza Tariq')
        self.assertEqual(leave_item['leave_type'], 'annual')
        self.assertEqual(leave_item['status'], 'approved')
        self.assertEqual(leave_item['reason'], 'Family vacation')
        self.assertEqual(leave_item['approved_by'], 'Director HR')

    def test_04_overtime_management_cloud_data(self):
        """4. Existing Overtime Management displays real overtime records."""
        api_resp = self.client.get(f'/api/cloud/overtime?organization_id={self.org1_id}&branch_id={self.branch1_id}')
        self.assertEqual(api_resp.status_code, 200)
        ot_list = api_resp.get_json()
        self.assertGreaterEqual(len(ot_list), 1)

        ot_item = ot_list[0]
        self.assertEqual(ot_item['user_name'], 'Zainab Qasim')
        self.assertEqual(ot_item['hours'], 3.5)
        self.assertEqual(ot_item['status'], 'approved')
        self.assertEqual(ot_item['reason'], 'Deployment support')

    def test_05_shift_management_cloud_data(self):
        """5. Existing Shift Management displays real shifts."""
        api_resp = self.client.get(f'/api/cloud/shifts?organization_id={self.org1_id}&branch_id={self.branch1_id}')
        self.assertEqual(api_resp.status_code, 200)
        shifts = api_resp.get_json()
        self.assertGreaterEqual(len(shifts), 1)

        shift_item = shifts[0]
        self.assertEqual(shift_item['name'], 'Morning Standard')
        self.assertEqual(shift_item['grace_period_minutes'], 15)
        self.assertTrue(shift_item['is_default'])

    def test_06_payroll_cloud_data(self):
        """6. Existing Payroll displays real payroll runs and employee records."""
        # A. Payroll Runs
        runs_resp = self.client.get(f'/api/cloud/payroll/runs?organization_id={self.org1_id}&branch_id={self.branch1_id}')
        self.assertEqual(runs_resp.status_code, 200)
        runs = runs_resp.get_json()
        self.assertGreaterEqual(len(runs), 1)

        run_item = runs[0]
        self.assertEqual(run_item['payroll_month'], '2026-10')
        self.assertEqual(run_item['employee_count'], 2)
        self.assertEqual(run_item['total_basic_salary'], 270000.0)
        self.assertEqual(run_item['status'], 'approved')

        # B. Payroll Records for that run
        rec_resp = self.client.get(f'/api/cloud/payroll/runs/{self.run1_id}/records?organization_id={self.org1_id}&branch_id={self.branch1_id}')
        self.assertEqual(rec_resp.status_code, 200)
        records = rec_resp.get_json()
        self.assertGreaterEqual(len(records), 1)

        p_rec = records[0]
        self.assertEqual(p_rec['employee_name'], 'Zainab Qasim')
        self.assertEqual(p_rec['basic_salary'], 150000.0)
        self.assertEqual(p_rec['net_salary'], 158000.0)
        self.assertEqual(p_rec['payable_days'], 22.0)

    def test_07_main_dashboard_real_kpis(self):
        """7. Main Dashboard KPIs load real aggregates directly from PostgreSQL."""
        resp = self.client.get('/api/dashboard/stats')
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data.get('success'))
        stats = data.get('stats', {})

        # Check real counts exist and are positive
        self.assertGreaterEqual(stats.get('organizations_total', 0), 2)
        self.assertGreaterEqual(stats.get('organizations_active', 0), 2)
        self.assertGreaterEqual(stats.get('modules_total', 0), 5)
        self.assertGreaterEqual(stats.get('employees_total', 0), 2)
        self.assertGreaterEqual(stats.get('events_total', 0), 1)

    def test_08_tenant_and_branch_isolation(self):
        """8. Cross-tenant queries are blocked or return only tenant-owned data."""
        # Request Org 2 data with Org 1 parameter: Org 2 should NOT see Org 1 records
        resp = self.client.get(f'/api/cloud/staff?organization_id={self.org2_id}')
        self.assertEqual(resp.status_code, 200)
        staff_list = resp.get_json()
        # Org 2 has no employees in our seed; must NOT contain Zainab Qasim
        self.assertFalse(any(s['name'] == 'Zainab Qasim' for s in staff_list))

    def test_09_module_entitlement_enforcement(self):
        """9. Organizations cannot access commercial modules they have not licensed."""
        # Org 2 does not have the 'payroll' module enabled
        resp = self.client.get(f'/api/cloud/payroll/runs?organization_id={self.org2_id}')
        self.assertEqual(resp.status_code, 403)
        data = resp.get_json()
        self.assertIn("not licensed", data.get("error", ""))

    def test_10_end_to_end_edge_sync_to_reporting(self):
        """10. End-to-end: Edge sync batch ingests event -> immediately visible in Cloud Attendance Report."""
        # A. Authenticate edge node
        node_id = f"NODE-{self.org1_id}-01"
        secret = "super_secure_secret_phase24"
        cloud_db.register_edge_node(self.org1_id, self.branch1_id, node_name="Edge Station A", secret=secret, node_id=node_id)
        auth_res = cloud_db.authenticate_edge_node(node_id, secret)
        token = auth_res["token"]

        # B. Sync an event for Hamza Tariq
        sync_ev_id = f"EVT-SYNC-P24-{datetime.now().timestamp()}"
        batch_payload = {
            "batch_sequence": 9999,
            "events": [
                {
                    "event_id": sync_ev_id,
                    "event_type": "CHECK_IN",
                    "confidence": 0.99,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "payload": {
                        "user_id": 102,
                        "detected_name": "Hamza Tariq",
                        "location": "Server Room",
                        "device_id": "Camera #2",
                        "source": "auto"
                    }
                }
            ]
        }

        resp = self.client.post(
            "/api/cloud/sync/batch",
            data=json.dumps(batch_payload),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {token}"
            }
        )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json()["success"])

        # C. Query Attendance Report for Org 1: Hamza Tariq must now show PRESENT
        rep_resp = self.client.get(f"/api/cloud/reports/attendance?organization_id={self.org1_id}&branch_id={self.branch1_id}&date={self.today_str}")
        self.assertEqual(rep_resp.status_code, 200)
        rep_data = rep_resp.get_json()

        hamza_rec = next((r for r in rep_data["records"] if "Hamza" in r["name"]), None)
        self.assertIsNotNone(hamza_rec)
        self.assertIn(hamza_rec["status"], ("PRESENT", "CHECKED_IN"))

if __name__ == "__main__":
    unittest.main()
