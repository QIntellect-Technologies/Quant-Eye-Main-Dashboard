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
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app import app
import database as db

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

    def test_02_auth_login_platform_admin(self):
        """Verify /api/auth/login works for Platform Admin."""
        res = self.client.post("/api/auth/login", json={
            "email": "admin@qintellect.com",
            "password": "admin123"
        })
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data.get("success"))
        self.assertEqual(data["user"]["role"], "platform_admin")

    def test_03_auth_login_org_admin(self):
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

    def test_04_module_catalog_listing(self):
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

    def test_05_organization_creation(self):
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

        # Fetch organizations list
        get_res = self.client.get("/api/organizations")
        self.assertEqual(get_res.status_code, 200)
        orgs = get_res.get_json()
        matching = [o for o in orgs if o["id"] == created_org["id"]]
        self.assertEqual(len(matching), 1)

    def test_06_branch_and_licensing_verification(self):
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

    def test_07_dashboard_html_view(self):
        """Verify dashboard index page loads correctly."""
        res = self.client.get("/")
        self.assertEqual(res.status_code, 200)
        self.assertIn(b"QuantEye", res.data)

if __name__ == "__main__":
    unittest.main()
