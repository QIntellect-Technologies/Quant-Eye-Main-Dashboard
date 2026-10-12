"""
Phase 2.1 & Phase 2.2 Security & Tenant Isolation Automated Test Suite
Tests:
1. Direct PostgreSQL Row-Level Security (RLS) enforcement under app_runtime_role (NOBYPASSRLS)
2. Cross-tenant READ attacks (blocked)
3. Cross-tenant UPDATE attacks (blocked)
4. Cross-tenant DELETE attacks (blocked)
5. Cross-tenant INSERT attacks with tenant ID injection (blocked by WITH CHECK policy)
6. Branch Manager scope restriction attacks (blocked)
7. Platform Admin controlled cross-tenant operations (allowed)
8. Inactive/deactivated user access rejection (blocked)
9. Tampered frontend organization_id injection (blocked)
"""

import os
import sys
import unittest
import psycopg2
from psycopg2.extras import RealDictCursor
from datetime import datetime, timezone

# Ensure project import path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from cloud_database import (
    get_raw_connection,
    init_cloud_db,
    tenant_context,
    CloudUser,
    create_cloud_user,
    authenticate_cloud_user,
    create_organization,
    create_branch,
    create_employee,
    record_attendance_event,
    get_organization,
    get_employees,
    get_attendance_events,
    APP_RUNTIME_ROLE
)

class TestPhase2TenantIsolationRLS(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        """Initialize database schema, tables, roles, and RLS policies."""
        print("\n=======================================================")
        print("SETTING UP PHASE 2 POSTGRESQL MULTI-TENANT TEST SUITE")
        print("=======================================================")
        init_cloud_db()

        # Seed Test Tenants: ORG-ALPHA and ORG-BETA
        cls.org_a_id = "ORG-TEST-ALPHA"
        cls.org_b_id = "ORG-TEST-BETA"

        # Superuser context to populate test baseline
        with tenant_context(is_platform_admin=True, enforce_rls=False) as cur:
            # Clean up old test data if present
            cur.execute("DELETE FROM organizations WHERE id IN (%s, %s);", (cls.org_a_id, cls.org_b_id))

            # 1. Organizations
            cur.execute("""
                INSERT INTO organizations (id, name, industry, email, status)
                VALUES (%s, 'Alpha Corp', 'AI Systems', 'admin@alpha.io', 'Active'),
                       (%s, 'Beta Industries', 'Robotics', 'admin@beta.io', 'Active');
            """, (cls.org_a_id, cls.org_b_id))

            # 2. Branches
            cur.execute("""
                INSERT INTO branches (id, organization_id, name, city, access)
                VALUES ('BR-A-01', %s, 'Alpha Main HQ', 'New York', 'Active'),
                       ('BR-A-02', %s, 'Alpha Secondary Lab', 'Boston', 'Active'),
                       ('BR-B-01', %s, 'Beta Main HQ', 'Chicago', 'Active');
            """, (cls.org_a_id, cls.org_a_id, cls.org_b_id))

            # 3. Employees
            cur.execute("""
                INSERT INTO employees (id, organization_id, branch_id, employee_code, name, email)
                VALUES ('EMP-A-1', %s, 'BR-A-01', 'AL-101', 'Alice Alpha', 'alice@alpha.io'),
                       ('EMP-A-2', %s, 'BR-A-02', 'AL-102', 'Aaron Alpha', 'aaron@alpha.io'),
                       ('EMP-B-1', %s, 'BR-B-01', 'BE-201', 'Bob Beta', 'bob@beta.io');
            """, (cls.org_a_id, cls.org_a_id, cls.org_b_id))

            # 4. Attendance Events
            cur.execute("""
                INSERT INTO attendance_events (id, organization_id, branch_id, employee_id, event_type, confidence, timestamp)
                VALUES ('ATT-A-1', %s, 'BR-A-01', 'EMP-A-1', 'CHECK_IN', 0.98, CURRENT_TIMESTAMP),
                       ('ATT-B-1', %s, 'BR-B-01', 'EMP-B-1', 'CHECK_IN', 0.99, CURRENT_TIMESTAMP);
            """, (cls.org_a_id, cls.org_b_id))

        # Create Cloud Users
        cls.user_a_admin = CloudUser(
            user_id="USR-A-ADM",
            organization_id=cls.org_a_id,
            role="org_admin",
            username="alpha_admin",
            email="admin@alpha.io",
            is_active=True
        )
        cls.user_a_mgr_br1 = CloudUser(
            user_id="USR-A-MGR1",
            organization_id=cls.org_a_id,
            role="branch_manager",
            username="alpha_mgr_1",
            email="mgr1@alpha.io",
            is_active=True,
            permitted_branches=["BR-A-01"]
        )
        cls.user_b_admin = CloudUser(
            user_id="USR-B-ADM",
            organization_id=cls.org_b_id,
            role="org_admin",
            username="beta_admin",
            email="admin@beta.io",
            is_active=True
        )
        cls.user_inactive = CloudUser(
            user_id="USR-A-INACT",
            organization_id=cls.org_a_id,
            role="org_admin",
            username="alpha_inactive",
            email="inactive@alpha.io",
            is_active=False
        )
        cls.user_plat_admin = CloudUser(
            user_id="USR-PLAT-ROOT",
            organization_id="PLATFORM",
            role="platform_admin",
            username="super_admin",
            email="root@qintellect.com",
            is_active=True
        )

    # =========================================================================
    # 1. ORG-A LEGITIMATE OPERATIONS
    # =========================================================================
    def test_01_org_a_legitimate_reads(self):
        """Test Org-A can read its own organization, branches, employees, and attendance."""
        with tenant_context(user=self.user_a_admin) as cur:
            # Own Org
            cur.execute("SELECT * FROM organizations WHERE id = %s;", (self.org_a_id,))
            org = cur.fetchone()
            self.assertIsNotNone(org)
            self.assertEqual(org["id"], self.org_a_id)

            # Own Branches
            cur.execute("SELECT * FROM branches;")
            branches = cur.fetchall()
            self.assertEqual(len(branches), 2)
            self.assertTrue(all(b["organization_id"] == self.org_a_id for b in branches))

            # Own Employees
            cur.execute("SELECT * FROM employees;")
            employees = cur.fetchall()
            self.assertEqual(len(employees), 2)
            self.assertTrue(all(e["organization_id"] == self.org_a_id for e in employees))

            # Own Attendance
            cur.execute("SELECT * FROM attendance_events;")
            events = cur.fetchall()
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["organization_id"], self.org_a_id)
        print("  [PASS] Test 1: Org-A legitimate reads succeeded.")

    # =========================================================================
    # 2. RAW POSTGRESQL RLS DIRECT SELECT ATTACK (app_runtime_role)
    # =========================================================================
    def test_02_direct_pg_rls_cross_tenant_select(self):
        """Direct DB attack: app_runtime_role attempts SELECT on Org-B rows."""
        raw_conn = get_raw_connection()
        try:
            with raw_conn:
                with raw_conn.cursor(cursor_factory=RealDictCursor) as cur:
                    # Switch to unprivileged runtime role and set context for Org-A
                    cur.execute(f"SET ROLE {APP_RUNTIME_ROLE};")
                    cur.execute("SELECT set_config('app.current_org_id', %s, true);", (self.org_a_id,))
                    cur.execute("SELECT set_config('app.current_branch_id', '', true);")
                    cur.execute("SELECT set_config('app.is_platform_admin', 'false', true);")

                    # Attack: Query Org-B directly
                    cur.execute("SELECT * FROM organizations WHERE id = %s;", (self.org_b_id,))
                    row = cur.fetchone()
                    self.assertIsNone(row, "RLS FAILURE: Org-A was able to SELECT Org-B row!")

                    # Attack: Query Org-B employees directly
                    cur.execute("SELECT * FROM employees WHERE organization_id = %s;", (self.org_b_id,))
                    emps = cur.fetchall()
                    self.assertEqual(len(emps), 0, "RLS FAILURE: Org-A was able to view Org-B employees!")

                    # Attack: Query Org-B attendance directly
                    cur.execute("SELECT * FROM attendance_events WHERE organization_id = %s;", (self.org_b_id,))
                    atts = cur.fetchall()
                    self.assertEqual(len(atts), 0, "RLS FAILURE: Org-A was able to view Org-B attendance!")

                    # Attack: SELECT * without WHERE filter
                    cur.execute("SELECT organization_id FROM employees;")
                    all_visible = cur.fetchall()
                    for r in all_visible:
                        self.assertEqual(r["organization_id"], self.org_a_id, "RLS FAILURE: Leaked foreign tenant row!")
        finally:
            raw_conn.close()
        print("  [PASS] Test 2: Direct PostgreSQL RLS cross-tenant SELECT completely blocked.")

    # =========================================================================
    # 3. RAW POSTGRESQL RLS DIRECT UPDATE ATTACK (app_runtime_role)
    # =========================================================================
    def test_03_direct_pg_rls_cross_tenant_update(self):
        """Direct DB attack: app_runtime_role attempts UPDATE on Org-B rows."""
        raw_conn = get_raw_connection()
        try:
            with raw_conn:
                with raw_conn.cursor() as cur:
                    cur.execute(f"SET ROLE {APP_RUNTIME_ROLE};")
                    cur.execute("SELECT set_config('app.current_org_id', %s, true);", (self.org_a_id,))
                    cur.execute("SELECT set_config('app.current_branch_id', '', true);")
                    cur.execute("SELECT set_config('app.is_platform_admin', 'false', true);")

                    # Attack: Try updating Org-B organization name
                    cur.execute("UPDATE organizations SET name = 'MALICIOUS_HACK' WHERE id = %s;", (self.org_b_id,))
                    self.assertEqual(cur.rowcount, 0, "RLS FAILURE: Cross-tenant UPDATE succeeded!")

                    # Attack: Try updating Org-B employee
                    cur.execute("UPDATE employees SET name = 'HACKED_EMP' WHERE organization_id = %s;", (self.org_b_id,))
                    self.assertEqual(cur.rowcount, 0, "RLS FAILURE: Cross-tenant employee UPDATE succeeded!")
        finally:
            raw_conn.close()

        # Verify database state was NOT modified
        with tenant_context(is_platform_admin=True, enforce_rls=False) as cur:
            cur.execute("SELECT name FROM organizations WHERE id = %s;", (self.org_b_id,))
            self.assertEqual(cur.fetchone()["name"], "Beta Industries")
        print("  [PASS] Test 3: Direct PostgreSQL RLS cross-tenant UPDATE blocked (0 rows affected).")

    # =========================================================================
    # 4. RAW POSTGRESQL RLS DIRECT DELETE ATTACK (app_runtime_role)
    # =========================================================================
    def test_04_direct_pg_rls_cross_tenant_delete(self):
        """Direct DB attack: app_runtime_role attempts DELETE on Org-B rows."""
        raw_conn = get_raw_connection()
        try:
            with raw_conn:
                with raw_conn.cursor() as cur:
                    cur.execute(f"SET ROLE {APP_RUNTIME_ROLE};")
                    cur.execute("SELECT set_config('app.current_org_id', %s, true);", (self.org_a_id,))
                    cur.execute("SELECT set_config('app.current_branch_id', '', true);")
                    cur.execute("SELECT set_config('app.is_platform_admin', 'false', true);")

                    # Attack: Try deleting Org-B
                    cur.execute("DELETE FROM organizations WHERE id = %s;", (self.org_b_id,))
                    self.assertEqual(cur.rowcount, 0, "RLS FAILURE: Cross-tenant DELETE succeeded!")

                    # Attack: Try deleting Org-B attendance
                    cur.execute("DELETE FROM attendance_events WHERE organization_id = %s;", (self.org_b_id,))
                    self.assertEqual(cur.rowcount, 0, "RLS FAILURE: Cross-tenant attendance DELETE succeeded!")
        finally:
            raw_conn.close()

        # Verify Org-B is intact
        with tenant_context(is_platform_admin=True, enforce_rls=False) as cur:
            cur.execute("SELECT COUNT(*) as cnt FROM organizations WHERE id = %s;", (self.org_b_id,))
            self.assertEqual(cur.fetchone()["cnt"], 1)
        print("  [PASS] Test 4: Direct PostgreSQL RLS cross-tenant DELETE blocked (0 rows affected).")

    # =========================================================================
    # 5. RAW POSTGRESQL RLS DIRECT INSERT / INJECTION ATTACK (WITH CHECK)
    # =========================================================================
    def test_05_direct_pg_rls_cross_tenant_insert_injection(self):
        """Direct DB attack: Attempt INSERT injecting foreign tenant ID (blocked by WITH CHECK policy)."""
        raw_conn = get_raw_connection()
        try:
            with raw_conn:
                with raw_conn.cursor() as cur:
                    cur.execute(f"SET ROLE {APP_RUNTIME_ROLE};")
                    cur.execute("SELECT set_config('app.current_org_id', %s, true);", (self.org_a_id,))
                    cur.execute("SELECT set_config('app.current_branch_id', '', true);")
                    cur.execute("SELECT set_config('app.is_platform_admin', 'false', true);")

                    # Attack: Insert employee into Org-B while logged in as Org-A
                    with self.assertRaises(psycopg2.errors.InsufficientPrivilege, msg="RLS WITH CHECK failed to reject injected tenant ID!"):
                        cur.execute("""
                            INSERT INTO employees (id, organization_id, branch_id, employee_code, name)
                            VALUES ('EMP-INJECTED', %s, 'BR-B-01', 'MAL-99', 'Malicious Injected Employee');
                        """, (self.org_b_id,))
        finally:
            raw_conn.close()
        print("  [PASS] Test 5: Cross-tenant INSERT injection blocked by RLS WITH CHECK policy.")

    # =========================================================================
    # 6. BRANCH MANAGER RESTRICTION & BRANCH ID MANIPULATION ATTACK
    # =========================================================================
    def test_06_branch_manager_scope_enforcement(self):
        """Branch manager of Branch 1 cannot access Branch 2 data."""
        with tenant_context(user=self.user_a_mgr_br1) as cur:
            # Can see Branch 1 employees
            cur.execute("SELECT * FROM employees;")
            emps = cur.fetchall()
            self.assertEqual(len(emps), 1)
            self.assertEqual(emps[0]["branch_id"], "BR-A-01")

            # Cannot see Branch 2 employees
            cur.execute("SELECT * FROM employees WHERE branch_id = 'BR-A-02';")
            emp2 = cur.fetchall()
            self.assertEqual(len(emp2), 0)

        # Attempt to forge branch_id in tenant context call
        with self.assertRaises(PermissionError, msg="Branch manager was allowed unauthorized branch scope!"):
            with tenant_context(user=self.user_a_mgr_br1, branch_id="BR-A-02") as cur:
                cur.execute("SELECT * FROM employees;")
        print("  [PASS] Test 6: Branch Manager scope enforced; branch ID forgery rejected.")

    # =========================================================================
    # 7. PLATFORM ADMIN CONTROLLED CROSS-TENANT ACCESS
    # =========================================================================
    def test_07_platform_admin_access(self):
        """Platform Admin can oversee all organizations."""
        with tenant_context(user=self.user_plat_admin) as cur:
            cur.execute("SELECT id FROM organizations WHERE id IN (%s, %s);", (self.org_a_id, self.org_b_id))
            orgs = cur.fetchall()
            self.assertEqual(len(orgs), 2)
        print("  [PASS] Test 7: Platform Admin controlled cross-tenant access verified.")

    # =========================================================================
    # 8. INACTIVE / DEACTIVATED USER ACCESS REJECTION
    # =========================================================================
    def test_08_inactive_user_rejection(self):
        """Deactivated users must be rejected before executing queries."""
        with self.assertRaises(PermissionError, msg="Inactive user was granted access!"):
            with tenant_context(user=self.user_inactive) as cur:
                cur.execute("SELECT * FROM organizations;")
        print("  [PASS] Test 8: Deactivated user access rejected.")

    # =========================================================================
    # 9. FRONTEND ORG_ID PAYLOAD INJECTION REJECTION
    # =========================================================================
    def test_09_frontend_org_id_payload_tampering(self):
        """Never trust organization_id supplied by frontend: tenant context must override with user org."""
        # Org-A user tries to pass org_id = 'ORG-TEST-BETA' to tenant_context
        with tenant_context(user=self.user_a_admin, org_id=self.org_b_id) as cur:
            # Query organizations
            cur.execute("SELECT id FROM organizations;")
            rows = cur.fetchall()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["id"], self.org_a_id, "Payload injection overrode user token organization!")
        print("  [PASS] Test 9: Frontend org_id parameter tampering neutralized.")

def run_tests():
    suite = unittest.TestLoader().loadTestsFromTestCase(TestPhase2TenantIsolationRLS)
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    return result.wasSuccessful()

if __name__ == "__main__":
    success = run_tests()
    sys.exit(0 if success else 1)
