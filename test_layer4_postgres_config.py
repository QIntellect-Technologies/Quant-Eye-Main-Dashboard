"""Layer 4: PostgreSQL is opt-in via DATABASE_URL / PGHOST. SQLite stays until cutover."""

import os
import unittest
from unittest.mock import patch

import cloud_database as cloud_db
from tenant_urls import company_portal_url, make_portal_slug


class Layer4PostgresConfigTests(unittest.TestCase):
    def test_unconfigured_runtime_does_not_use_localhost(self):
        with patch.dict(os.environ, {"DATABASE_URL": "", "POSTGRES_URL": "", "PGHOST": ""}, clear=False):
            os.environ.pop("DATABASE_URL", None)
            os.environ.pop("POSTGRES_URL", None)
            os.environ.pop("PGHOST", None)
            os.environ.pop("RAILWAY_ENVIRONMENT", None)
            self.assertFalse(cloud_db.postgres_is_configured())

    def test_production_rejects_localhost_pghost(self):
        with patch.dict(
            os.environ,
            {"RAILWAY_ENVIRONMENT": "production", "PGHOST": "127.0.0.1", "DATABASE_URL": ""},
            clear=False,
        ):
            os.environ.pop("DATABASE_URL", None)
            os.environ.pop("POSTGRES_URL", None)
            self.assertFalse(cloud_db.postgres_is_configured())

    def test_database_url_enables_postgres(self):
        with patch.dict(
            os.environ,
            {"DATABASE_URL": "postgresql://user:pass@cloud.example:5432/quanteye"},
            clear=False,
        ):
            self.assertTrue(cloud_db.postgres_is_configured())
            params = cloud_db.get_connection_params()
            self.assertEqual(params["dsn"], "postgresql://user:pass@cloud.example:5432/quanteye")
            self.assertEqual(params["connect_timeout"], cloud_db.PG_CONNECT_TIMEOUT)

    def test_init_pool_fails_closed_when_unconfigured(self):
        with patch.dict(os.environ, {"PGHOST": "", "DATABASE_URL": ""}, clear=False):
            os.environ.pop("DATABASE_URL", None)
            os.environ.pop("POSTGRES_URL", None)
            os.environ.pop("PGHOST", None)
            with self.assertRaises(RuntimeError):
                cloud_db.init_cloud_pool()
            with self.assertRaises(RuntimeError):
                cloud_db.init_cloud_db()

    def test_production_rejects_localhost_database_url(self):
        with patch.dict(
            os.environ,
            {
                "RAILWAY_ENVIRONMENT": "production",
                "DATABASE_URL": "postgresql://user:pass@127.0.0.1:5432/quanteye",
            },
            clear=False,
        ):
            self.assertFalse(cloud_db.postgres_is_configured())


class UniqueCompanyUrlTests(unittest.TestCase):
    def test_each_org_gets_a_stable_unique_path(self):
        slug_a = make_portal_slug("Fork n Knives", "ORG-100")
        slug_b = make_portal_slug("QuFok", "ORG-200")
        self.assertNotEqual(slug_a, slug_b)
        self.assertIn("/c/", company_portal_url("Fork n Knives", "ORG-100"))
        self.assertIn(slug_a, company_portal_url("Fork n Knives", "ORG-100"))
        self.assertTrue(company_portal_url("Fork n Knives", "ORG-100").endswith("/"))


class Layer4ReplicaHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("FLASK_ENV", "testing")
        os.environ.setdefault("APP_ENV", "test")
        from app import app

        app.testing = True
        cls.client = app.test_client()

    def test_cloud_stats_503_without_database_url(self):
        with patch.dict(os.environ, {"DATABASE_URL": "", "POSTGRES_URL": "", "PGHOST": ""}, clear=False):
            os.environ.pop("DATABASE_URL", None)
            os.environ.pop("POSTGRES_URL", None)
            os.environ.pop("PGHOST", None)
            response = self.client.get("/api/cloud/stats")
            self.assertEqual(response.status_code, 503)
            body = response.get_json()
            self.assertFalse(body.get("success"))
            self.assertIn("DATABASE_URL", body.get("error", ""))

    def test_edge_auth_503_without_database_url(self):
        with patch.dict(os.environ, {"DATABASE_URL": "", "POSTGRES_URL": "", "PGHOST": ""}, clear=False):
            os.environ.pop("DATABASE_URL", None)
            os.environ.pop("POSTGRES_URL", None)
            os.environ.pop("PGHOST", None)
            response = self.client.post(
                "/api/cloud/edge/auth",
                json={"edge_node_id": "node-1", "secret": "secret-value"},
            )
            self.assertEqual(response.status_code, 503)


if __name__ == "__main__":
    unittest.main()
