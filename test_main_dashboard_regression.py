"""
Comprehensive Regression Test Suite for QuantEye Main Dashboard
Verifies:
1. Main Dashboard starts and initializes cleanly
2. Authentication works via /api/auth/login
3. Current profile via /api/auth/me
4. Organization creation works via /api/organizations (POST)
5. Branch creation works via /api/branches (POST)
6. Module selection / listing works via /api/modules
7. Licensing works via /api/licenses
8. Existing API routes still respond (e.g. /api/config, /api/organizations, etc.)
"""

import os
import sys
import json
import atexit
import tempfile
import unittest
import uuid
from unittest.mock import MagicMock, patch

_MAIN_DASHBOARD_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_MAIN_DASHBOARD_DIR)
sys.path.insert(0, _MAIN_DASHBOARD_DIR)

# The three projects use generic top-level module names (for example `app`,
# `database`, and `cloud_database`). When pytest collects their tests together,
# remove modules imported from sibling projects so these imports resolve here.
for _module_name, _module in list(sys.modules.items()):
    _module_file = getattr(_module, "__file__", None)
    if not _module_file:
        continue
    _module_path = os.path.normcase(os.path.abspath(_module_file))
    if not _module_path.startswith(os.path.normcase(_PROJECT_ROOT + os.sep)):
        continue
    if not _module_path.startswith(os.path.normcase(_MAIN_DASHBOARD_DIR + os.sep)):
        del sys.modules[_module_name]

# Keep regression fixtures and generated organizations out of the deployed database.
_TEST_DB_DIR = tempfile.TemporaryDirectory(prefix="quanteye-main-tests-")
_TEST_ENV_KEYS = (
    "QINTELLECT_MAIN_DB_PATH",
    "PLATFORM_ADMIN_EMAIL",
    "PLATFORM_ADMIN_PASSWORD",
    "ORGANIZATION_ADMIN_EMAIL",
    "ORGANIZATION_ADMIN_PASSWORD",
    "ORGANIZATION_ADMIN_ORG_ID",
)
_ORIGINAL_TEST_ENV = {key: os.environ.get(key) for key in _TEST_ENV_KEYS}
os.environ["QINTELLECT_MAIN_DB_PATH"] = os.path.join(_TEST_DB_DIR.name, "dashboard.db")
os.environ["PLATFORM_ADMIN_EMAIL"] = "admin@qintellect.com"
os.environ["PLATFORM_ADMIN_PASSWORD"] = "admin123"
os.environ["ORGANIZATION_ADMIN_EMAIL"] = "admin@forknknives.com"
os.environ["ORGANIZATION_ADMIN_PASSWORD"] = "fork123"
os.environ["ORGANIZATION_ADMIN_ORG_ID"] = "ORG-001"


def _cleanup_test_environment():
    for key, value in _ORIGINAL_TEST_ENV.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    _TEST_DB_DIR.cleanup()


atexit.register(_cleanup_test_environment)

import database as db

db.init_db()
db.create_organization({
    "id": "ORG-001",
    "name": "Regression Fixture Organization",
    "branches": [{"id": "BR-01", "name": "Main Branch"}],
})

from app import app, query_local_dashboard

class TestMainDashboardRegression(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app.testing = True
        cls.client = app.test_client()
        db.init_db()
        db.ensure_default_users()

    def test_01_system_config_route(self):
        """Verify /api/config returns valid endpoints."""
        res = self.client.get("/api/config")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertIn("client_dashboard_url", data)
        self.assertIn("main_dashboard_url", data)

    def test_client_portal_link_redirects_to_configured_client_service(self):
        org = db.get_organization_by_id("ORG-001")
        response = self.client.get(
            "/client/portal?organization_id=ORG-001&branch_id=BR-01"
        )
        self.assertEqual(response.status_code, 302)
        location = response.headers["Location"]
        self.assertIn("/c/", location)
        self.assertIn(org["portal_slug"], location)
        self.assertTrue(location.endswith("/"))

    def test_dashboard_uses_configured_client_portal_url(self):
        with patch("app.DEFAULT_CLIENT_URL", "https://client.example.test/"):
            response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn(
            b'let GLOBAL_CLIENT_URL = "https://client.example.test/";',
            response.data,
        )
        self.assertIn(b'clientPortalHref(org,branch)', response.data)

    @patch("app._load_branch_cameras", return_value=[{"id": "cam-1", "rtsp_url": "rtsp://camera/stream"}])
    @patch("app.db.get_branch_by_token_key")
    def test_branch_camera_endpoint_requires_and_uses_branch_key(self, get_branch_by_token_key, load_cameras):
        with self.client.session_transaction() as session:
            session.clear()

        denied = self.client.get("/api/branches/BR-01/cameras")
        self.assertEqual(denied.status_code, 401)

        get_branch_by_token_key.return_value = {
            "branch_id": "BR-01",
            "organization_id": "ORG-001",
            "organization_status": "Active",
            "branch_access": "Active",
        }
        response = self.client.get(
            "/api/branches/BR-01/cameras",
            headers={"X-Branch-Key": "QE-TEST-KEY"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["cameras"], load_cameras.return_value)
        load_cameras.assert_called_once_with("ORG-001", "BR-01")

    @patch("app.query_local_dashboard")
    @patch("app.db.get_branch_by_token_key")
    def test_edge_shifts_require_branch_key_and_relay_local(self, get_branch_by_token_key, relay):
        denied = self.client.get("/api/edge/shifts")
        self.assertEqual(denied.status_code, 401)
        get_branch_by_token_key.return_value = {
            "branch_id": "BR-01",
            "organization_id": "ORG-001",
            "organization_status": "Active",
            "branch_access": "Active",
        }
        relay.return_value = {
            "ok": True,
            "status_code": 200,
            "data": {
                "success": True,
                "organization_id": "ORG-001",
                "branch_id": "BR-01",
                "shifts": [{"id": 1, "name": "Day"}],
            },
        }
        response = self.client.get("/api/edge/shifts", headers={"X-Branch-Key": "QE-TEST-KEY"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["shifts"][0]["name"], "Day")
        self.assertIn("/api/integration/shifts?", relay.call_args.args[0])

    @patch("app.query_local_dashboard")
    @patch("app.db.get_branch_by_token_key")
    def test_branch_employee_endpoint_relays_scoped_portal_profiles(self, get_branch_by_token_key, relay):
        get_branch_by_token_key.return_value = {
            "branch_id": "BR-01",
            "organization_id": "ORG-001",
            "organization_status": "Active",
            "branch_access": "Active",
        }
        relay.return_value = {
            "ok": True,
            "status_code": 200,
            "data": {
                "success": True,
                "organization_id": "ORG-001",
                "branch_id": "BR-01",
                "employees": [{"id": 17, "name": "New Employee"}],
            },
        }

        denied = self.client.get("/api/branches/BR-01/employees")
        self.assertEqual(denied.status_code, 401)
        response = self.client.get(
            "/api/branches/BR-01/employees",
            headers={"X-Branch-Key": "QE-TEST-KEY"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["employees"], [{"id": 17, "name": "New Employee"}])
        relay.assert_called_once_with(
            "/api/integration/employees?organization_id=ORG-001&branch_id=BR-01"
        )

        relay.return_value = {"ok": False, "status_code": 503}
        unavailable = self.client.get(
            "/api/branches/BR-01/employees",
            headers={"X-Branch-Key": "QE-TEST-KEY"},
        )
        self.assertEqual(unavailable.status_code, 503)

    @patch("app.query_local_dashboard")
    @patch("app.db.get_branch_by_token_key")
    def test_branch_training_endpoint_relays_embeddings_with_service_auth(self, get_branch, relay):
        get_branch.return_value = {
            "branch_id": "BR-01",
            "organization_id": "ORG-001",
            "organization_status": "Active",
            "branch_access": "Active",
        }
        relay.return_value = {
            "ok": True,
            "status_code": 200,
            "data": {"success": True, "synced_count": 1, "is_trained": True},
        }
        endpoint = "/api/edge/branches/BR-01/employees/17/embeddings"
        payload = {"embeddings": [[0.5] * 512], "source": "node-test"}

        denied = self.client.post(endpoint, json=payload)
        self.assertEqual(denied.status_code, 401)
        response = self.client.post(
            endpoint,
            json=payload,
            headers={"X-Branch-Key": "QE-TEST-KEY"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["is_trained"])
        relay.assert_called_once_with(
            "/api/integration/employees/17/embeddings?organization_id=ORG-001&branch_id=BR-01",
            method="POST",
            payload=payload,
        )

    @patch("app.query_local_dashboard")
    @patch("app.db.get_branch_by_token_key")
    def test_branch_face_template_download_requires_branch_key_and_checks_branch_binding(self, get_branch, relay):
        get_branch.return_value = {
            "branch_id": "BR-01",
            "organization_id": "ORG-001",
            "organization_status": "Active",
            "branch_access": "Active",
        }
        relay.return_value = {
            "ok": True,
            "status_code": 200,
            "data": {
                "success": True,
                "organization_id": "ORG-001",
                "branch_id": "BR-01",
                "user_id": 17,
                "embeddings": [[0.5] * 512],
                "is_trained": True,
            },
        }
        endpoint = "/api/edge/branches/BR-01/employees/17/embeddings"

        denied = self.client.get(endpoint)
        self.assertEqual(denied.status_code, 401)
        response = self.client.get(
            endpoint,
            headers={"X-Branch-Key": "QE-TEST-KEY"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["embeddings"], [[0.5] * 512])
        relay.assert_called_once_with(
            "/api/integration/employees/17/embeddings?organization_id=ORG-001&branch_id=BR-01",
            method="GET",
        )

        relay.return_value["data"]["branch_id"] = "BR-OTHER"
        mismatched = self.client.get(
            endpoint,
            headers={"X-Branch-Key": "QE-TEST-KEY"},
        )
        self.assertEqual(mismatched.status_code, 502)

    def test_02_protected_routes_require_a_valid_session(self):
        """Unauthenticated requests and stale session IDs must not acquire admin access."""
        with self.client.session_transaction() as session:
            session.clear()

        page_res = self.client.get("/")
        self.assertEqual(page_res.status_code, 200)
        self.assertIn(b'<body class="auth-required">', page_res.data)
        self.assertIn(b'id="loginModal"', page_res.data)
        self.assertIn(b'Admin email configured for this service', page_res.data)
        self.assertNotIn(b'admin@qintellect.com</code>', page_res.data)
        self.assertNotIn(b'admin@forknknives.com</code>', page_res.data)

        protected_res = self.client.get("/api/organizations")
        self.assertEqual(protected_res.status_code, 401)
        profile_res = self.client.get("/api/auth/me")
        self.assertEqual(profile_res.status_code, 401)

        with self.client.session_transaction() as session:
            session["user_id"] = "missing-user-id"
        stale_session_res = self.client.get("/api/organizations")
        self.assertEqual(stale_session_res.status_code, 401)

    def test_03_auth_login_platform_admin(self):
        """Verify /api/auth/login works for Platform Admin."""
        res = self.client.post("/api/auth/login", json={
            "email": "admin@qintellect.com",
            "password": "admin123"
        })
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data.get("success"))
        self.assertEqual(data["user"]["role"], "platform_admin")
        page_res = self.client.get("/")
        self.assertNotIn(b'<body class="auth-required">', page_res.data)

    def test_04_auth_login_org_admin(self):
        """Verify /api/auth/login works for Org Admin."""
        res = self.client.post("/api/auth/login", json={
            "email": "admin@forknknives.com",
            "password": "fork123"
        })
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data.get("success"))
        self.assertEqual(data["user"]["role"], "organization_admin")
        self.assertEqual(data["user"]["organization_id"], "ORG-001")

    def test_build_creation_returns_validation_errors_and_saves_valid_builds(self):
        login = self.client.post("/api/auth/login", json={
            "email": "admin@qintellect.com",
            "password": "admin123",
        })
        self.assertEqual(login.status_code, 200)
        build_id = f"BUILD-REGRESSION-{uuid.uuid4().hex[:8]}"
        payload = {
            "id": build_id,
            "organizationId": "ORG-001",
            "organizationName": "Regression Fixture Organization",
            "branchId": "BR-01",
            "branchName": "Main Branch",
            "appName": "Regression Build",
            "clientUsername": "build@regression.example",
            "clientPassword": "ValidBuild!Password2026",
            "modules": ["attendance"],
        }
        try:
            invalid = self.client.post(
                "/api/builds",
                json=dict(payload, clientPassword="Pass1234!"),
            )
            self.assertEqual(invalid.status_code, 400)
            self.assertIn("14 characters", invalid.get_json()["error"])
            self.assertFalse(any(build["id"] == build_id for build in db.get_all_builds()))

            created = self.client.post("/api/builds", json=payload)
            self.assertEqual(created.status_code, 201, created.get_json())
            self.assertEqual(created.get_json()["id"], build_id)
        finally:
            db.delete_build(build_id)

    def test_delete_organization_removes_associated_build_history(self):
        org_id = f"ORG-DELETE-{uuid.uuid4().hex[:8]}"
        org = db.create_organization({
            "id": org_id,
            "name": f"Delete Regression {uuid.uuid4().hex[:8]}",
            "branches": [{"id": "BR-DELETE", "name": "Delete Test Branch"}],
        })
        build_id = f"BUILD-DELETE-{uuid.uuid4().hex[:8]}"
        with db.get_connection() as conn:
            conn.execute(
                """INSERT INTO application_builds (
                       id, organization_id, organization_name, branch_id, branch_name,
                       app_name, version, modules_snapshot
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    build_id,
                    org_id,
                    org["name"],
                    "BR-DELETE",
                    "Delete Test Branch",
                    "Delete Test App",
                    "v1.0.1",
                    "[]",
                ),
            )
        login = self.client.post("/api/auth/login", json={
            "email": "admin@qintellect.com",
            "password": "admin123",
        })
        self.assertEqual(login.status_code, 200)

        deleted = self.client.delete(f"/api/organizations/{org_id}")

        self.assertEqual(deleted.status_code, 200, deleted.get_json())
        self.assertIsNone(db.get_organization_by_id(org_id))
        self.assertFalse(any(build["id"] == build_id for build in db.get_all_builds()))

    def test_bootstrap_uses_unique_id_when_configured_email_changes(self):
        """An old bootstrap ID must not crash startup for a newly configured admin email."""
        platform_admin = db.get_user_by_id("USR-PLATFORM-ADMIN")
        self.assertIsNotNone(platform_admin)
        configured_email = "new-admin@example.test"
        with db.get_connection() as conn:
            conn.execute(
                "UPDATE control_plane_users SET email = ? WHERE id = ?",
                ("previous-admin@example.test", "USR-PLATFORM-ADMIN"),
            )
        try:
            with patch.dict(os.environ, {
                "PLATFORM_ADMIN_EMAIL": configured_email,
                "PLATFORM_ADMIN_PASSWORD": "strong-test-password",
                "ORGANIZATION_ADMIN_EMAIL": "",
                "ORGANIZATION_ADMIN_PASSWORD": "",
                "ORGANIZATION_ADMIN_ORG_ID": "",
            }):
                db.ensure_default_users()

            new_admin = db.get_user_by_email(configured_email)
            self.assertIsNotNone(new_admin)
            self.assertNotEqual(new_admin["id"], "USR-PLATFORM-ADMIN")
            self.assertEqual(
                db.get_user_by_id("USR-PLATFORM-ADMIN")["email"],
                "previous-admin@example.test",
            )
        finally:
            with db.get_connection() as conn:
                conn.execute("DELETE FROM control_plane_users WHERE email = ?", (configured_email,))
                conn.execute(
                    "UPDATE control_plane_users SET email = ? WHERE id = ?",
                    (platform_admin["email"], "USR-PLATFORM-ADMIN"),
                )

    def test_05_client_auth_uses_hashed_build_credentials_and_branch_binding(self):
        """Only the provisioned build password and its matching branch may authenticate."""
        build_password = "RegressionBuild!Password-2026"
        db.create_build({
            "id": "BUILD-AUTH-REGRESSION",
            "organizationId": "ORG-001",
            "organizationName": "Regression Fixture Organization",
            "branchId": "BR-01",
            "branchName": "Main Branch",
            "clientUsername": "node@regression.example",
            "clientPassword": build_password,
            "modules": ["attendance"],
        })
        org = db.get_organization_by_id("ORG-001")
        branch_key = org["branches"][0]["tokenKey"]
        other_org = db.create_organization({
            "id": "ORG-002",
            "name": "Second Regression Organization",
            "branches": [{"id": "BR-02", "name": "Second Branch"}],
        })
        other_branch_key = other_org["branches"][0]["tokenKey"]

        valid_res = self.client.post("/api/client/authenticate", json={
            "username": "node@regression.example",
            "password": build_password,
        })
        self.assertEqual(valid_res.status_code, 200)
        self.assertEqual(valid_res.get_json()["branch_id"], "BR-01")

        wrong_password_res = self.client.post("/api/client/authenticate", json={
            "username": "node@regression.example",
            "password": "wrong-password",
        })
        self.assertEqual(wrong_password_res.status_code, 401)

        mismatched_key_res = self.client.post("/api/client/authenticate", json={
            "username": "node@regression.example",
            "password": build_password,
            "branch_key": other_branch_key,
        })
        self.assertEqual(mismatched_key_res.status_code, 401)

        valid_key_res = self.client.post("/api/client/authenticate", json={
            "branch_key": branch_key,
        })
        self.assertEqual(valid_key_res.status_code, 200)
        self.assertEqual(valid_key_res.get_json()["branch_id"], "BR-01")

        with db.get_connection() as conn:
            conn.execute(
                "UPDATE branches SET access = ? WHERE organization_id = ? AND id = ?",
                ("Suspended", "ORG-001", "BR-01"),
            )
        try:
            inactive_branch_res = self.client.post(
                "/api/client/authenticate",
                json={"branch_key": branch_key},
            )
            self.assertEqual(inactive_branch_res.status_code, 403)
        finally:
            with db.get_connection() as conn:
                conn.execute(
                    "UPDATE branches SET access = ? WHERE organization_id = ? AND id = ?",
                    ("Active", "ORG-001", "BR-01"),
                )

        legacy_fallback_res = self.client.post("/api/client/authenticate", json={
            "username": "Regression Fixture Organization",
            "password": "admin123",
        })
        self.assertEqual(legacy_fallback_res.status_code, 401)

    def test_client_authenticate_returns_live_org_modules_and_portal_url(self):
        live_modules = ["attendance", "shifts", "payroll"]
        org = db.create_organization({
            "name": "Live Modules Org",
            "modules": live_modules,
            "branches": [{"id": "BR-LIVE-01", "name": "Live Branch"}],
        })
        build_password = "LiveModules-Build-Pass-2026"
        db.create_build({
            "id": "BUILD-LIVE-MODULES",
            "organizationId": org["id"],
            "organizationName": org["name"],
            "branchId": org["branches"][0]["id"],
            "branchName": org["branches"][0]["name"],
            "clientUsername": "live-modules@example.test",
            "clientPassword": build_password,
            "modules": ["attendance"],
        })
        res = self.client.post("/api/client/authenticate", json={
            "username": "live-modules@example.test",
            "password": build_password,
        })
        self.assertEqual(res.status_code, 200)
        payload = res.get_json()
        self.assertEqual(sorted(payload["modules"]), sorted(live_modules))
        self.assertEqual(payload["build_modules"], ["attendance"])
        self.assertTrue(payload["client_portal_url"].endswith("/"))
        self.assertNotIn("main-dashboard", payload["client_portal_url"])
        self.assertIn("/c/", payload["client_portal_url"])
        self.assertEqual(payload["portal_slug"], db.get_organization_by_id(org["id"])["portal_slug"])

        wrong_slug = self.client.post("/api/client/authenticate", json={
            "username": "live-modules@example.test",
            "password": build_password,
            "portal_slug": "someone-else-org",
        })
        self.assertEqual(wrong_slug.status_code, 403)

    def test_handover_page_labels_portal_not_main(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"function resolveClientPortalUrl", response.data)
        self.assertIn(b"not Main Dashboard", response.data)
        self.assertIn(b"EXE BRANCH KEY", response.data)

    def test_attendance_relay_requires_branch_key_and_is_idempotent(self):
        organization = db.get_organization_by_id("ORG-001")
        branch_key = organization["branches"][0]["tokenKey"]
        event_id = str(uuid.uuid4())
        payload = {
            "event_id": event_id,
            "organization_id": "ORG-OTHER",
            "branch_id": "BR-OTHER",
            "user_id": 101,
            "name": "Regression Employee",
            "timestamp": "2026-10-10T12:00:00",
            "bypass_shift_check": True,
        }

        unauthenticated = self.client.post("/api/sync/attendance-event", json=payload)
        self.assertEqual(unauthenticated.status_code, 401)
        forged = self.client.post(
            "/api/sync/attendance-event",
            json=payload,
            headers={"X-Branch-Key": "not-a-branch-key"},
        )
        self.assertEqual(forged.status_code, 401)

        with patch("app.cloud_db", None), patch(
            "app.query_local_dashboard",
            return_value={"ok": True, "status_code": 201, "data": {"success": True}},
        ) as relay:
            headers = {"X-Branch-Key": branch_key}
            accepted = self.client.post(
                "/api/sync/attendance-event", json=payload, headers=headers
            )
            self.assertEqual(accepted.status_code, 201)
            self.assertEqual(
                relay.call_args.kwargs["payload"]["organization_id"], "ORG-001"
            )
            self.assertEqual(
                relay.call_args.kwargs["payload"]["branch_id"], "BR-01"
            )
            self.assertNotIn(
                "bypass_shift_check", relay.call_args.kwargs["payload"]
            )

            replay = self.client.post(
                "/api/sync/attendance-event", json=payload, headers=headers
            )
            self.assertEqual(replay.status_code, 200)
            self.assertTrue(replay.get_json()["duplicate"])
            self.assertEqual(relay.call_count, 2)

            changed = dict(payload, name="Changed Employee")
            conflict = self.client.post(
                "/api/sync/attendance-event", json=changed, headers=headers
            )
            self.assertEqual(conflict.status_code, 409)

    def test_edge_sync_state_requires_branch_key_and_returns_bound_local_state(self):
        organization = db.get_organization_by_id("ORG-001")
        branch_key = organization["branches"][0]["tokenKey"]

        self.assertEqual(
            self.client.get("/api/edge/sync-state").status_code,
            401,
        )
        self.assertEqual(
            self.client.get(
                "/api/edge/sync-state",
                headers={"X-Branch-Key": "invalid-branch-key"},
            ).status_code,
            401,
        )

        with patch(
            "app.query_local_dashboard",
            return_value={
                "ok": True,
                "status_code": 200,
                "data": {
                    "success": True,
                    "organization_id": "ORG-001",
                    "branch_id": "BR-01",
                    "date": "2026-10-14",
                    "manual_absent_user_ids": [101],
                },
            },
        ) as relay:
            response = self.client.get(
                "/api/edge/sync-state",
                headers={"X-Branch-Key": branch_key},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["manual_absent_user_ids"], [101])
        relay.assert_called_once_with(
            "/api/integration/edge-sync-state?organization_id=ORG-001&branch_id=BR-01",
            method="GET",
        )

    def test_06_module_catalog_listing(self):
        """Verify /api/modules returns canonical modules."""
        # Login first
        self.client.post("/api/auth/login", json={
            "email": "admin@qintellect.com",
            "password": "admin123"
        })
        res = self.client.get("/api/modules")
        self.assertEqual(res.status_code, 200)
        modules = res.get_json()
        self.assertIsInstance(modules, list)
        self.assertGreaterEqual(len(modules), 5)
        mod_ids = [m["id"] for m in modules]
        self.assertIn("attendance", mod_ids)
        self.assertIn("shifts", mod_ids)
        self.assertIn("payroll", mod_ids)

    def test_07_organization_creation(self):
        """Verify organization creation via /api/organizations (POST)."""
        self.client.post("/api/auth/login", json={
            "email": "admin@qintellect.com",
            "password": "admin123"
        })
        new_org_payload = {
            "name": f"Regression Testing Inc {os.urandom(3).hex()}",
            "industry": "Artificial Intelligence",
            "email": "regression@test.com",
            "phone": "+92 300 9999999",
            "address": "Islamabad High Tech Park",
            "modules": ["attendance", "shifts", "payroll"],
            "branches": [
                {
                    "id": "BR-REG-01",
                    "name": "North Branch",
                    "city": "Islamabad",
                    "address": "Sector F-7",
                    "employeeCount": 25
                }
            ]
        }
        res = self.client.post("/api/organizations", json=new_org_payload)
        self.assertIn(res.status_code, (200, 201))
        created_org = res.get_json()
        self.assertIsNotNone(created_org.get("id"))
        self.assertEqual(created_org.get("name"), new_org_payload["name"])
        self.assertRegex(created_org["branches"][0]["tokenKey"], r"^QE-[A-F0-9]{64}$")

        # Fetch organizations list
        get_res = self.client.get("/api/organizations")
        self.assertEqual(get_res.status_code, 200)
        orgs = get_res.get_json()
        matching = [o for o in orgs if o["id"] == created_org["id"]]
        self.assertEqual(len(matching), 1)

    def test_08_branch_and_licensing_verification(self):
        """Verify organization license and branches are accessible."""
        self.client.post("/api/auth/login", json={
            "email": "admin@qintellect.com",
            "password": "admin123"
        })
        # Check ORG-001
        res = self.client.get("/api/organizations/ORG-001")
        self.assertEqual(res.status_code, 200)
        org_data = res.get_json()
        self.assertIsNotNone(org_data)
        self.assertIn("branches", org_data)
        self.assertIn("tokenKey", org_data)

    def test_09_dashboard_html_view(self):
        """Verify dashboard index page loads correctly."""
        res = self.client.get("/")
        self.assertEqual(res.status_code, 200)
        self.assertIn(b"QuantEye", res.data)

    def test_10_local_dashboard_sync_uses_shared_integration_key(self):
        """Configuration sync fails closed without the key and sends it when configured."""
        with patch.dict(os.environ, {"LOCAL_DASHBOARD_API_KEY": ""}):
            missing_key = query_local_dashboard(
                "/api/integration/sync",
                method="POST",
                payload={"organization_id": "ORG-001", "branch_id": "BR-01"},
            )
        self.assertFalse(missing_key["ok"])
        self.assertEqual(missing_key["status_code"], 503)

        shared_key = "MainLocal-Integration-Test-Key-2026-Secure"
        response = MagicMock()
        response.__enter__.return_value = response
        response.status = 200
        response.read.return_value = b'{"success": true}'
        with patch.dict(os.environ, {"LOCAL_DASHBOARD_API_KEY": shared_key}):
            with patch("urllib.request.urlopen", return_value=response) as urlopen:
                result = query_local_dashboard(
                    "/api/integration/sync",
                    method="POST",
                    payload={"organization_id": "ORG-001", "branch_id": "BR-01"},
                )
        self.assertTrue(result["ok"])
        sent_request = urlopen.call_args.args[0]
        self.assertEqual(sent_request.headers.get("X-integration-key"), shared_key)

    def test_local_dashboard_camera_proxy_uses_integration_key_with_query_string(self):
        shared_key = "MainLocal-Integration-Test-Key-2026-Secure"
        response = MagicMock()
        response.__enter__.return_value = response
        response.status = 200
        response.read.return_value = b'{"success": true, "cameras": []}'
        with patch.dict(os.environ, {"LOCAL_DASHBOARD_API_KEY": shared_key}):
            with patch("urllib.request.urlopen", return_value=response) as urlopen:
                result = query_local_dashboard(
                    "/api/integration/cameras?organization_id=ORG-001&branch_id=BR-01",
                )
        self.assertTrue(result["ok"])
        sent_request = urlopen.call_args.args[0]
        self.assertEqual(sent_request.headers.get("X-integration-key"), shared_key)

if __name__ == "__main__":
    unittest.main()
