import sqlite3
import os
import uuid
import json
import logging
import secrets
import hashlib
from datetime import datetime, timezone
from typing import List, Dict, Optional, Tuple, Any, Union
from werkzeug.security import check_password_hash, generate_password_hash
from tenant_urls import company_portal_url, make_portal_slug

logger = logging.getLogger("quanteye.main_db")

try:
    import cloud_database as cloud_db
    _HAS_CLOUD_DB = True
except Exception as _ce:
    cloud_db = None
    _HAS_CLOUD_DB = False
    logger.warning("Cloud PostgreSQL persistence layer not initialized: %s", _ce)

def _resolve_db_path() -> str:
    if os.environ.get("QINTELLECT_MAIN_DB_PATH"):
        return os.environ.get("QINTELLECT_MAIN_DB_PATH")
    if os.environ.get("DATABASE_PATH"):
        return os.environ.get("DATABASE_PATH")
    if os.path.isdir("/data"):
        return "/data/dashboard.db"
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard.db")

DB_PATH = _resolve_db_path()


def new_enrollment_key() -> str:
    """Client Settings key that enables Staff Enrollment. Not an EXE branch key."""
    return f"ENR-{secrets.token_hex(10).upper()}"

def get_connection() -> sqlite3.Connection:
    """Return a SQLite connection with foreign keys enabled and row factory configured."""
    os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.row_factory = sqlite3.Row
    return conn

def init_db() -> bool:
    """Initialize the persistent schema for QuantEye-Main-Dashboard."""
    with get_connection() as conn:
        cursor = conn.cursor()

        # If legacy tables are present, ensure they are removed/refreshed
        legacy_tables = ['modules', 'builds']
        for lt in legacy_tables:
            cursor.execute(f"DROP TABLE IF EXISTS {lt}")

        # 1. Organizations table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS organizations (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                industry TEXT DEFAULT 'Technology',
                email TEXT,
                phone TEXT,
                address TEXT,
                status TEXT NOT NULL DEFAULT 'Active' CHECK(status IN ('Active', 'Build Pending', 'Inactive', 'Suspended')),
                portal_slug TEXT UNIQUE,
                enrollment_key TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # 2. Organization Contacts table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS organization_contacts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                organization_id TEXT NOT NULL,
                name TEXT NOT NULL,
                email TEXT,
                phone TEXT,
                role TEXT DEFAULT 'Primary Contact',
                is_primary INTEGER NOT NULL DEFAULT 1 CHECK(is_primary IN (0, 1)),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (organization_id) REFERENCES organizations(id) ON DELETE CASCADE
            )
        ''')

        # 3. Branches table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS branches (
                id TEXT NOT NULL,
                organization_id TEXT NOT NULL,
                name TEXT NOT NULL,
                city TEXT DEFAULT '',
                address TEXT DEFAULT '',
                access TEXT NOT NULL DEFAULT 'Active' CHECK(access IN ('Active', 'Suspended', 'Grace Period')),
                grace_days INTEGER NOT NULL DEFAULT 0,
                employee_count INTEGER NOT NULL DEFAULT 0,
                token_key TEXT,
                token_parent_id TEXT,
                token_started_at TEXT,
                token_expires_at TEXT,
                suspension_kind TEXT,
                suspension_days INTEGER DEFAULT 0,
                suspension_started_at TEXT,
                suspension_ends_at TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (organization_id, id),
                FOREIGN KEY (organization_id) REFERENCES organizations(id) ON DELETE CASCADE
            )
        ''')

        # 4. Branch Contacts table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS branch_contacts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                organization_id TEXT NOT NULL,
                branch_id TEXT NOT NULL,
                name TEXT NOT NULL,
                phone TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'Branch Manager',
                email TEXT,
                is_primary INTEGER NOT NULL DEFAULT 1 CHECK(is_primary IN (0, 1)),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (organization_id, branch_id) REFERENCES branches(organization_id, id) ON DELETE CASCADE
            )
        ''')

        # 5. Module Catalog table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS module_catalog (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                icon TEXT NOT NULL,
                description TEXT NOT NULL,
                required INTEGER NOT NULL DEFAULT 0 CHECK(required IN (0, 1)),
                sort_order INTEGER NOT NULL DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # 6. Organization Modules (many-to-many subscription)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS organization_modules (
                organization_id TEXT NOT NULL,
                module_id TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0, 1)),
                enabled_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (organization_id, module_id),
                FOREIGN KEY (organization_id) REFERENCES organizations(id) ON DELETE CASCADE,
                FOREIGN KEY (module_id) REFERENCES module_catalog(id) ON DELETE CASCADE
            )
        ''')

        # 7. Organization Licenses / Tokens table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS organization_licenses (
                id TEXT PRIMARY KEY,
                organization_id TEXT NOT NULL,
                token_key TEXT NOT NULL UNIQUE,
                started_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                validity_days INTEGER NOT NULL DEFAULT 30,
                status TEXT NOT NULL DEFAULT 'Valid' CHECK(status IN ('Valid', 'Expiring Soon', 'Expired', 'Blocked')),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (organization_id) REFERENCES organizations(id) ON DELETE CASCADE
            )
        ''')

        # 8. Organization Branding table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS organization_branding (
                organization_id TEXT PRIMARY KEY,
                logo_url TEXT DEFAULT '',
                app_title TEXT DEFAULT 'QuantEye - AI Attendance System',
                primary_color TEXT DEFAULT '#0284c7',
                slogan TEXT DEFAULT 'DRIVING THE FUTURE TECHNOLOGY POWERED BY QUANTUM & AI',
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (organization_id) REFERENCES organizations(id) ON DELETE CASCADE
            )
        ''')

        # 9. Invoices table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS invoices (
                id TEXT PRIMARY KEY,
                invoice_number TEXT NOT NULL UNIQUE,
                organization_id TEXT NOT NULL,
                organization_name TEXT NOT NULL,
                branch_id TEXT NOT NULL,
                branch_name TEXT NOT NULL,
                client_name TEXT NOT NULL,
                client_email TEXT,
                client_phone TEXT,
                billing_address TEXT,
                tax_details TEXT DEFAULT 'NTN: J756903',
                issue_date TEXT NOT NULL,
                due_date TEXT NOT NULL,
                subtotal REAL NOT NULL DEFAULT 0.0,
                discount REAL NOT NULL DEFAULT 0.0,
                tax_rate REAL NOT NULL DEFAULT 0.0,
                tax REAL NOT NULL DEFAULT 0.0,
                total REAL NOT NULL DEFAULT 0.0,
                paid REAL NOT NULL DEFAULT 0.0,
                payment_method TEXT,
                status TEXT NOT NULL DEFAULT 'Pending' CHECK(status IN ('Draft', 'Pending', 'Paid', 'Partial', 'Overdue', 'Cancelled', 'Unpaid', 'Partially Paid')),
                notes TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (organization_id, branch_id) REFERENCES branches(organization_id, id) ON DELETE CASCADE
            )
        ''')

        # 10. Invoice Items table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS invoice_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                invoice_id TEXT NOT NULL,
                description TEXT NOT NULL,
                quantity INTEGER NOT NULL DEFAULT 1 CHECK(quantity > 0),
                unit_price REAL NOT NULL DEFAULT 0.0 CHECK(unit_price >= 0),
                amount REAL NOT NULL DEFAULT 0.0,
                sort_order INTEGER NOT NULL DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (invoice_id) REFERENCES invoices(id) ON DELETE CASCADE
            )
        ''')

        # 11. Payments table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                invoice_id TEXT NOT NULL,
                amount REAL NOT NULL CHECK(amount > 0),
                method TEXT NOT NULL DEFAULT 'Bank Transfer',
                reference TEXT DEFAULT '',
                payment_date TEXT NOT NULL,
                verified INTEGER NOT NULL DEFAULT 0 CHECK(verified IN (0, 1)),
                notes TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (invoice_id) REFERENCES invoices(id) ON DELETE CASCADE
            )
        ''')

        # 12. Application Builds table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS application_builds (
                id TEXT PRIMARY KEY,
                organization_id TEXT NOT NULL,
                organization_name TEXT NOT NULL,
                branch_id TEXT NOT NULL,
                branch_name TEXT NOT NULL,
                app_name TEXT NOT NULL,
                version TEXT NOT NULL,
                modules_snapshot TEXT NOT NULL DEFAULT '[]',
                status TEXT NOT NULL DEFAULT 'queued' CHECK(status IN ('Queued', 'Building', 'Success', 'Failed', 'queued', 'building', 'succeeded', 'failed')),
                progress INTEGER NOT NULL DEFAULT 0 CHECK(progress >= 0 AND progress <= 100),
                download_manifest_json TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (organization_id, branch_id) REFERENCES branches(organization_id, id) ON DELETE CASCADE
            )
        ''')

        # 13. Platform Audit Logs table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS platform_audit_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                organization_id TEXT,
                branch_id TEXT,
                action TEXT NOT NULL,
                message TEXT NOT NULL,
                metadata_json TEXT DEFAULT '{}',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # 14. Control Plane Users table (Step 6 control plane authentication)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS control_plane_users (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                email TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'organization_admin' CHECK(role IN ('platform_admin', 'organization_admin')),
                organization_id TEXT,
                active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0, 1)),
                last_login_at TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (organization_id) REFERENCES organizations(id) ON DELETE CASCADE
            )
        ''')

        # 15. Branch Cameras (Cloud-configured cameras per branch)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS branch_cameras (
                id TEXT NOT NULL,
                organization_id TEXT NOT NULL,
                branch_id TEXT NOT NULL,
                name TEXT NOT NULL,
                camera_type TEXT NOT NULL DEFAULT 'rtsp' CHECK(camera_type IN ('rtsp', 'webcam', 'dvr', 'nvr')),
                rtsp_url TEXT NOT NULL,
                room_name TEXT DEFAULT 'Entrance',
                channel_number INTEGER DEFAULT 1,
                active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0, 1)),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (organization_id, branch_id, id),
                FOREIGN KEY (organization_id, branch_id) REFERENCES branches(organization_id, id) ON DELETE CASCADE
            )
        ''')

        # 16. Live Attendance Sync Events (Real-time events pushed from Client Nodes)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS live_attendance_sync_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                organization_id TEXT NOT NULL,
                branch_id TEXT NOT NULL,
                camera_id TEXT,
                camera_name TEXT,
                user_id INTEGER,
                name TEXT NOT NULL,
                confidence REAL,
                status TEXT NOT NULL DEFAULT 'Present',
                source TEXT DEFAULT 'ai_camera',
                face_crop_base64 TEXT,
                timestamp TEXT NOT NULL,
                synced_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS live_attendance_sync_event_receipts (
                source_event_id TEXT PRIMARY KEY,
                payload_hash TEXT NOT NULL,
                attendance_event_id INTEGER NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS attendance_postgres_sync_queue (
                source_event_id TEXT PRIMARY KEY,
                organization_id TEXT NOT NULL,
                branch_id TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_attempt_at TIMESTAMP,
                last_error TEXT,
                synced_at TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # Explicit Indexes
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_branch_cams ON branch_cameras(organization_id, branch_id);')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_sync_events_org ON live_attendance_sync_events(organization_id, branch_id);')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_sync_events_time ON live_attendance_sync_events(timestamp);')
        cursor.execute(
            'CREATE INDEX IF NOT EXISTS idx_attendance_pg_sync_due '
            'ON attendance_postgres_sync_queue(status, next_attempt_at)'
        )
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_org_status ON organizations(status);')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_cpu_email ON control_plane_users(email);')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_cpu_org ON control_plane_users(organization_id);')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_branches_org ON branches(organization_id);')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_branches_access ON branches(access);')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_org_licenses_org ON organization_licenses(organization_id);')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_org_modules_org ON organization_modules(organization_id);')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_invoices_org ON invoices(organization_id);')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_invoices_branch ON invoices(organization_id, branch_id);')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_invoices_status ON invoices(status);')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_invoice_items_inv ON invoice_items(invoice_id);')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_payments_inv ON payments(invoice_id);')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_builds_branch ON application_builds(organization_id, branch_id);')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_audit_created ON platform_audit_logs(created_at);')

        # Initial Seed: 5 canonical commercial QIntellect modules
        canonical_modules = [
            ("attendance", "Attendance Management", "fa-calendar-check", "Track daily attendance, presence, and AI camera check-in activity.", 1, 1),
            ("shifts", "Shift Management", "fa-business-time", "Configure shifts, rosters, and schedules.", 0, 2),
            ("overtime", "Overtime Management", "fa-clock", "Track overtime hours, policies, and approvals.", 0, 3),
            ("leave", "Leave Management", "fa-plane-departure", "Handle leave balances, requests, and approvals.", 0, 4),
            ("payroll", "Payroll Management", "fa-money-check-dollar", "Automated payroll processing based on attendance.", 0, 5),
            ("mobile", "Mobile App", "fa-mobile-screen", "One phone page where each employee sees only their own attendance.", 0, 6),
        ]
        cursor.execute("DELETE FROM module_catalog WHERE id NOT IN ('attendance', 'shifts', 'overtime', 'leave', 'payroll', 'mobile')")
        cursor.execute("DELETE FROM organization_modules WHERE module_id NOT IN ('attendance', 'shifts', 'overtime', 'leave', 'payroll', 'mobile')")

        cursor.executemany('''
            INSERT INTO module_catalog (id, name, icon, description, required, sort_order)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                name = excluded.name,
                icon = excluded.icon,
                description = excluded.description,
                required = excluded.required,
                sort_order = excluded.sort_order
        ''', canonical_modules)

        # Seed initial Demo Organization if organizations is empty
        def _seed_org(o_id, o_name, o_ind, o_email, o_phone, o_addr, o_emp, o_modules, o_tok, o_user, o_pass):
            cursor.execute("SELECT COUNT(*) FROM organizations WHERE id = ? OR name = ?", (o_id, o_name))
            if cursor.fetchone()[0] == 0:
                cursor.execute('''
                    INSERT INTO organizations (id, name, industry, email, phone, address, status)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                ''', (o_id, o_name, o_ind, o_email, o_phone, o_addr, "Active"))

                cursor.execute('''
                    INSERT INTO organization_contacts (organization_id, name, email, phone, role, is_primary)
                    VALUES (?, ?, ?, ?, ?, 1)
                ''', (o_id, "Operations Director", o_email, o_phone, "Primary Contact"))

                br_id = f"BR-{o_id}"
                cursor.execute('''
                    INSERT INTO branches (id, organization_id, name, city, address, access, grace_days, employee_count, token_key, token_parent_id, token_started_at, token_expires_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (br_id, o_id, "Main Branch", "Rahim Yar Khan", o_addr or "Main Campus", "Active", 5, o_emp, o_tok, f"TOK-{o_id}", "2026-10-01", "2027-10-01"))

                cursor.execute('''
                    INSERT INTO branch_contacts (organization_id, branch_id, name, phone, role, email)
                    VALUES (?, ?, ?, ?, ?, ?)
                ''', (o_id, br_id, "Branch Manager", o_phone, "Branch Head", o_email))

                for mod_id in o_modules:
                    cursor.execute('''
                        INSERT OR IGNORE INTO organization_modules (organization_id, module_id, enabled)
                        VALUES (?, ?, 1)
                    ''', (o_id, mod_id))

                cursor.execute('''
                    INSERT OR IGNORE INTO organization_licenses (id, organization_id, token_key, started_at, expires_at, validity_days, status)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                ''', (f"TOK-{o_id}", o_id, o_tok, "2026-10-01", "2027-10-01", 365, "Valid"))

                cursor.execute('''
                    INSERT OR IGNORE INTO organization_branding (organization_id, logo_url, app_title, primary_color, slogan)
                    VALUES (?, ?, ?, ?, ?)
                ''', (o_id, "", f"QuantEye - {o_name}", "#0284c7", "DRIVING THE FUTURE TECHNOLOGY POWERED BY QUANTUM & AI"))

                manifest_json = json.dumps({
                    "client_portal_url": "https://quanteye-ai-attendence-production.up.railway.app",
                    "client_username": o_user,
                    "client_password": o_pass,
                    "branch_key": o_tok
                })
                cursor.execute('''
                    INSERT OR IGNORE INTO application_builds (
                        id, organization_id, organization_name, branch_id, branch_name, app_name,
                        version, status, progress, download_manifest_json, modules_snapshot, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                ''', (
                    f"BUILD-{o_id}", o_id, o_name, br_id, "Main Branch", f"{o_name} Application",
                    "v1.0.1", "succeeded", 100, manifest_json, json.dumps(o_modules)
                ))

        # NOTE: Demo organization auto-seeding has been intentionally removed.
        # Organizations are now created exclusively through the UI or API.
        # This prevents deleted organizations from reappearing after a server restart.

        _migrate_build_manifest_credentials(cursor)
        try:
            cursor.execute("ALTER TABLE organizations ADD COLUMN portal_slug TEXT")
        except sqlite3.OperationalError:
            pass
        cursor.execute("SELECT id, name, portal_slug FROM organizations")
        for row in cursor.fetchall():
            if row["portal_slug"]:
                continue
            slug = make_portal_slug(row["name"], row["id"])
            cursor.execute("UPDATE organizations SET portal_slug = ? WHERE id = ?", (slug, row["id"]))
        try:
            cursor.execute("ALTER TABLE organizations ADD COLUMN enrollment_key TEXT")
        except sqlite3.OperationalError:
            pass
        cursor.execute("SELECT id, enrollment_key FROM organizations")
        for row in cursor.fetchall():
            if str(row["enrollment_key"] or "").strip():
                continue
            cursor.execute(
                "UPDATE organizations SET enrollment_key = ? WHERE id = ?",
                (new_enrollment_key(), row["id"]),
            )
        conn.commit()

    if _HAS_CLOUD_DB and cloud_db.postgres_is_configured():
        try:
            cloud_db.ensure_portal_slug_column()
            cloud_db.ensure_enrollment_key_column()
            cloud_db.init_cloud_db()
        except Exception as e:
            logger.warning("cloud_db init failed during init_db: %s", e)
            try:
                cloud_db.ensure_portal_slug_column()
                cloud_db.ensure_enrollment_key_column()
            except Exception as migrate_error:
                logger.warning("portal_slug migration failed: %s", migrate_error)
    elif _HAS_CLOUD_DB:
        logger.info("PostgreSQL is not configured; SQLite remains the control-plane store.")

    return True


# ==============================================================================
# ORGANIZATIONS & BRANCHES CRUD HELPERS
# ==============================================================================

def _postgres_control_plane_ready() -> bool:
    return bool(_HAS_CLOUD_DB and cloud_db is not None and cloud_db.postgres_is_configured())


def _sqlite_branch_contacts(org_id: str) -> Dict[str, Dict[str, str]]:
    """Contacts saved with the branch, keyed by branch id."""
    try:
        with get_connection() as conn:
            rows = conn.execute(
                """SELECT branch_id, name, phone, role, email
                     FROM branch_contacts
                    WHERE organization_id = ? AND is_primary = 1
                    ORDER BY id""",
                (org_id,),
            ).fetchall()
    except Exception:
        return {}
    contacts = {}
    for row in rows:
        contacts[str(row["branch_id"])] = {
            "name": row["name"] or "",
            "phone": row["phone"] or "",
            "role": row["role"] or "",
            "email": row["email"] or "",
        }
    return contacts


def _decorate_organization_urls(org_dict: Dict[str, Any]) -> Dict[str, Any]:
    org_id = str(org_dict.get("id") or "")
    name = str(org_dict.get("name") or "")
    slug = str(org_dict.get("portal_slug") or org_dict.get("portalSlug") or "").strip() or make_portal_slug(name, org_id)
    org_dict["portal_slug"] = slug
    org_dict["portalSlug"] = slug
    org_dict["clientPortalUrl"] = company_portal_url(name, org_id, slug)
    org_dict["client_portal_url"] = org_dict["clientPortalUrl"]
    org_dict["createdAt"] = org_dict.get("createdAt") or org_dict.get("created_at") or ""
    org_dict["updatedAt"] = org_dict.get("updatedAt") or org_dict.get("updated_at") or ""
    org_dict["tokenKey"] = org_dict.get("tokenKey") or org_dict.get("token_key") or ""
    enrollment_key = str(org_dict.get("enrollmentKey") or org_dict.get("enrollment_key") or "").strip()
    org_dict["enrollment_key"] = enrollment_key
    org_dict["enrollmentKey"] = enrollment_key
    if not isinstance(org_dict.get("modules"), list):
        org_dict["modules"] = []
    return org_dict


def verify_enrollment_key(organization_id: str, provided_key: str) -> bool:
    org = get_organization_by_id(organization_id)
    stored = str((org or {}).get("enrollmentKey") or "").strip()
    offered = str(provided_key or "").strip()
    if not stored or not offered:
        return False
    return secrets.compare_digest(stored.upper(), offered.upper())


def _organization_from_postgres(org_id: str) -> Optional[Dict[str, Any]]:
    if not _postgres_control_plane_ready():
        return None
    try:
        row = cloud_db.get_organization(org_id, user_context=None)
        if not row:
            return None
        org_dict = dict(row)
        modules = cloud_db.get_organization_modules(org_id, user_context=None) or []
        org_dict["modules"] = [m.get("module_id") for m in modules if m.get("is_enabled")]
        saved_contacts = _sqlite_branch_contacts(org_id)
        branches = []
        for branch in cloud_db.list_branches(org_id, user_context=None) or []:
            b_dict = dict(branch)
            b_dict["graceDays"] = b_dict.get("grace_days", 0)
            b_dict["employeeCount"] = b_dict.get("employee_count", 0)
            b_dict["tokenKey"] = b_dict.get("token_key") or ""
            b_dict["tokenExpiresAt"] = b_dict.get("token_expires_at") or ""
            b_dict["access"] = b_dict.get("access") or "Active"
            contact = {
                "name": str(b_dict.get("contact_name") or "").strip(),
                "phone": str(b_dict.get("contact_phone") or "").strip(),
                "role": str(b_dict.get("contact_role") or "").strip(),
                "email": str(b_dict.get("contact_email") or "").strip(),
            }
            if not contact["name"]:
                contact = saved_contacts.get(str(b_dict.get("id") or "")) or contact
            b_dict["contact"] = contact
            branches.append(b_dict)
        org_dict["branches"] = branches
        licenses = cloud_db.get_organization_licenses(org_id, user_context=None) or []
        if licenses:
            lic = licenses[0]
            org_dict["tokenKey"] = lic.get("license_key") or lic.get("token_key") or ""
            org_dict["tokenExpiresAt"] = lic.get("expires_at") or ""
        branding = cloud_db.get_organization_branding(org_id, user_context=None) or {}
        org_dict["branding"] = {
            "appTitle": branding.get("company_name") or f"{org_dict.get('name', 'App')} Application",
            "logo": branding.get("logo_url") or "",
        }
        if not org_dict.get("portal_slug"):
            org_dict["portal_slug"] = make_portal_slug(org_dict.get("name") or "", org_id)
        return _decorate_organization_urls(org_dict)
    except Exception as error:
        logger.warning("PostgreSQL control-plane read failed for %s: %s", org_id, error)
        return None


def get_all_organizations() -> List[Dict[str, Any]]:
    """Retrieve all organizations with aggregated module, branch, and license info."""
    if _postgres_control_plane_ready():
        try:
            rows = cloud_db.list_organizations(user_context=None)
            hydrated = []
            for row in rows or []:
                bundle = _organization_from_postgres(row["id"])
                if bundle:
                    hydrated.append(bundle)
            if hydrated:
                return hydrated
        except Exception as error:
            logger.warning("PostgreSQL control-plane list failed; using SQLite: %s", error)
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM organizations ORDER BY created_at DESC")
        org_rows = cursor.fetchall()
        result = []
        for org in org_rows:
            org_dict = dict(org)
            # Fetch subscribed module IDs
            cursor.execute("SELECT module_id FROM organization_modules WHERE organization_id = ? AND enabled = 1", (org['id'],))
            org_dict['modules'] = [r['module_id'] for r in cursor.fetchall()]
            
            # Fetch branches
            cursor.execute("SELECT * FROM branches WHERE organization_id = ?", (org['id'],))
            branch_rows = cursor.fetchall()
            branches = []
            total_emp = 0
            for b in branch_rows:
                b_dict = dict(b)
                b_dict['graceDays'] = b_dict.get('grace_days', 0)
                b_dict['employeeCount'] = b_dict.get('employee_count', 0)
                b_dict['tokenKey'] = b_dict.get('token_key') or ''
                b_dict['tokenParentId'] = b_dict.get('token_parent_id') or ''
                b_dict['tokenStartedAt'] = b_dict.get('token_started_at') or ''
                b_dict['tokenExpiresAt'] = b_dict.get('token_expires_at') or ''
                if b_dict.get('suspension_kind'):
                    b_dict['suspension'] = {
                        'kind': b_dict.get('suspension_kind'),
                        'days': b_dict.get('suspension_days', 0),
                        'startedAt': b_dict.get('suspension_started_at', ''),
                        'endsAt': b_dict.get('suspension_ends_at', '')
                    }
                else:
                    b_dict['suspension'] = None
                total_emp += b_dict.get('employee_count', 0)
                # Fetch branch contact
                cursor.execute("SELECT * FROM branch_contacts WHERE organization_id = ? AND branch_id = ? AND is_primary = 1", (org['id'], b['id']))
                bc = cursor.fetchone()
                b_dict['contact'] = dict(bc) if bc else {}
                branches.append(b_dict)
            org_dict['branches'] = branches
            org_dict['employeeCount'] = total_emp

            # Fetch license
            cursor.execute("SELECT * FROM organization_licenses WHERE organization_id = ? ORDER BY created_at DESC LIMIT 1", (org['id'],))
            lic = cursor.fetchone()
            if lic:
                org_dict['tokenId'] = lic['id']
                org_dict['tokenKey'] = lic['token_key']
                org_dict['tokenStartedAt'] = lic['started_at']
                org_dict['tokenExpiresAt'] = lic['expires_at']
                org_dict['tokenValidityDays'] = lic['validity_days']

            # Fetch branding
            cursor.execute("SELECT * FROM organization_branding WHERE organization_id = ?", (org['id'],))
            br = cursor.fetchone()
            if br:
                br_dict = dict(br)
                br_dict['appTitle'] = br_dict.get('app_title', '')
                br_dict['logo'] = br_dict.get('logo_url', '')
                org_dict['branding'] = br_dict
            else:
                org_dict['branding'] = {'appTitle': f"{org_dict.get('name', 'App')} Application", 'logo': ''}

            result.append(_decorate_organization_urls(org_dict))
        return result

def get_organization_by_id(org_id: str) -> Optional[Dict[str, Any]]:
    """Retrieve a single organization by ID."""
    orgs = [o for o in get_all_organizations() if o['id'] == org_id]
    return orgs[0] if orgs else None

def create_organization(data: Dict[str, Any]) -> Dict[str, Any]:
    """Create a new organization in persistent storage."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM organizations")
        count = cursor.fetchone()[0]
        org_id = data.get('id') or f"ORG-{count + 1:03d}"
        name = str(data.get('name', '')).strip()
        industry = str(data.get('industry', 'Technology')).strip()
        email = str(data.get('email', '')).strip()
        phone = str(data.get('phone', '')).strip()
        address = str(data.get('address', '')).strip()
        status = data.get('status', 'Active')

        portal_slug = make_portal_slug(name, org_id)
        enrollment_key = data.get("enrollmentKey") or data.get("enrollment_key") or new_enrollment_key()
        cursor.execute('''
            INSERT INTO organizations (id, name, industry, email, phone, address, status, portal_slug, enrollment_key)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (org_id, name, industry, email, phone, address, status, portal_slug, enrollment_key))

        # Organization Modules
        selected_modules = data.get('modules') or ['attendance', 'employees']
        cursor.execute("SELECT id, required FROM module_catalog")
        all_mods = cursor.fetchall()
        for mod in all_mods:
            enabled = 1 if (mod['id'] in selected_modules) else 0
            cursor.execute('''
                INSERT OR REPLACE INTO organization_modules (organization_id, module_id, enabled)
                VALUES (?, ?, ?)
            ''', (org_id, mod['id'], enabled))

        # Primary Contact
        contact = data.get('contact', {})
        if contact:
            cursor.execute('''
                INSERT INTO organization_contacts (organization_id, name, email, phone, role, is_primary)
                VALUES (?, ?, ?, ?, ?, 1)
            ''', (org_id, contact.get('name', 'Admin'), contact.get('email', email), contact.get('phone', phone), contact.get('role', 'Primary Contact')))

        # License Token
        token_id = data.get('tokenId') or f"TOK-{org_id}"
        token_key = data.get('tokenKey') or f"QE-{secrets.token_hex(32).upper()}"

        # Default Branch if provided
        branches_data = data.get('branches', [])
        branch_token_keys = {}
        if branches_data:
            for b in branches_data:
                b_id = b.get('id', 'BR-01')
                b_token = b.get('tokenKey') or b.get('branchKey') or f"QE-{secrets.token_hex(32).upper()}"
                branch_token_keys[b_id] = b_token
                cursor.execute('''
                    INSERT OR REPLACE INTO branches (id, organization_id, name, city, address, access, grace_days, employee_count, token_key, token_parent_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (b_id, org_id, b.get('name', 'Main Branch'), b.get('city', ''), b.get('address', ''), b.get('access', 'Active'), b.get('graceDays', 0), b.get('employeeCount', 0), b_token, token_id))
                b_contact = b.get('contact', {})
                if b_contact:
                    cursor.execute('''
                        INSERT OR REPLACE INTO branch_contacts (organization_id, branch_id, name, phone, role, email)
                        VALUES (?, ?, ?, ?, ?, ?)
                    ''', (org_id, b_id, b_contact.get('name', 'Manager'), b_contact.get('phone', ''), b_contact.get('role', 'Branch Manager'), b_contact.get('email', '')))
        else:
            # Create standard default branch
            b_id = "BR-01"
            branch_token_keys[b_id] = f"QE-{secrets.token_hex(32).upper()}"
            cursor.execute('''
                INSERT INTO branches (id, organization_id, name, city, address, access, grace_days, employee_count, token_key, token_parent_id)
                VALUES (?, ?, 'Main Branch', '', address, 'Active', 0, 0, ?, ?)
            ''', (b_id, org_id, branch_token_keys[b_id], token_id))
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        exp_str = (datetime.now(timezone.utc).replace(year=datetime.now().year + 1)).strftime("%Y-%m-%d")
        cursor.execute('''
            INSERT OR REPLACE INTO organization_licenses (id, organization_id, token_key, started_at, expires_at, validity_days, status)
            VALUES (?, ?, ?, ?, ?, 365, 'Valid')
        ''', (token_id, org_id, token_key, now_str, exp_str))

        # Branding
        cursor.execute('''
            INSERT OR REPLACE INTO organization_branding (organization_id, app_title, primary_color)
            VALUES (?, ?, '#0284c7')
        ''', (org_id, f"QuantEye - {name}"))

        # Audit
        cursor.execute('''
            INSERT INTO platform_audit_logs (organization_id, action, message)
            VALUES (?, 'ORG_CREATED', ?)
        ''', (org_id, f"Created organization '{name}' (ID: {org_id})"))

        conn.commit()

    if _HAS_CLOUD_DB and cloud_db.postgres_is_configured():
        try:
            cloud_db.create_organization(
                org_id=org_id,
                name=name,
                industry=industry,
                email=email,
                phone=phone,
                address=address,
                status=status,
                portal_slug=portal_slug,
                enrollment_key=enrollment_key,
            )
            b_list = branches_data if branches_data else [{'id': 'BR-01', 'name': 'Main Branch', 'city': '', 'address': address}]
            for b in b_list:
                b_id = b.get('id', 'BR-01')
                b_contact = b.get('contact') or {}
                cloud_db.create_branch(
                    org_id=org_id,
                    branch_id=b_id,
                    name=b.get('name', 'Main Branch'),
                    city=b.get('city', ''),
                    address=b.get('address', address),
                    token_key=branch_token_keys[b_id],
                    contact_name=str(b_contact.get('name') or ''),
                    contact_phone=str(b_contact.get('phone') or ''),
                    contact_role=str(b_contact.get('role') or ''),
                    contact_email=str(b_contact.get('email') or ''),
                )
            for mid in selected_modules:
                cloud_db.set_organization_module(org_id, mid, is_enabled=True)
            cloud_db.create_organization_license(org_id, token_key)
            cloud_db.set_organization_branding(org_id, company_name=f"QuantEye - {name}")
        except Exception as e:
            logger.warning("cloud_db dual write error in create_organization: %s", e)

    return get_organization_by_id(org_id) or {}

def get_module_catalog() -> List[Dict[str, Any]]:
    """Retrieve canonical module catalog."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM module_catalog ORDER BY sort_order ASC")
        return [dict(r) for r in cursor.fetchall()]

def _sync_organization_update_to_postgres(org_id: str, data: Dict[str, Any]) -> None:
    """Keep the live company record, modules, and branch contact in Postgres."""
    if not _postgres_control_plane_ready():
        return
    cloud_db.update_organization_profile(
        org_id,
        name=data.get('name'),
        industry=data.get('industry'),
        email=data.get('email'),
        phone=data.get('phone'),
        address=data.get('address'),
        status=data.get('status'),
    )
    if 'modules' in data:
        selected = {str(module).strip().lower() for module in (data.get('modules') or [])}
        for module in cloud_db.list_module_catalog() or []:
            module_id = str(module.get('id') or '').strip()
            if module_id:
                cloud_db.set_organization_module(org_id, module_id, is_enabled=module_id in selected)
    for branch in data.get('branches') or []:
        if not isinstance(branch, dict):
            continue
        contact = branch.get('contact') or {}
        cloud_db.create_branch(
            org_id=org_id,
            branch_id=branch.get('id') or 'BR-01',
            name=branch.get('name') or 'Main Branch',
            city=branch.get('city') or '',
            address=branch.get('address') or '',
            access=branch.get('access') or 'Active',
            grace_days=int(branch.get('graceDays') or 0),
            employee_count=int(branch.get('employeeCount') or 0),
            token_key=branch.get('tokenKey') or '',
            contact_name=str(contact.get('name') or ''),
            contact_phone=str(contact.get('phone') or ''),
            contact_role=str(contact.get('role') or ''),
            contact_email=str(contact.get('email') or ''),
        )


def update_organization(org_id: str, data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Update an existing organization, its modules, branches, branding, and license in persistent storage."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM organizations WHERE id = ?", (org_id,))
        if not cursor.fetchone():
            return None

        name = data.get('name')
        industry = data.get('industry')
        email = data.get('email')
        phone = data.get('phone')
        address = data.get('address')
        status = data.get('status')
        now_ts = datetime.now(timezone.utc).isoformat()

        cursor.execute('''
            UPDATE organizations
            SET name = COALESCE(?, name),
                industry = COALESCE(?, industry),
                email = COALESCE(?, email),
                phone = COALESCE(?, phone),
                address = COALESCE(?, address),
                status = COALESCE(?, status),
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
        ''', (name, industry, email, phone, address, status, org_id))

        # Reconcile modules if provided
        if 'modules' in data:
            selected_modules = list(data.get('modules') or [])
            cursor.execute("SELECT id, required FROM module_catalog")
            all_mods = cursor.fetchall()
            for mod in all_mods:
                enabled = 1 if (mod['id'] in selected_modules) else 0
                cursor.execute('''
                    INSERT INTO organization_modules (organization_id, module_id, enabled, enabled_at)
                    VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(organization_id, module_id) DO UPDATE SET enabled = excluded.enabled
                ''', (org_id, mod['id'], enabled))

        # Reconcile branches if provided
        if 'branches' in data and isinstance(data['branches'], list):
            existing_branch_ids = set()
            for b in data['branches']:
                b_id = b.get('id') or "BR-01"
                existing_branch_ids.add(b_id)
                b_name = b.get('name', 'Main Branch')
                b_city = b.get('city', '')
                b_addr = b.get('address', '')
                b_access = b.get('access', 'Active')
                b_grace = int(b.get('graceDays', 0) or 0)
                b_emp = int(b.get('employeeCount', 0) or 0)
                b_token = b.get('tokenKey')
                b_parent_tok = b.get('tokenParentId')
                b_tok_start = b.get('tokenStartedAt')
                b_tok_exp = b.get('tokenExpiresAt')
                
                suspension = b.get('suspension') or {}
                s_kind = suspension.get('kind')
                s_days = suspension.get('days')
                s_start = suspension.get('startedAt')
                s_end = suspension.get('endsAt')

                cursor.execute('''
                    INSERT INTO branches (id, organization_id, name, city, address, access, grace_days, employee_count,
                                          token_key, token_parent_id, token_started_at, token_expires_at,
                                          suspension_kind, suspension_days, suspension_started_at, suspension_ends_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(organization_id, id) DO UPDATE SET
                        name = excluded.name,
                        city = excluded.city,
                        address = excluded.address,
                        access = excluded.access,
                        grace_days = excluded.grace_days,
                        employee_count = excluded.employee_count,
                        token_key = COALESCE(excluded.token_key, branches.token_key),
                        token_parent_id = COALESCE(excluded.token_parent_id, branches.token_parent_id),
                        token_started_at = COALESCE(excluded.token_started_at, branches.token_started_at),
                        token_expires_at = COALESCE(excluded.token_expires_at, branches.token_expires_at),
                        suspension_kind = excluded.suspension_kind,
                        suspension_days = excluded.suspension_days,
                        suspension_started_at = excluded.suspension_started_at,
                        suspension_ends_at = excluded.suspension_ends_at,
                        updated_at = CURRENT_TIMESTAMP
                ''', (b_id, org_id, b_name, b_city, b_addr, b_access, b_grace, b_emp,
                      b_token, b_parent_tok, b_tok_start, b_tok_exp,
                      s_kind, s_days, s_start, s_end))

                # Update contact
                contact = b.get('contact')
                if contact:
                    contact_name = contact.get('name') or 'Manager'
                    contact_phone = contact.get('phone') or ''
                    contact_role = contact.get('role') or 'Branch Manager'
                    contact_email = contact.get('email') or ''
                    cursor.execute(
                        """SELECT id FROM branch_contacts
                            WHERE organization_id = ? AND branch_id = ? AND is_primary = 1
                            ORDER BY id LIMIT 1""",
                        (org_id, b_id),
                    )
                    existing_contact = cursor.fetchone()
                    if existing_contact:
                        cursor.execute(
                            """UPDATE branch_contacts
                                  SET name = ?, phone = ?, role = ?, email = ?, updated_at = CURRENT_TIMESTAMP
                                WHERE id = ?""",
                            (contact_name, contact_phone, contact_role, contact_email, existing_contact['id']),
                        )
                    else:
                        cursor.execute(
                            """INSERT INTO branch_contacts (organization_id, branch_id, name, phone, role, email)
                               VALUES (?, ?, ?, ?, ?, ?)""",
                            (org_id, b_id, contact_name, contact_phone, contact_role, contact_email),
                        )

        # Reconcile license if provided. Keep the key this company already has.
        if 'tokenKey' in data or 'tokenExpiresAt' in data:
            tok_key = data.get('tokenKey') or f"KEY-{org_id}"
            tok_start = data.get('tokenStartedAt') or ""
            tok_exp = data.get('tokenExpiresAt') or ""
            tok_days = int(data.get('tokenValidityDays', 30) or 30)
            cursor.execute(
                "SELECT id FROM organization_licenses WHERE organization_id = ? OR token_key = ? ORDER BY created_at DESC LIMIT 1",
                (org_id, tok_key),
            )
            existing_license = cursor.fetchone()
            tok_id = (existing_license['id'] if existing_license else None) or data.get('tokenId') or f"TOK-{org_id}"
            cursor.execute('''
                INSERT INTO organization_licenses (id, organization_id, token_key, started_at, expires_at, validity_days, status, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, 'Valid', CURRENT_TIMESTAMP)
                ON CONFLICT(id) DO UPDATE SET
                    token_key = excluded.token_key,
                    started_at = excluded.started_at,
                    expires_at = excluded.expires_at,
                    validity_days = excluded.validity_days,
                    updated_at = CURRENT_TIMESTAMP
            ''', (tok_id, org_id, tok_key, tok_start, tok_exp, tok_days))

        # Reconcile branding if provided
        if 'branding' in data:
            br = data['branding']
            cursor.execute('''
                INSERT INTO organization_branding (organization_id, logo_url, app_title, updated_at)
                VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(organization_id) DO UPDATE SET
                    logo_url = COALESCE(excluded.logo_url, organization_branding.logo_url),
                    app_title = COALESCE(excluded.app_title, organization_branding.app_title),
                    updated_at = CURRENT_TIMESTAMP
            ''', (org_id, br.get('logo', ''), br.get('appTitle', f"QuantEye - {name or org_id}")))

        cursor.execute('''
            INSERT INTO platform_audit_logs (organization_id, action, message)
            VALUES (?, 'ORG_UPDATED', ?)
        ''', (org_id, f"Updated organization details for '{name or org_id}'"))

        conn.commit()

    _sync_organization_update_to_postgres(org_id, data)
    return get_organization_by_id(org_id)

def delete_organization(org_id: str) -> bool:
    """Delete an organization and its cascading branch configs from database."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM organizations WHERE id = ?", (org_id,))
        row = cursor.fetchone()
        if not row:
            return False
        org_name = row['name']
        cursor.execute(
            "DELETE FROM payments WHERE invoice_id IN (SELECT id FROM invoices WHERE organization_id = ?)",
            (org_id,),
        )
        cursor.execute(
            "DELETE FROM invoice_items WHERE invoice_id IN (SELECT id FROM invoices WHERE organization_id = ?)",
            (org_id,),
        )
        cursor.execute("DELETE FROM invoices WHERE organization_id = ?", (org_id,))
        cursor.execute("DELETE FROM application_builds WHERE organization_id = ?", (org_id,))
        cursor.execute("DELETE FROM branch_contacts WHERE organization_id = ?", (org_id,))
        cursor.execute("DELETE FROM branch_cameras WHERE organization_id = ?", (org_id,))
        cursor.execute("DELETE FROM control_plane_users WHERE organization_id = ?", (org_id,))
        cursor.execute("DELETE FROM organization_contacts WHERE organization_id = ?", (org_id,))
        cursor.execute("DELETE FROM organization_modules WHERE organization_id = ?", (org_id,))
        cursor.execute("DELETE FROM organization_licenses WHERE organization_id = ?", (org_id,))
        cursor.execute("DELETE FROM organization_branding WHERE organization_id = ?", (org_id,))
        cursor.execute("DELETE FROM organizations WHERE id = ?", (org_id,))
        cursor.execute('''
            INSERT INTO platform_audit_logs (organization_id, action, message)
            VALUES (?, 'ORG_DELETED', ?)
        ''', (org_id, f"Deleted organization '{org_name}' ({org_id})"))
        conn.commit()
        return True

def toggle_organization_status(org_id: str) -> Optional[str]:
    """Toggle organization between Active and Inactive."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT status, name FROM organizations WHERE id = ?", (org_id,))
        row = cursor.fetchone()
        if not row:
            return None
        new_status = "Inactive" if row['status'] == "Active" else "Active"
        cursor.execute("UPDATE organizations SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?", (new_status, org_id))
        cursor.execute('''
            INSERT INTO platform_audit_logs (organization_id, action, message)
            VALUES (?, 'STATUS_TOGGLED', ?)
        ''', (org_id, f"Set organization '{row['name']}' status to {new_status}"))
        conn.commit()
        return new_status

def rotate_organization_token(org_id: str, starts_at: str, expires_at: str, validity_days: int, token_key: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Rotate and save token validity period for an organization and propagate to branches."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id, name FROM organizations WHERE id = ?", (org_id,))
        org = cursor.fetchone()
        if not org:
            return None
        tok_id = f"TOK-{org_id}"
        if not token_key:
            import secrets
            token_key = "QE-" + secrets.token_hex(10).upper()
        
        cursor.execute('''
            INSERT INTO organization_licenses (id, organization_id, token_key, started_at, expires_at, validity_days, status, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, 'Valid', CURRENT_TIMESTAMP)
            ON CONFLICT(id) DO UPDATE SET
                token_key = excluded.token_key,
                started_at = excluded.started_at,
                expires_at = excluded.expires_at,
                validity_days = excluded.validity_days,
                updated_at = CURRENT_TIMESTAMP
        ''', (tok_id, org_id, token_key, starts_at, expires_at, validity_days))

        cursor.execute('''
            UPDATE branches
            SET token_parent_id = ?,
                token_started_at = ?,
                token_expires_at = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE organization_id = ?
        ''', (tok_id, starts_at, expires_at, org_id))

        cursor.execute('''
            INSERT INTO platform_audit_logs (organization_id, action, message)
            VALUES (?, 'TOKEN_ROTATED', ?)
        ''', (org_id, f"Rotated license token for '{org['name']}'; valid {starts_at} through {expires_at}"))

        conn.commit()
        return {
            "tokenId": tok_id,
            "tokenKey": token_key,
            "tokenStartedAt": starts_at,
            "tokenExpiresAt": expires_at,
            "tokenValidityDays": validity_days
        }

def update_branch_access(org_id: str, branch_id: str, access: str, suspension: Optional[Dict[str, Any]] = None) -> bool:
    """Update branch access status and suspension settings."""
    with get_connection() as conn:
        cursor = conn.cursor()
        s_kind = suspension.get('kind') if suspension else None
        s_days = suspension.get('days') if suspension else None
        s_start = suspension.get('startedAt') if suspension else None
        s_end = suspension.get('endsAt') if suspension else None

        cursor.execute('''
            UPDATE branches
            SET access = ?,
                suspension_kind = ?,
                suspension_days = ?,
                suspension_started_at = ?,
                suspension_ends_at = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE organization_id = ? AND id = ?
        ''', (access, s_kind, s_days, s_start, s_end, org_id, branch_id))

        cursor.execute('''
            INSERT INTO platform_audit_logs (organization_id, branch_id, action, message)
            VALUES (?, ?, 'BRANCH_ACCESS_UPDATED', ?)
        ''', (org_id, branch_id, f"Branch '{branch_id}' access set to {access}"))
        conn.commit()
        return True

def reactivate_branch(org_id: str, branch_id: str) -> bool:
    """Reactivate a suspended branch."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            UPDATE branches
            SET access = 'Active',
                suspension_kind = NULL,
                suspension_days = 0,
                suspension_started_at = NULL,
                suspension_ends_at = NULL,
                updated_at = CURRENT_TIMESTAMP
            WHERE organization_id = ? AND id = ?
        ''', (org_id, branch_id))

        cursor.execute('''
            INSERT INTO platform_audit_logs (organization_id, branch_id, action, message)
            VALUES (?, ?, 'BRANCH_REACTIVATED', ?)
        ''', (org_id, branch_id, f"Reactivated branch '{branch_id}'"))
        conn.commit()
        return True

def update_branch_grace(org_id: str, branch_id: str, grace_days: int) -> bool:
    """Update grace period days for a branch."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            UPDATE branches
            SET grace_days = ?, updated_at = CURRENT_TIMESTAMP
            WHERE organization_id = ? AND id = ?
        ''', (grace_days, org_id, branch_id))
        conn.commit()
        return True

def update_organization_branding(org_id: str, app_title: str, logo_url: str = "") -> bool:
    """Update branding for an organization."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO organization_branding (organization_id, app_title, logo_url, updated_at)
            VALUES (?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(organization_id) DO UPDATE SET
                app_title = excluded.app_title,
                logo_url = excluded.logo_url,
                updated_at = CURRENT_TIMESTAMP
        ''', (org_id, app_title, logo_url))

        cursor.execute('''
            INSERT INTO platform_audit_logs (organization_id, action, message)
            VALUES (?, 'BRANDING_UPDATED', ?)
        ''', (org_id, f"Updated branding title to '{app_title}'"))
        conn.commit()
        return True


# ==============================================================================
# BUILDS CRUD HELPERS
# ==============================================================================

def get_all_builds() -> List[Dict[str, Any]]:
    """Retrieve all application builds."""
    default_cloud_url = os.environ.get("CLIENT_DASHBOARD_URL") or os.environ.get("LOCAL_DASHBOARD_URL") or "https://quanteye-ai-attendence-production.up.railway.app"
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM application_builds ORDER BY created_at DESC")
        builds = []
        import json
        for r in cursor.fetchall():
            b = dict(r)
            try:
                b['modules'] = json.loads(b.get('modules_snapshot') or '[]')
            except Exception:
                b['modules'] = []
            
            manifest = {}
            if b.get('download_manifest_json'):
                try:
                    manifest = json.loads(b['download_manifest_json'])
                except Exception:
                    manifest = {}
            
            b['organizationId'] = b['organization_id']
            b['organizationName'] = b['organization_name']
            b['branchId'] = b['branch_id']
            b['branchName'] = b['branch_name']
            b['appName'] = b['app_name']
            b['clientPortalUrl'] = manifest.get('client_portal_url') or default_cloud_url
            b['clientUsername'] = manifest.get('client_username') or ""
            b['clientPassword'] = ""
            b['clientPasswordSet'] = bool(manifest.get('client_password_hash'))
            b['branchKey'] = ""
            builds.append(b)
        return builds


def _migrate_build_manifest_credentials(cursor: sqlite3.Cursor) -> None:
    """Hash legacy build passwords and remove credentials from stored manifests."""
    cursor.execute("SELECT id, download_manifest_json FROM application_builds WHERE download_manifest_json IS NOT NULL")
    for build_id, raw_manifest in cursor.fetchall():
        try:
            manifest = json.loads(raw_manifest)
        except (TypeError, ValueError):
            logger.warning("Skipping malformed build manifest during credential migration: %s", build_id)
            continue
        if not isinstance(manifest, dict):
            continue
        password = manifest.pop("client_password", None)
        if password and not manifest.get("client_password_hash"):
            manifest["client_password_hash"] = generate_password_hash(str(password))
        manifest.pop("branch_key", None)
        cursor.execute(
            "UPDATE application_builds SET download_manifest_json = ? WHERE id = ?",
            (json.dumps(manifest), build_id),
        )


def authenticate_client_build(username: str, password: str) -> Optional[Dict[str, Any]]:
    """Return a build's scope after verifying its stored password hash."""
    clean_username = username.strip().lower()
    if not clean_username or not password:
        return None
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """SELECT id, organization_id, branch_id, modules_snapshot, download_manifest_json
               FROM application_builds ORDER BY created_at DESC"""
        )
        for row in cursor.fetchall():
            try:
                manifest = json.loads(row["download_manifest_json"] or "{}")
            except (TypeError, ValueError):
                continue
            if not isinstance(manifest, dict):
                continue
            if str(manifest.get("client_username") or "").strip().lower() != clean_username:
                continue
            password_hash = manifest.get("client_password_hash")
            if password_hash and check_password_hash(str(password_hash), password):
                try:
                    modules = json.loads(row["modules_snapshot"] or "[]")
                except (TypeError, ValueError):
                    modules = []
                return {
                    "id": row["id"],
                    "organization_id": row["organization_id"],
                    "branch_id": row["branch_id"],
                    "modules": modules if isinstance(modules, list) else [],
                }
    return None

def create_build(data: Dict[str, Any]) -> Dict[str, Any]:
    """Create a new application build job with cloud URL and client credentials."""
    default_cloud_url = os.environ.get("CLIENT_DASHBOARD_URL") or os.environ.get("LOCAL_DASHBOARD_URL") or "https://quanteye-ai-attendence-production.up.railway.app"
    with get_connection() as conn:
        cursor = conn.cursor()
        import json
        build_id = data.get('id') or f"BUILD-{int(datetime.now().timestamp()*1000)}"
        org_id = data.get('organizationId') or data.get('organization_id')
        org_name = data.get('organizationName') or data.get('organization_name', '')
        branch_id = data.get('branchId') or data.get('branch_id')
        branch_name = data.get('branchName') or data.get('branch_name', '')
        app_name = data.get('appName') or data.get('app_name', 'QIntellect Application')
        version = data.get('version', 'v1.0.1')
        modules_list = data.get('modules', [])
        status = data.get('status', 'queued')
        progress = int(data.get('progress', 0) or 0)

        if not org_id or not branch_id:
            raise ValueError("An organization and branch are required for a build.")

        # Resolve the target branch before creating a build so invalid IDs fail clearly.
        cursor.execute(
            "SELECT name, token_key FROM branches WHERE organization_id = ? AND id = ?",
            (org_id, branch_id),
        )
        branch_row = cursor.fetchone()
        if not branch_row:
            raise ValueError("The selected branch does not belong to this organization.")
        branch_name = branch_row["name"]
        branch_key = branch_row['token_key'] or (data.get('branchKey') or f"QE-{uuid.uuid4().hex[:16].upper()}")

        org_row = get_organization_by_id(org_id)
        client_portal_url = (
            (org_row or {}).get("clientPortalUrl")
            or company_portal_url(org_name, org_id)
        )
        client_username = data.get('clientUsername') or data.get('email') or f"admin@{org_name.lower().replace(' ', '')}.com"
        client_password = str(data.get('clientPassword') or '').strip()
        if len(client_password) < 14:
            raise ValueError("Client password must contain at least 14 characters.")

        manifest_data = {
            "app_name": app_name,
            "version": version,
            "organization_id": org_id,
            "organization_name": org_name,
            "branch_id": branch_id,
            "branch_name": branch_name,
            "client_portal_url": client_portal_url,
            "client_username": client_username,
            "client_password_hash": generate_password_hash(client_password),
            "modules": modules_list,
            "created_at": datetime.now().isoformat()
        }

        cursor.execute('''
            INSERT INTO application_builds (id, organization_id, organization_name, branch_id, branch_name,
                                           app_name, version, modules_snapshot, status, progress, download_manifest_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        ''', (build_id, org_id, org_name, branch_id, branch_name, app_name, version, json.dumps(modules_list), status, progress, json.dumps(manifest_data)))

        cursor.execute('''
            INSERT INTO platform_audit_logs (organization_id, branch_id, action, message)
            VALUES (?, ?, 'BUILD_CREATED', ?)
        ''', (org_id, branch_id, f"Build {version} queued for {org_name} · {branch_name} (Cloud Portal: {client_portal_url})"))
        conn.commit()

    builds = [b for b in get_all_builds() if b['id'] == build_id]
    return builds[0] if builds else {}

def update_build(build_id: str, data: Dict[str, Any]) -> bool:
    """Update build progress or status."""
    with get_connection() as conn:
        cursor = conn.cursor()
        status = data.get('status')
        progress = data.get('progress')
        cursor.execute('''
            UPDATE application_builds
            SET status = COALESCE(?, status),
                progress = COALESCE(?, progress),
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
        ''', (status, progress, build_id))
        conn.commit()
        return True

def delete_build(build_id: str) -> bool:
    """Delete a build record."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM application_builds WHERE id = ?", (build_id,))
        cursor.execute('''
            INSERT INTO platform_audit_logs (action, message)
            VALUES ('BUILD_DELETED', ?)
        ''', (f"Deleted build {build_id}",))
        conn.commit()
        return True


# ==============================================================================
# INVOICES & PAYMENTS CRUD HELPERS
# ==============================================================================

def get_all_invoices() -> List[Dict[str, Any]]:
    """Retrieve all invoices with line items and payments."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM invoices ORDER BY created_at DESC")
        invoices = []
        for inv_row in cursor.fetchall():
            inv = dict(inv_row)
            cursor.execute("SELECT * FROM invoice_items WHERE invoice_id = ? ORDER BY sort_order ASC", (inv['id'],))
            inv['items'] = [dict(it) for it in cursor.fetchall()]
            cursor.execute("SELECT * FROM payments WHERE invoice_id = ? ORDER BY payment_date DESC", (inv['id'],))
            inv['payments'] = [dict(p) for p in cursor.fetchall()]
            
            # Alias field names matching frontend expectations
            inv['organizationId'] = inv['organization_id']
            inv['organizationName'] = inv['organization_name']
            inv['branchId'] = inv['branch_id']
            inv['branchName'] = inv['branch_name']
            inv['clientName'] = inv['client_name']
            inv['clientEmail'] = inv['client_email']
            inv['clientPhone'] = inv['client_phone']
            inv['billingAddress'] = inv['billing_address']
            inv['taxDetails'] = inv['tax_details']
            inv['issueDate'] = inv['issue_date']
            inv['dueDate'] = inv['due_date']
            inv['taxRate'] = inv['tax_rate']
            inv['paymentMethod'] = inv['payment_method']
            inv['number'] = inv['invoice_number']
            invoices.append(inv)
        return invoices

def create_invoice(data: Dict[str, Any]) -> Dict[str, Any]:
    """Create a new invoice and line items."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM invoices")
        count = cursor.fetchone()[0]
        now = datetime.now()
        inv_id = data.get('id') or f"INV-{int(now.timestamp()*1000)}"
        inv_number = data.get('number') or f"QIN-{now.year}-{count + 1:04d}"
        org_id = data.get('organizationId') or data.get('organization_id')
        org_name = data.get('organizationName') or data.get('organization_name', '')
        branch_id = data.get('branchId') or data.get('branch_id')
        branch_name = data.get('branchName') or data.get('branch_name', '')
        client_name = data.get('clientName') or org_name
        client_email = data.get('clientEmail') or data.get('email', '')
        client_phone = data.get('clientPhone') or data.get('phone', '')
        billing_addr = data.get('billingAddress') or data.get('address', '')
        tax_details = data.get('taxDetails') or 'NTN: J756903'
        issue_date = data.get('issueDate') or now.strftime('%Y-%m-%d')
        due_date = data.get('dueDate') or now.strftime('%Y-%m-%d')
        subtotal = float(data.get('subtotal', 0) or 0)
        discount = float(data.get('discount', 0) or 0)
        tax_rate = float(data.get('taxRate', 0) or 0)
        tax = float(data.get('tax', 0) or 0)
        total = float(data.get('total', 0) or 0)
        paid = float(data.get('paid', 0) or 0)
        status = data.get('status', 'Pending')
        notes = data.get('notes', '')

        cursor.execute('''
            INSERT INTO invoices (id, invoice_number, organization_id, organization_name, branch_id, branch_name,
                                 client_name, client_email, client_phone, billing_address, tax_details,
                                 issue_date, due_date, subtotal, discount, tax_rate, tax, total, paid, status, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (inv_id, inv_number, org_id, org_name, branch_id, branch_name,
              client_name, client_email, client_phone, billing_addr, tax_details,
              issue_date, due_date, subtotal, discount, tax_rate, tax, total, paid, status, notes))

        items = data.get('items', [])
        for idx, it in enumerate(items):
            desc = it.get('description', 'Service')
            qty = int(it.get('quantity', 1) or 1)
            price = float(it.get('unitPrice', 0) or 0)
            amount = qty * price
            cursor.execute('''
                INSERT INTO invoice_items (invoice_id, description, quantity, unit_price, amount, sort_order)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (inv_id, desc, qty, price, amount, idx))

        cursor.execute('''
            INSERT INTO platform_audit_logs (organization_id, branch_id, action, message)
            VALUES (?, ?, 'INVOICE_CREATED', ?)
        ''', (org_id, branch_id, f"Invoice {inv_number} created for {org_name} / {branch_name}"))
        conn.commit()

    invoices = [inv for inv in get_all_invoices() if inv['id'] == inv_id]
    return invoices[0] if invoices else {}

def update_invoice(invoice_id: str, data: Dict[str, Any]) -> bool:
    """Update due date, notes, status, or branch assignment for an invoice."""
    with get_connection() as conn:
        cursor = conn.cursor()
        due_date = data.get('dueDate')
        status = data.get('status')
        notes = data.get('notes')
        branch_id = data.get('branchId')
        branch_name = data.get('branchName')
        cursor.execute('''
            UPDATE invoices
            SET due_date = COALESCE(?, due_date),
                status = COALESCE(?, status),
                notes = COALESCE(?, notes),
                branch_id = COALESCE(?, branch_id),
                branch_name = COALESCE(?, branch_name),
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
        ''', (due_date, status, notes, branch_id, branch_name, invoice_id))
        conn.commit()
        return True

def record_payment(invoice_id: str, data: Dict[str, Any]) -> bool:
    """Record an unverified payment against an invoice."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT total, paid, invoice_number, organization_id, branch_id FROM invoices WHERE id = ?", (invoice_id,))
        row = cursor.fetchone()
        if not row:
            return False
        amount = float(data.get('amount', 0) or 0)
        method = data.get('method', 'Bank transfer')
        ref = data.get('reference', '')
        p_date = data.get('date') or datetime.now().strftime('%Y-%m-%d')
        new_paid = float(row['paid']) + amount
        new_status = "Paid" if new_paid >= float(row['total']) else "Partial"

        cursor.execute('''
            INSERT INTO payments (invoice_id, amount, method, reference, payment_date, verified)
            VALUES (?, ?, ?, ?, ?, 0)
        ''', (invoice_id, amount, method, ref, p_date))

        cursor.execute('''
            UPDATE invoices
            SET paid = ?,
                payment_method = ?,
                status = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
        ''', (new_paid, method, new_status, invoice_id))

        cursor.execute('''
            INSERT INTO platform_audit_logs (organization_id, branch_id, action, message)
            VALUES (?, ?, 'PAYMENT_RECORDED', ?)
        ''', (row['organization_id'], row['branch_id'], f"Payment of {amount} recorded for {row['invoice_number']} (unverified)"))
        conn.commit()
        return True


# ==============================================================================
# AUDIT LOG HELPERS
# ==============================================================================

def record_audit_log(message: str, org_id: Optional[str] = None, branch_id: Optional[str] = None, action: str = 'ACTION') -> bool:
    """Record an audit log message."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO platform_audit_logs (organization_id, branch_id, action, message)
            VALUES (?, ?, ?, ?)
        ''', (org_id, branch_id, action, message))
        conn.commit()
        return True

def get_all_audit_logs(limit: int = 50) -> List[Dict[str, Any]]:
    """Retrieve platform audit events."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT message, created_at as at FROM platform_audit_logs ORDER BY created_at DESC LIMIT ?", (limit,))
        return [dict(r) for r in cursor.fetchall()]

# ==============================================================================
# CONTROL-PLANE USERS & AUTHENTICATION (STEP 6)
# ==============================================================================

def get_user_by_email(email: str) -> Optional[Dict[str, Any]]:
    """Fetch user by email address."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM control_plane_users WHERE LOWER(email) = LOWER(?)", (email.strip(),))
        row = cursor.fetchone()
        return dict(row) if row else None

def get_user_by_id(user_id: str) -> Optional[Dict[str, Any]]:
    """Fetch user by user ID."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM control_plane_users WHERE id = ?", (user_id,))
        row = cursor.fetchone()
        return dict(row) if row else None

def create_control_plane_user(user_data: Dict[str, Any]) -> Dict[str, Any]:
    """Create a new control plane user with hashed password."""
    user_id = user_data.get('id') or f"USR-{uuid.uuid4().hex[:8].upper()}"
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO control_plane_users (id, name, email, password_hash, role, organization_id, active)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (
            user_id,
            user_data['name'].strip(),
            user_data['email'].strip().lower(),
            user_data['password_hash'],
            user_data.get('role', 'organization_admin'),
            user_data.get('organization_id'),
            int(user_data.get('active', 1))
        ))
        conn.commit()
    return get_user_by_id(user_id)

def update_user_last_login(user_id: str) -> bool:
    """Update last_login_at timestamp for a user."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE control_plane_users SET last_login_at = CURRENT_TIMESTAMP WHERE id = ?", (user_id,))
        conn.commit()
        return True

def ensure_default_users() -> None:
    """Bootstrap administrator accounts only from explicitly configured credentials."""
    bootstrap_users = (
        ("PLATFORM_ADMIN_EMAIL", "PLATFORM_ADMIN_PASSWORD", "USR-PLATFORM-ADMIN", "Platform Administrator", "platform_admin", None),
        ("ORGANIZATION_ADMIN_EMAIL", "ORGANIZATION_ADMIN_PASSWORD", "USR-ORG-ADMIN", "Organization Administrator", "organization_admin", os.environ.get("ORGANIZATION_ADMIN_ORG_ID")),
    )
    with get_connection() as conn:
        cursor = conn.cursor()
        configured_emails = []
        for email_env, password_env, user_id, name, role, organization_id in bootstrap_users:
            email = os.environ.get(email_env, "").strip().lower()
            password = os.environ.get(password_env, "")
            if bool(email) != bool(password):
                raise ValueError(f"Both {email_env} and {password_env} must be configured together.")
            if role == "organization_admin" and email and not organization_id:
                raise ValueError("ORGANIZATION_ADMIN_ORG_ID is required with organization admin credentials.")
            if not email:
                continue
            configured_emails.append(email)
            cursor.execute("SELECT id FROM control_plane_users WHERE LOWER(email) = ?", (email,))
            existing = cursor.fetchone()
            password_hash = generate_password_hash(password)
            if existing:
                cursor.execute(
                    "UPDATE control_plane_users SET password_hash = ?, role = ?, organization_id = ?, active = 1 WHERE id = ?",
                    (password_hash, role, organization_id, existing["id"]),
                )
            else:
                cursor.execute("SELECT 1 FROM control_plane_users WHERE id = ?", (user_id,))
                if cursor.fetchone():
                    user_id = f"{user_id}-{uuid.uuid4().hex[:12].upper()}"
                cursor.execute(
                    "INSERT INTO control_plane_users (id, name, email, password_hash, role, organization_id, active) VALUES (?, ?, ?, ?, ?, ?, 1)",
                    (user_id, name, email, password_hash, role, organization_id),
                )
        for email in ("admin@qintellect.com", "admin@forknknives.com"):
            if email not in configured_emails:
                cursor.execute("UPDATE control_plane_users SET active = 0 WHERE LOWER(email) = ?", (email,))
        conn.commit()
def get_branch_cameras(organization_id: str, branch_id: str) -> List[Dict[str, Any]]:
    """Retrieve all configured cameras for a branch."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT * FROM branch_cameras
            WHERE organization_id = ? AND branch_id = ? AND active = 1
            ORDER BY channel_number ASC, created_at ASC
        ''', (organization_id, branch_id))
        return [dict(row) for row in cursor.fetchall()]

def set_branch_cameras(organization_id: str, branch_id: str, cameras: List[Dict[str, Any]]) -> bool:
    """Replace/Update configured cameras for a branch from Cloud."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('DELETE FROM branch_cameras WHERE organization_id = ? AND branch_id = ?', (organization_id, branch_id))
        for idx, cam in enumerate(cameras, start=1):
            cam_id = cam.get('id') or f"cam_{idx}"
            cursor.execute('''
                INSERT INTO branch_cameras (
                    id, organization_id, branch_id, name, camera_type, rtsp_url, room_name, channel_number, active
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                cam_id,
                organization_id,
                branch_id,
                cam.get('name') or f"Camera {idx}",
                cam.get('camera_type', 'rtsp'),
                cam.get('rtsp_url', ''),
                cam.get('room_name', 'Main Gate'),
                cam.get('channel_number', idx),
                1 if cam.get('active', True) else 0
            ))
        conn.commit()
        return True

def record_live_attendance_sync_event(event_data: Dict[str, Any]) -> int:
    """Ingest a real-time attendance event pushed from a Client Node."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO live_attendance_sync_events (
                organization_id, branch_id, camera_id, camera_name,
                user_id, name, confidence, status, source, face_crop_base64, timestamp
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            event_data.get('organization_id'),
            event_data.get('branch_id'),
            event_data.get('camera_id'),
            event_data.get('camera_name'),
            event_data.get('user_id'),
            event_data.get('name', 'Unknown'),
            event_data.get('confidence'),
            event_data.get('status', 'Present'),
            event_data.get('source', 'ai_camera'),
            event_data.get('face_crop_base64'),
            event_data.get('timestamp') or datetime.now().isoformat()
        ))
        conn.commit()
        return cursor.lastrowid


def _live_attendance_event_hash(event_data: Dict[str, Any]) -> str:
    hashed_payload = {
        key: value for key, value in event_data.items() if key != "event_id"
    }
    return hashlib.sha256(
        json.dumps(
            hashed_payload, sort_keys=True, separators=(",", ":"), default=str
        ).encode("utf-8")
    ).hexdigest()


def get_live_attendance_sync_event_by_source_id(source_event_id: str) -> Optional[Dict[str, Any]]:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT attendance_event_id AS id, payload_hash "
            "FROM live_attendance_sync_event_receipts WHERE source_event_id = ?",
            (source_event_id,),
        ).fetchone()
        return dict(row) if row else None


def record_live_attendance_sync_event_once(
    event_data: Dict[str, Any],
    source_event_id: str,
) -> Tuple[int, bool, bool]:
    """Persist a client event once and detect reuse of its ID with changed data."""
    payload_hash = _live_attendance_event_hash(event_data)
    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        cursor = conn.cursor()
        existing = cursor.execute(
            "SELECT attendance_event_id, payload_hash "
            "FROM live_attendance_sync_event_receipts WHERE source_event_id = ?",
            (source_event_id,),
        ).fetchone()
        if existing:
            conn.commit()
            return int(existing["attendance_event_id"]), False, existing["payload_hash"] == payload_hash

        cursor.execute('''
            INSERT INTO live_attendance_sync_events (
                organization_id, branch_id, camera_id, camera_name,
                user_id, name, confidence, status, source, face_crop_base64, timestamp
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            event_data.get('organization_id'),
            event_data.get('branch_id'),
            event_data.get('camera_id'),
            event_data.get('camera_name'),
            event_data.get('user_id'),
            event_data.get('name', 'Unknown'),
            event_data.get('confidence'),
            event_data.get('status', 'Present'),
            event_data.get('source', 'ai_camera'),
            event_data.get('face_crop_base64'),
            event_data.get('timestamp') or datetime.now().isoformat(),
        ))
        event_id = int(cursor.lastrowid)
        cursor.execute(
            "INSERT INTO live_attendance_sync_event_receipts "
            "(source_event_id, payload_hash, attendance_event_id) VALUES (?, ?, ?)",
            (source_event_id, payload_hash, event_id),
        )
        conn.commit()
        return event_id, True, True


def enqueue_attendance_postgres_sync(
    event_id: str,
    organization_id: str,
    branch_id: str,
    payload: Dict[str, Any],
) -> None:
    with get_connection() as conn:
        conn.execute(
            """INSERT OR IGNORE INTO attendance_postgres_sync_queue
               (source_event_id, organization_id, branch_id, payload_json)
               VALUES (?, ?, ?, ?)""",
            (
                event_id,
                organization_id,
                branch_id,
                json.dumps(payload, separators=(",", ":"), sort_keys=True),
            ),
        )
        conn.commit()


def get_due_attendance_postgres_sync(limit: int = 25) -> List[Dict[str, Any]]:
    with get_connection() as conn:
        rows = conn.execute(
            """SELECT source_event_id, organization_id, branch_id, payload_json, attempts
               FROM attendance_postgres_sync_queue
               WHERE status = 'pending'
                 AND (next_attempt_at IS NULL OR next_attempt_at <= datetime('now', 'localtime'))
               ORDER BY created_at, source_event_id
               LIMIT ?""",
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]


def mark_attendance_postgres_sync_succeeded(event_id: str) -> None:
    with get_connection() as conn:
        conn.execute(
            """UPDATE attendance_postgres_sync_queue
               SET status = 'synced', synced_at = CURRENT_TIMESTAMP,
                   last_attempt_at = CURRENT_TIMESTAMP, last_error = NULL
               WHERE source_event_id = ?""",
            (event_id,),
        )
        conn.commit()


def mark_attendance_postgres_sync_failed(event_id: str, error: str) -> None:
    with get_connection() as conn:
        conn.execute(
            """UPDATE attendance_postgres_sync_queue
               SET attempts = attempts + 1,
                   next_attempt_at = datetime(
                       'now', 'localtime',
                       '+' || MIN(300, (2 << MIN(attempts, 7))) || ' seconds'
                   ),
                   last_attempt_at = CURRENT_TIMESTAMP, last_error = ?
               WHERE source_event_id = ? AND status = 'pending'""",
            (error[:500], event_id),
        )
        conn.commit()

def get_live_attendance_sync_events(organization_id: str, branch_id: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
    """Retrieve recent live attendance events for an organization."""
    with get_connection() as conn:
        cursor = conn.cursor()
        if branch_id:
            cursor.execute('''
                SELECT * FROM live_attendance_sync_events
                WHERE organization_id = ? AND branch_id = ?
                ORDER BY id DESC LIMIT ?
            ''', (organization_id, branch_id, limit))
        else:
            cursor.execute('''
                SELECT * FROM live_attendance_sync_events
                WHERE organization_id = ?
                ORDER BY id DESC LIMIT ?
            ''', (organization_id, limit))
        return [dict(row) for row in cursor.fetchall()]


def get_branch_by_token_key(token_key: str) -> Optional[Dict[str, Any]]:
    """Look up a branch and its parent organization by the branch token key (case-insensitive, trimmed)."""
    clean_key = str(token_key or '').strip()
    if not clean_key:
        return None
    if _postgres_control_plane_ready():
        try:
            pg_branch = cloud_db.get_branch_by_token_key(clean_key)
            if pg_branch:
                return pg_branch
        except Exception as error:
            logger.warning("PostgreSQL branch-key lookup failed; using SQLite: %s", error)
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT 
                b.id AS branch_id,
                b.name AS branch_name,
                b.organization_id,
                o.name AS organization_name,
                b.access AS branch_access,
                b.token_key,
                b.token_expires_at,
                o.status AS organization_status
            FROM branches b
            JOIN organizations o ON b.organization_id = o.id
            WHERE LOWER(TRIM(b.token_key)) = LOWER(?)
        ''', (clean_key,))
        row = cursor.fetchone()
        return dict(row) if row else None


if __name__ == "__main__":
    init_db()
    ensure_default_users()
    print("✓ QuantEye-Main-Dashboard database.py initialized successfully.")
