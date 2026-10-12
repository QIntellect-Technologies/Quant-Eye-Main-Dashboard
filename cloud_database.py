"""
QuantEye Cloud Multi-Tenant Persistence & Row-Level Security Layer
Phase 2.1 — Cloud PostgreSQL Foundation
Phase 2.2 — Cloud Tenant Authentication + RLS
"""

import os
import uuid
import json
import logging
from datetime import datetime, timezone
from contextlib import contextmanager
from typing import Dict, List, Optional, Any, Tuple
import psycopg2
from psycopg2 import pool
from psycopg2.extras import RealDictCursor
from werkzeug.security import generate_password_hash, check_password_hash

logger = logging.getLogger("quanteye.cloud_db")
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("[%(asctime)s] %(levelname)s [%(name)s] %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)

# =====================================================================
# Configuration
# =====================================================================
PG_HOST = os.environ.get("PGHOST", "127.0.0.1")
PG_PORT = int(os.environ.get("PGPORT", "5432"))
PG_DATABASE = os.environ.get("PGDATABASE", "quanteye_cloud")
PG_USER = os.environ.get("PGUSER", "postgres")
PG_PASSWORD = os.environ.get("PGPASSWORD", "")

# Application Runtime Role credentials
APP_RUNTIME_ROLE = "app_runtime_role"

_pool: Optional[pool.ThreadedConnectionPool] = None

def get_connection_params(user: Optional[str] = None, password: Optional[str] = None) -> Dict[str, Any]:
    return {
        "host": PG_HOST,
        "port": PG_PORT,
        "dbname": PG_DATABASE,
        "user": user or PG_USER,
        "password": password or PG_PASSWORD,
    }

def get_raw_connection(user: Optional[str] = None, password: Optional[str] = None):
    params = get_connection_params(user, password)
    return psycopg2.connect(**params)

def init_cloud_pool(minconn: int = 1, maxconn: int = 20) -> pool.ThreadedConnectionPool:
    global _pool
    if _pool is None:
        params = get_connection_params()
        try:
            _pool = pool.ThreadedConnectionPool(minconn, maxconn, **params)
            logger.info("Initialized PostgreSQL connection pool (%s:%s/%s)", PG_HOST, PG_PORT, PG_DATABASE)
        except Exception as e:
            logger.warning("Could not initialize PostgreSQL connection pool: %s", e)
            raise
    return _pool

# =====================================================================
# Cloud User Representation
# =====================================================================
class CloudUser:
    """Represents an authenticated cloud control-plane user with tenant & scope boundaries."""
    def __init__(
        self,
        user_id: str,
        organization_id: str,
        role: str,
        username: str,
        email: str,
        is_active: bool = True,
        permitted_branches: Optional[List[str]] = None
    ):
        self.user_id = user_id
        self.organization_id = organization_id
        self.role = role  # platform_admin, org_admin, branch_manager, auditor
        self.username = username
        self.email = email
        self.is_active = is_active
        self.permitted_branches = permitted_branches or []

    @property
    def is_platform_admin(self) -> bool:
        return self.role == "platform_admin"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "user_id": self.user_id,
            "organization_id": self.organization_id,
            "role": self.role,
            "username": self.username,
            "email": self.email,
            "is_active": self.is_active,
            "permitted_branches": self.permitted_branches
        }

# =====================================================================
# Centralized Tenant Context & Session Manager
# =====================================================================
@contextmanager
def tenant_context(
    user: Optional[CloudUser] = None,
    org_id: Optional[str] = None,
    branch_id: Optional[str] = None,
    is_platform_admin: bool = False,
    enforce_rls: bool = True,
    custom_conn = None
):
    """
    Centralized Backend Helper:
    The mandatory flow:
    1. Authenticated user verified
    2. Verified organization scope
    3. Verified branch permissions
    4. PostgreSQL transaction started
    5. SET LOCAL tenant context (org_id, branch_id, is_platform_admin)
    6. App runtime role switch (NOBYPASSRLS)
    7. Query execution under active RLS enforcement
    8. Transaction commit / rollback
    """
    # Step 1: Verify Scope & Role
    effective_org_id = ""
    effective_branch_id = ""
    effective_is_platform_admin = False

    if user:
        if not user.is_active:
            raise PermissionError("User account is inactive or disabled.")

        if user.is_platform_admin:
            effective_is_platform_admin = True
            effective_org_id = org_id or user.organization_id or ""
            effective_branch_id = branch_id or ""
        else:
            # Never trust frontend-supplied org_id for non-platform admins
            effective_org_id = user.organization_id
            if user.role == "branch_manager":
                if branch_id:
                    if branch_id not in user.permitted_branches:
                        raise PermissionError(f"Access denied: User is not authorized for branch '{branch_id}'.")
                    effective_branch_id = branch_id
                else:
                    # Restrict to first permitted branch if single, or branch must be specified
                    if len(user.permitted_branches) == 1:
                        effective_branch_id = user.permitted_branches[0]
                    else:
                        effective_branch_id = ""
            elif user.role in ("org_admin", "auditor"):
                # Org admin / auditor can inspect specific branch if requested, or full org if blank
                effective_branch_id = branch_id or ""
            else:
                raise PermissionError(f"Unknown or unauthorized role: {user.role}")
    else:
        # Explicit context (e.g. system background worker or administrative scripts)
        effective_is_platform_admin = bool(is_platform_admin)
        effective_org_id = org_id or ""
        effective_branch_id = branch_id or ""

    # Step 2: Establish connection and transaction
    conn = custom_conn
    close_conn = False
    if conn is None:
        conn = get_raw_connection()
        close_conn = True

    try:
        with conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                # Step 3: Switch to app_runtime_role if enforcing RLS
                if enforce_rls:
                    cur.execute(f"SET ROLE {APP_RUNTIME_ROLE};")

                # Step 4: SET LOCAL transaction-level tenant context
                # Values reset automatically at transaction end
                cur.execute("SELECT set_config('app.current_org_id', %s, true);", (effective_org_id,))
                cur.execute("SELECT set_config('app.current_branch_id', %s, true);", (effective_branch_id,))
                cur.execute("SELECT set_config('app.is_platform_admin', %s, true);", (
                    "true" if effective_is_platform_admin else "false",
                ))

                # Step 5: Yield cursor to caller
                yield cur
                
                # If we switched roles, reset back to connection owner before commit/cleanup
                if enforce_rls:
                    cur.execute("RESET ROLE;")
    finally:
        if close_conn:
            conn.close()

# =====================================================================
# Database Schema Initialization & Migrations
# =====================================================================
def init_cloud_db(schema_sql_path: Optional[str] = None) -> bool:
    """Initialize cloud schema using superuser/admin connection."""
    if not schema_sql_path:
        schema_sql_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cloud_schema.sql")

    if not os.path.exists(schema_sql_path):
        raise FileNotFoundError(f"Cloud schema DDL not found at {schema_sql_path}")

    with open(schema_sql_path, "r", encoding="utf-8") as f:
        ddl = f.read()

    conn = get_raw_connection()
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(ddl)
        logger.info("Successfully executed cloud_schema.sql DDL.")

        # Seed Module Catalog
        seed_modules(conn)
        return True
    finally:
        conn.close()

def seed_modules(conn):
    canonical_modules = [
        ("attendance", "Attendance Management", "Track daily attendance, presence, and AI camera check-in activity.", "core", True),
        ("shifts", "Shift Management", "Configure shifts, rosters, and schedules.", "core", True),
        ("overtime", "Overtime Management", "Track overtime hours, policies, and approvals.", "core", True),
        ("leave", "Leave Management", "Handle leave balances, requests, and approvals.", "core", True),
        ("payroll", "Payroll Management", "Automated payroll processing based on attendance.", "core", True),
    ]
    with conn.cursor() as cur:
        for mod in canonical_modules:
            cur.execute("""
                INSERT INTO module_catalog (id, name, description, category, is_active)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (id) DO UPDATE SET
                    name = EXCLUDED.name,
                    description = EXCLUDED.description,
                    category = EXCLUDED.category,
                    is_active = EXCLUDED.is_active;
            """, mod)

# =====================================================================
# Cloud Authentication & User Management
# =====================================================================
def create_cloud_user(
    organization_id: str,
    username: str,
    email: str,
    password: str,
    role: str = "org_admin",
    branch_ids: Optional[List[str]] = None,
    user_id: Optional[str] = None
) -> Dict[str, Any]:
    """Create a new user with proper tenant isolation and role permissions."""
    valid_roles = ("platform_admin", "org_admin", "branch_manager", "auditor")
    if role not in valid_roles:
        raise ValueError(f"Invalid role '{role}'. Must be one of {valid_roles}")

    uid = user_id or f"usr_{uuid.uuid4().hex[:12]}"
    pwd_hash = generate_password_hash(password)

    # Use administrative/system context to insert user
    with tenant_context(org_id=organization_id, is_platform_admin=True, enforce_rls=False) as cur:
        cur.execute("""
            INSERT INTO organization_users (id, organization_id, username, email, password_hash, role, is_active)
            VALUES (%s, %s, %s, %s, %s, %s, TRUE)
            ON CONFLICT (organization_id, username) DO UPDATE SET
                password_hash = EXCLUDED.password_hash,
                role = EXCLUDED.role,
                is_active = TRUE
            RETURNING id, organization_id, username, email, role, is_active, created_at;
        """, (uid, organization_id, username, email.lower(), pwd_hash, role))
        row = cur.fetchone()

        # Insert branch memberships if role is branch_manager or branches provided
        if branch_ids:
            for bid in branch_ids:
                mem_id = f"ubm_{uuid.uuid4().hex[:10]}"
                cur.execute("""
                    INSERT INTO user_branch_memberships (id, organization_id, user_id, branch_id)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (organization_id, user_id, branch_id) DO NOTHING;
                """, (mem_id, organization_id, uid, bid))

        return dict(row)

def authenticate_cloud_user(email: str, password: str) -> Optional[CloudUser]:
    """Authenticate a cloud user and return CloudUser object with verified branch scope."""
    with tenant_context(is_platform_admin=True, enforce_rls=False) as cur:
        cur.execute("""
            SELECT id, organization_id, username, email, password_hash, role, is_active
            FROM organization_users
            WHERE LOWER(email) = LOWER(%s);
        """, (email,))
        row = cur.fetchone()

        if not row:
            return None

        if not check_password_hash(row["password_hash"], password):
            return None

        if not row["is_active"]:
            logger.warning("Authentication failed: User account %s is deactivated.", email)
            return None

        # Fetch permitted branches
        permitted_branches = []
        if row["role"] == "branch_manager":
            cur.execute("""
                SELECT branch_id
                FROM user_branch_memberships
                WHERE organization_id = %s AND user_id = %s;
            """, (row["organization_id"], row["id"]))
            permitted_branches = [b["branch_id"] for b in cur.fetchall()]

        return CloudUser(
            user_id=row["id"],
            organization_id=row["organization_id"],
            role=row["role"],
            username=row["username"],
            email=row["email"],
            is_active=row["is_active"],
            permitted_branches=permitted_branches
        )

# =====================================================================
# Persistence CRUD Operations (Multi-Tenant & RLS Protected)
# =====================================================================

# --- Organizations ---
def create_organization(
    org_id: str,
    name: str,
    industry: str = "Technology",
    email: str = "",
    phone: str = "",
    address: str = "",
    status: str = "Active",
    user_context: Optional[CloudUser] = None
) -> Dict[str, Any]:
    # Creating an organization is typically platform_admin or onboarding
    is_plat = user_context.is_platform_admin if user_context else True
    with tenant_context(user=user_context, org_id=org_id, is_platform_admin=is_plat) as cur:
        cur.execute("""
            INSERT INTO organizations (id, name, industry, email, phone, address, status)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (id) DO UPDATE SET
                name = EXCLUDED.name,
                industry = EXCLUDED.industry,
                email = EXCLUDED.email,
                phone = EXCLUDED.phone,
                address = EXCLUDED.address,
                status = EXCLUDED.status,
                updated_at = CURRENT_TIMESTAMP
            RETURNING *;
        """, (org_id, name, industry, email, phone, address, status))
        row = cur.fetchone()
        return dict(row)

def get_organization(org_id: str, user_context: Optional[CloudUser] = None) -> Optional[Dict[str, Any]]:
    with tenant_context(user=user_context, org_id=org_id) as cur:
        cur.execute("SELECT * FROM organizations WHERE id = %s;", (org_id,))
        row = cur.fetchone()
        return dict(row) if row else None

def list_organizations(user_context: Optional[CloudUser] = None) -> List[Dict[str, Any]]:
    with tenant_context(user=user_context) as cur:
        cur.execute("SELECT * FROM organizations ORDER BY name ASC;")
        return [dict(r) for r in cur.fetchall()]

# --- Branches ---
def create_branch(
    org_id: str,
    branch_id: str,
    name: str,
    city: str = "",
    address: str = "",
    access: str = "Active",
    grace_days: int = 0,
    employee_count: int = 0,
    token_key: Optional[str] = None,
    user_context: Optional[CloudUser] = None
) -> Dict[str, Any]:
    with tenant_context(user=user_context, org_id=org_id) as cur:
        cur.execute("""
            INSERT INTO branches (id, organization_id, name, city, address, access, grace_days, employee_count, token_key)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (organization_id, id) DO UPDATE SET
                name = EXCLUDED.name,
                city = EXCLUDED.city,
                address = EXCLUDED.address,
                access = EXCLUDED.access,
                grace_days = EXCLUDED.grace_days,
                employee_count = EXCLUDED.employee_count,
                token_key = EXCLUDED.token_key,
                updated_at = CURRENT_TIMESTAMP
            RETURNING *;
        """, (branch_id, org_id, name, city, address, access, grace_days, employee_count, token_key))
        row = cur.fetchone()
        return dict(row)

def get_branch(org_id: str, branch_id: str, user_context: Optional[CloudUser] = None) -> Optional[Dict[str, Any]]:
    with tenant_context(user=user_context, org_id=org_id, branch_id=branch_id) as cur:
        cur.execute("SELECT * FROM branches WHERE organization_id = %s AND id = %s;", (org_id, branch_id))
        row = cur.fetchone()
        return dict(row) if row else None

def list_branches(org_id: str, user_context: Optional[CloudUser] = None) -> List[Dict[str, Any]]:
    with tenant_context(user=user_context, org_id=org_id) as cur:
        cur.execute("SELECT * FROM branches WHERE organization_id = %s ORDER BY name ASC;", (org_id,))
        return [dict(r) for r in cur.fetchall()]

# --- Modules ---
def list_module_catalog(user_context: Optional[CloudUser] = None) -> List[Dict[str, Any]]:
    with tenant_context(user=user_context) as cur:
        cur.execute("SELECT * FROM module_catalog WHERE is_active = TRUE ORDER BY id ASC;")
        return [dict(r) for r in cur.fetchall()]

def get_organization_modules(org_id: str, user_context: Optional[CloudUser] = None) -> List[Dict[str, Any]]:
    with tenant_context(user=user_context, org_id=org_id) as cur:
        cur.execute("""
            SELECT om.module_id, om.is_enabled, mc.name, mc.description, mc.category
            FROM organization_modules om
            JOIN module_catalog mc ON om.module_id = mc.id
            WHERE om.organization_id = %s;
        """, (org_id,))
        return [dict(r) for r in cur.fetchall()]

def set_organization_module(
    org_id: str,
    module_id: str,
    is_enabled: bool = True,
    user_context: Optional[CloudUser] = None
) -> Dict[str, Any]:
    with tenant_context(user=user_context, org_id=org_id) as cur:
        om_id = f"om_{uuid.uuid4().hex[:10]}"
        cur.execute("""
            INSERT INTO organization_modules (id, organization_id, module_id, is_enabled)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (organization_id, module_id) DO UPDATE SET
                is_enabled = EXCLUDED.is_enabled
            RETURNING *;
        """, (om_id, org_id, module_id, is_enabled))
        row = cur.fetchone()
        return dict(row)

# --- Licenses ---
def create_organization_license(
    org_id: str,
    license_key: str,
    tier: str = "standard",
    max_branches: int = 1,
    max_employees: int = 50,
    starts_at: Optional[datetime] = None,
    expires_at: Optional[datetime] = None,
    status: str = "active",
    user_context: Optional[CloudUser] = None
) -> Dict[str, Any]:
    start = starts_at or datetime.now(timezone.utc)
    from datetime import timedelta
    expire = expires_at or (start + timedelta(days=365))
    lic_id = f"lic_{uuid.uuid4().hex[:10]}"

    with tenant_context(user=user_context, org_id=org_id) as cur:
        cur.execute("""
            INSERT INTO organization_licenses (id, organization_id, license_key, tier, max_branches, max_employees, starts_at, expires_at, status)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (license_key) DO UPDATE SET
                tier = EXCLUDED.tier,
                max_branches = EXCLUDED.max_branches,
                max_employees = EXCLUDED.max_employees,
                starts_at = EXCLUDED.starts_at,
                expires_at = EXCLUDED.expires_at,
                status = EXCLUDED.status,
                updated_at = CURRENT_TIMESTAMP
            RETURNING *;
        """, (lic_id, org_id, license_key, tier, max_branches, max_employees, start, expire, status))
        row = cur.fetchone()
        return dict(row)

def get_organization_licenses(org_id: str, user_context: Optional[CloudUser] = None) -> List[Dict[str, Any]]:
    with tenant_context(user=user_context, org_id=org_id) as cur:
        cur.execute("SELECT * FROM organization_licenses WHERE organization_id = %s;", (org_id,))
        return [dict(r) for r in cur.fetchall()]

# --- Branding ---
def set_organization_branding(
    org_id: str,
    company_name: str,
    logo_url: str = "",
    primary_color: str = "#4f46e5",
    accent_color: str = "#06b6d4",
    custom_css: str = "",
    user_context: Optional[CloudUser] = None
) -> Dict[str, Any]:
    with tenant_context(user=user_context, org_id=org_id) as cur:
        cur.execute("""
            INSERT INTO organization_branding (organization_id, company_name, logo_url, primary_color, accent_color, custom_css)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (organization_id) DO UPDATE SET
                company_name = EXCLUDED.company_name,
                logo_url = EXCLUDED.logo_url,
                primary_color = EXCLUDED.primary_color,
                accent_color = EXCLUDED.accent_color,
                custom_css = EXCLUDED.custom_css,
                updated_at = CURRENT_TIMESTAMP
            RETURNING *;
        """, (org_id, company_name, logo_url, primary_color, accent_color, custom_css))
        row = cur.fetchone()
        return dict(row)

def get_organization_branding(org_id: str, user_context: Optional[CloudUser] = None) -> Optional[Dict[str, Any]]:
    with tenant_context(user=user_context, org_id=org_id) as cur:
        cur.execute("SELECT * FROM organization_branding WHERE organization_id = %s;", (org_id,))
        row = cur.fetchone()
        return dict(row) if row else None

# --- Employees & Attendance (Tenant Isolation Verification Operations) ---
def create_employee(
    org_id: str,
    branch_id: str,
    employee_code: str,
    name: str,
    email: str = "",
    phone: str = "",
    user_context: Optional[CloudUser] = None
) -> Dict[str, Any]:
    emp_id = f"emp_{uuid.uuid4().hex[:10]}"
    with tenant_context(user=user_context, org_id=org_id, branch_id=branch_id) as cur:
        cur.execute("""
            INSERT INTO employees (id, organization_id, branch_id, employee_code, name, email, phone)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (organization_id, branch_id, employee_code) DO UPDATE SET
                name = EXCLUDED.name,
                email = EXCLUDED.email,
                phone = EXCLUDED.phone,
                updated_at = CURRENT_TIMESTAMP
            RETURNING *;
        """, (emp_id, org_id, branch_id, employee_code, name, email, phone))
        row = cur.fetchone()
        return dict(row)

def get_employees(org_id: str, branch_id: Optional[str] = None, user_context: Optional[CloudUser] = None) -> List[Dict[str, Any]]:
    with tenant_context(user=user_context, org_id=org_id, branch_id=branch_id) as cur:
        if branch_id:
            cur.execute("SELECT * FROM employees WHERE organization_id = %s AND branch_id = %s;", (org_id, branch_id))
        else:
            cur.execute("SELECT * FROM employees WHERE organization_id = %s;", (org_id,))
        return [dict(r) for r in cur.fetchall()]

def record_attendance_event(
    org_id: str,
    branch_id: str,
    employee_id: str,
    event_type: str = "CHECK_IN",
    confidence: float = 1.0,
    timestamp: Optional[datetime] = None,
    user_context: Optional[CloudUser] = None
) -> Dict[str, Any]:
    ev_id = f"att_{uuid.uuid4().hex[:12]}"
    ts = timestamp or datetime.now(timezone.utc)
    with tenant_context(user=user_context, org_id=org_id, branch_id=branch_id) as cur:
        cur.execute("""
            INSERT INTO attendance_events (id, organization_id, branch_id, employee_id, event_type, confidence, timestamp)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            RETURNING *;
        """, (ev_id, org_id, branch_id, employee_id, event_type, confidence, ts))
        row = cur.fetchone()
        return dict(row)

def get_attendance_events(org_id: str, branch_id: Optional[str] = None, user_context: Optional[CloudUser] = None) -> List[Dict[str, Any]]:
    with tenant_context(user=user_context, org_id=org_id, branch_id=branch_id) as cur:
        if branch_id:
            cur.execute("SELECT * FROM attendance_events WHERE organization_id = %s AND branch_id = %s ORDER BY timestamp DESC;", (org_id, branch_id))
        else:
            cur.execute("SELECT * FROM attendance_events WHERE organization_id = %s ORDER BY timestamp DESC;", (org_id,))
        return [dict(r) for r in cur.fetchall()]


# =====================================================================
# PHASE 2.3: EDGE NODE AUTHENTICATION & BATCH INGESTION
# =====================================================================

EDGE_JWT_SECRET = os.environ.get("EDGE_JWT_SECRET", "quanteye_cloud_edge_secret_key_v2_secure_38210")
EDGE_TOKEN_EXPIRY_SECONDS = 3600  # 1 hour

def register_edge_node(
    org_id: str,
    branch_id: str,
    node_name: str,
    secret: str,
    node_id: Optional[str] = None,
    hardware_uuid: Optional[str] = None,
    status: str = "online"
) -> Dict[str, Any]:
    """Register or update an edge node with hashed cryptographic secret."""
    n_id = node_id or f"edge_{uuid.uuid4().hex[:10]}"
    sec_hash = generate_password_hash(secret)
    with tenant_context(org_id=org_id, branch_id=branch_id, is_platform_admin=True, enforce_rls=False) as cur:
        cur.execute("""
            INSERT INTO edge_nodes (id, organization_id, branch_id, node_name, hardware_uuid, status, api_key_hash)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (organization_id, id) DO UPDATE SET
                node_name = EXCLUDED.node_name,
                hardware_uuid = COALESCE(EXCLUDED.hardware_uuid, edge_nodes.hardware_uuid),
                status = EXCLUDED.status,
                api_key_hash = EXCLUDED.api_key_hash,
                updated_at = CURRENT_TIMESTAMP
            RETURNING id, organization_id, branch_id, node_name, status, created_at;
        """, (n_id, org_id, branch_id, node_name, hardware_uuid, status, sec_hash))
        row = cur.fetchone()
        return dict(row)

def authenticate_edge_node(edge_node_id: str, secret: str) -> Optional[Dict[str, Any]]:
    """
    Authenticate edge node using ID and secret.
    Returns token payload dict if valid and active, else None.
    """
    import jwt
    with tenant_context(is_platform_admin=True, enforce_rls=False) as cur:
        cur.execute("""
            SELECT id, organization_id, branch_id, node_name, status, api_key_hash
            FROM edge_nodes
            WHERE id = %s;
        """, (edge_node_id,))
        node = cur.fetchone()

        if not node:
            return None

        if node["status"] != "online":
            logger.warning("Edge node '%s' authentication rejected: status is '%s'", edge_node_id, node["status"])
            return None

        if not check_password_hash(node["api_key_hash"], secret):
            logger.warning("Edge node '%s' authentication rejected: invalid secret", edge_node_id)
            return None

        # Update last heartbeat
        cur.execute("UPDATE edge_nodes SET last_heartbeat = CURRENT_TIMESTAMP WHERE id = %s;", (edge_node_id,))

        # Generate short-lived Edge JWT
        now = datetime.now(timezone.utc)
        payload = {
            "sub": node["id"],
            "org_id": node["organization_id"],
            "branch_id": node["branch_id"],
            "node_name": node["node_name"],
            "iat": int(now.timestamp()),
            "exp": int(now.timestamp()) + EDGE_TOKEN_EXPIRY_SECONDS
        }
        token = jwt.encode(payload, EDGE_JWT_SECRET, algorithm="HS256")
        return {
            "token": token,
            "expires_in": EDGE_TOKEN_EXPIRY_SECONDS,
            "organization_id": node["organization_id"],
            "branch_id": node["branch_id"],
            "edge_node_id": node["id"]
        }

def verify_edge_token(token: str) -> Optional[Dict[str, Any]]:
    """Verify an Edge JWT and check that the underlying node remains active."""
    import jwt
    try:
        decoded = jwt.decode(token, EDGE_JWT_SECRET, algorithms=["HS256"])
        node_id = decoded.get("sub")
        org_id = decoded.get("org_id")
        branch_id = decoded.get("branch_id")

        # Verify against database that node hasn't been revoked/deactivated since token issuance
        with tenant_context(org_id=org_id, is_platform_admin=True, enforce_rls=False) as cur:
            cur.execute("SELECT status FROM edge_nodes WHERE id = %s AND organization_id = %s;", (node_id, org_id))
            row = cur.fetchone()
            if not row or row["status"] != "online":
                return None

        return {
            "edge_node_id": node_id,
            "organization_id": org_id,
            "branch_id": branch_id,
            "node_name": decoded.get("node_name")
        }
    except Exception as e:
        logger.warning("Edge token verification failed: %s", e)
        return None

def ingest_edge_sync_batch(
    edge_node_auth: Dict[str, Any],
    batch_sequence: int,
    events: List[Dict[str, Any]]
) -> Dict[str, Any]:
    """
    Idempotently ingest an attendance sync batch.
    Enforces verified tenant scope (NEVER trusts client payload).
    """
    org_id = edge_node_auth["organization_id"]
    branch_id = edge_node_auth["branch_id"]
    edge_node_id = edge_node_auth["edge_node_id"]

    batch_id = f"BATCH-{org_id}-{branch_id}-{edge_node_id}-{batch_sequence}"
    processed_count = 0
    duplicate_count = 0

    with tenant_context(org_id=org_id, branch_id=branch_id, is_platform_admin=True, enforce_rls=False) as cur:
        # 1. Record/Update batch in edge_sync_batches
        cur.execute("""
            INSERT INTO edge_sync_batches (id, organization_id, branch_id, edge_node_id, batch_sequence, record_count, status, received_at)
            VALUES (%s, %s, %s, %s, %s, %s, 'received', CURRENT_TIMESTAMP)
            ON CONFLICT (organization_id, id) DO UPDATE SET
                record_count = EXCLUDED.record_count,
                status = 'received',
                received_at = CURRENT_TIMESTAMP;
        """, (batch_id, org_id, branch_id, edge_node_id, batch_sequence, len(events)))

        # 2. Ingest each event idempotently
        for ev in events:
            ev_id = ev.get("event_id") or f"ATT-{uuid.uuid4().hex}"
            payload = ev.get("payload") or {}

            # Fallback to payload fields if top-level event fields are inside payload
            u_name = payload.get("detected_name") or ev.get("detected_name") or "Employee"
            u_id = payload.get("user_id") or ev.get("user_id") or 1
            conf = float(payload.get("confidence") or ev.get("confidence") or 1.0)
            ev_type = ev.get("event_type") or payload.get("event_type") or "CHECK_IN"
            if ev_type not in ('CHECK_IN', 'CHECK_OUT', 'BREAK_IN', 'BREAK_OUT', 'RECOGNITION'):
                ev_type = "CHECK_IN"

            raw_ts = payload.get("timestamp") or ev.get("timestamp")
            if raw_ts:
                try:
                    ev_ts = datetime.fromisoformat(str(raw_ts))
                except Exception:
                    try:
                        ev_ts = datetime.strptime(str(raw_ts), "%Y-%m-%d %H:%M:%S")
                    except Exception:
                        ev_ts = datetime.now(timezone.utc)
            else:
                ev_ts = datetime.now(timezone.utc)

            # Look up if employee already exists by ID or by employee_code
            cur.execute("""
                SELECT id FROM employees 
                WHERE organization_id = %s AND (id = %s OR (branch_id = %s AND employee_code = %s))
                LIMIT 1;
            """, (org_id, f"EMP-{org_id}-{branch_id}-{u_id}", branch_id, f"EMP-{u_id}"))
            existing_emp = cur.fetchone()

            if existing_emp:
                emp_pk = existing_emp["id"]
            else:
                emp_pk = f"EMP-{org_id}-{branch_id}-{u_id}"
                # Ensure employee placeholder exists so foreign key is satisfied
                cur.execute("""
                    INSERT INTO employees (id, organization_id, branch_id, employee_code, name)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (organization_id, branch_id, employee_code) DO NOTHING;
                """, (emp_pk, org_id, branch_id, f"EMP-{u_id}", u_name))

            loc = payload.get("location") or ev.get("location") or "Main Entrance"
            dev = payload.get("device_id") or ev.get("device_id") or "Edge Camera"
            src = payload.get("source") or ev.get("source") or "auto"

            # Idempotently insert into attendance_events
            # Unique identification: (organization_id, id)
            cur.execute("""
                INSERT INTO attendance_events (
                    id, organization_id, branch_id, employee_id,
                    event_type, confidence, sync_status, timestamp,
                    location, device_id, source
                )
                VALUES (%s, %s, %s, %s, %s, %s, 'synced', %s, %s, %s, %s)
                ON CONFLICT (organization_id, id) DO NOTHING;
            """, (ev_id, org_id, branch_id, emp_pk, ev_type, conf, ev_ts, loc, dev, src))

            if cur.rowcount > 0:
                processed_count += 1
            else:
                duplicate_count += 1

        # Mark batch processed
        cur.execute("""
            UPDATE edge_sync_batches
            SET status = 'processed', processed_at = CURRENT_TIMESTAMP
            WHERE organization_id = %s AND id = %s;
        """, (org_id, batch_id))

    return {
        "success": True,
        "batch_id": batch_id,
        "batch_sequence": batch_sequence,
        "organization_id": org_id,
        "branch_id": branch_id,
        "records_received": len(events),
        "records_ingested": processed_count,
        "duplicates_skipped": duplicate_count
    }


# =====================================================================
# PHASE 2.5: CLOUD -> EDGE CONFIGURATION SYNCHRONIZATION & HEARTBEAT
# =====================================================================

def update_edge_node_heartbeat(
    edge_node_auth: Dict[str, Any],
    status: str = "online",
    software_version: Optional[str] = None
) -> Dict[str, Any]:
    """
    Update edge node heartbeat, status, and optional software version.
    Returns current configuration version so edge can detect drift.
    """
    org_id = edge_node_auth["organization_id"]
    branch_id = edge_node_auth["branch_id"]
    node_id = edge_node_auth["edge_node_id"]

    with tenant_context(org_id=org_id, branch_id=branch_id, is_platform_admin=True, enforce_rls=False) as cur:
        # Update heartbeat and status
        if software_version:
            cur.execute("""
                UPDATE edge_nodes
                SET last_heartbeat = CURRENT_TIMESTAMP,
                    status = %s,
                    software_version = %s,
                    updated_at = CURRENT_TIMESTAMP
                WHERE organization_id = %s AND id = %s;
            """, (status, software_version, org_id, node_id))
        else:
            cur.execute("""
                UPDATE edge_nodes
                SET last_heartbeat = CURRENT_TIMESTAMP,
                    status = %s,
                    updated_at = CURRENT_TIMESTAMP
                WHERE organization_id = %s AND id = %s;
            """, (status, org_id, node_id))

        # Query branch config_version
        cur.execute("""
            SELECT config_version, access
            FROM branches
            WHERE organization_id = %s AND id = %s;
        """, (org_id, branch_id))
        branch_info = cur.fetchone()
        cfg_ver = branch_info["config_version"] if branch_info else 1
        branch_access = branch_info["access"] if branch_info else "Active"

        return {
            "success": True,
            "edge_node_id": node_id,
            "organization_id": org_id,
            "branch_id": branch_id,
            "status": status,
            "config_version": cfg_ver,
            "branch_status": branch_access.lower(),
            "server_time": datetime.now(timezone.utc).isoformat()
        }


def get_edge_node_configuration(edge_node_auth: Dict[str, Any]) -> Dict[str, Any]:
    """
    Retrieve authoritative branch configuration for an authenticated Edge Node.
    Resolves ONLY the edge node's assigned organization and branch.
    Never exposes sensitive unhashed credentials or cross-tenant data.
    """
    org_id = edge_node_auth["organization_id"]
    branch_id = edge_node_auth["branch_id"]
    node_id = edge_node_auth["edge_node_id"]

    with tenant_context(org_id=org_id, branch_id=branch_id, enforce_rls=False) as cur:
        # 1. Fetch organization details
        cur.execute("SELECT id, name, status FROM organizations WHERE id = %s;", (org_id,))
        org_row = cur.fetchone()
        org_name = org_row["name"] if org_row else "Unknown Organization"
        org_status = org_row["status"] if org_row else "Active"

        # 2. Fetch branch details & config_version
        cur.execute("""
            SELECT id, name, access, config_version, grace_days, token_expires_at
            FROM branches
            WHERE organization_id = %s AND id = %s;
        """, (org_id, branch_id))
        br_row = cur.fetchone()
        if not br_row:
            raise ValueError(f"Branch '{branch_id}' not found for organization '{org_id}'")

        branch_name = br_row["name"]
        branch_access = br_row["access"] or "Active"
        config_version = br_row["config_version"] or 1

        # 3. Fetch active license
        cur.execute("""
            SELECT id, status, tier, starts_at, expires_at
            FROM organization_licenses
            WHERE organization_id = %s
            ORDER BY created_at DESC
            LIMIT 1;
        """, (org_id,))
        lic_row = cur.fetchone()

        license_info = {
            "license_id": lic_row["id"] if lic_row else "LIC-DEFAULT",
            "license_status": lic_row["status"] if lic_row else "active",
            "plan_tier": lic_row["tier"] if lic_row else "standard",
            "starts_at": lic_row["starts_at"].isoformat() if lic_row and lic_row["starts_at"] else None,
            "expires_at": lic_row["expires_at"].isoformat() if lic_row and lic_row["expires_at"] else None
        }

        # If branch is suspended, reflect suspended state
        if branch_access in ("Suspended", "suspended"):
            license_info["license_status"] = "suspended"

        # 4. Fetch enabled modules for organization
        cur.execute("""
            SELECT module_id
            FROM organization_modules
            WHERE organization_id = %s AND is_enabled = TRUE;
        """, (org_id,))
        enabled_modules = [r["module_id"] for r in cur.fetchall()]
        if not enabled_modules:
            enabled_modules = ["attendance"]

        # 5. Fetch configured cameras for this branch
        cur.execute("""
            SELECT 
                id as camera_id,
                camera_name as name,
                location,
                COALESCE(room_name, location) as room_name,
                COALESCE(camera_type, 'rtsp') as camera_type,
                rtsp_url,
                COALESCE(channel_number, 1) as channel_number,
                CASE WHEN is_active THEN 1 ELSE 0 END as active
            FROM cameras
            WHERE organization_id = %s AND branch_id = %s
            ORDER BY created_at ASC;
        """, (org_id, branch_id))
        raw_cameras = [dict(c) for c in cur.fetchall()]

        # Filter and structure cameras safely
        cameras = []
        for idx, c in enumerate(raw_cameras, start=1):
            cameras.append({
                "camera_id": str(c["camera_id"]),
                "name": str(c["name"]),
                "room_name": str(c["room_name"] or c["location"] or f"Room {idx}"),
                "camera_type": str(c["camera_type"] or "rtsp"),
                "rtsp_url": str(c["rtsp_url"]),
                "channel_number": int(c.get("channel_number") or idx),
                "active": int(c["active"])
            })

        # Update edge_node config_version and last sync time
        cur.execute("""
            UPDATE edge_nodes
            SET config_version = %s,
                updated_at = CURRENT_TIMESTAMP
            WHERE organization_id = %s AND id = %s;
        """, (config_version, org_id, node_id))

        return {
            "success": True,
            "edge_node_id": node_id,
            "organization_id": org_id,
            "organization_name": org_name,
            "branch_id": branch_id,
            "branch_name": branch_name,
            "branch_status": branch_access.lower(),
            "config_version": config_version,
            "license": license_info,
            "licensed_modules": enabled_modules,
            "cameras": cameras,
            "camera_count": len(cameras),
            "synced_at": datetime.now(timezone.utc).isoformat()
        }


def get_cloud_cameras(org_id: str, branch_id: str) -> List[Dict[str, Any]]:
    """Fetch real cameras from PostgreSQL for an organization and branch."""
    with tenant_context(org_id=org_id, branch_id=branch_id, is_platform_admin=True, enforce_rls=False) as cur:
        cur.execute("""
            SELECT 
                id as camera_id,
                camera_name as name,
                location,
                COALESCE(room_name, location) as room_name,
                COALESCE(camera_type, 'rtsp') as camera_type,
                rtsp_url,
                COALESCE(channel_number, 1) as channel_number,
                CASE WHEN is_active THEN 1 ELSE 0 END as active,
                created_at,
                updated_at
            FROM cameras
            WHERE organization_id = %s AND branch_id = %s
            ORDER BY created_at ASC;
        """, (org_id, branch_id))
        return [dict(c) for c in cur.fetchall()]


def set_cloud_cameras(org_id: str, branch_id: str, cameras: List[Dict[str, Any]]) -> int:
    """
    Replace or update configured cameras for an organization and branch in PostgreSQL.
    Atomically increments branch config_version so edge nodes automatically detect the update.
    Returns the new config_version.
    """
    with tenant_context(org_id=org_id, branch_id=branch_id, is_platform_admin=True, enforce_rls=False) as cur:
        # Delete existing cameras for branch
        cur.execute("DELETE FROM cameras WHERE organization_id = %s AND branch_id = %s;", (org_id, branch_id))

        # Insert updated camera list
        for idx, cam in enumerate(cameras, start=1):
            cam_id = str(cam.get("camera_id") or cam.get("id") or f"cam_{uuid.uuid4().hex[:8]}")
            cam_name = str(cam.get("name") or cam.get("camera_name") or f"Camera #{idx}")
            cam_type = str(cam.get("camera_type") or "rtsp")
            rtsp = str(cam.get("rtsp_url") or "0")
            room = str(cam.get("room_name") or cam.get("location") or "Main Area")
            ch_num = int(cam.get("channel_number") or idx)
            is_act = bool(cam.get("active", 1) in (1, True, "1", "true"))

            cur.execute("""
                INSERT INTO cameras (
                    id, organization_id, branch_id, camera_name,
                    camera_type, rtsp_url, location, room_name,
                    channel_number, is_active, updated_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP);
            """, (cam_id, org_id, branch_id, cam_name, cam_type, rtsp, room, room, ch_num, is_act))

        # Increment branch config_version
        cur.execute("""
            UPDATE branches
            SET config_version = config_version + 1,
                updated_at = CURRENT_TIMESTAMP
            WHERE organization_id = %s AND id = %s
            RETURNING config_version;
        """, (org_id, branch_id))
        row = cur.fetchone()
        new_version = row["config_version"] if row else 1

        return new_version


# =====================================================================
# PHASE 2.4: CONNECT EXISTING PAGES TO REAL CLOUD POSTGRESQL DATA
# Real Data Only — No Fake Data — Strict Tenant Isolation
# =====================================================================

def check_module_entitlement(org_id: str, module_key: str, user_context: Optional[CloudUser] = None) -> bool:
    """Check if organization is licensed for the requested module."""
    # Foundational modules are always permitted
    if module_key in ("attendance", "staff", "settings", "employees"):
        return True
    
    with tenant_context(user=user_context, org_id=org_id, is_platform_admin=True, enforce_rls=False) as cur:
        # Check organization_modules table
        cur.execute("""
            SELECT om.is_enabled 
            FROM organization_modules om
            JOIN module_catalog mc ON om.module_id = mc.id
            WHERE om.organization_id = %s AND (mc.id = %s OR lower(mc.name) LIKE %s)
            AND om.is_enabled = TRUE;
        """, (org_id, module_key, f"%{module_key.lower()}%"))
        row = cur.fetchone()
        if row and row.get("is_enabled"):
            return True

        # Check organization_licenses active modules
        cur.execute("""
            SELECT tier FROM organization_licenses
            WHERE organization_id = %s AND status = 'active' AND expires_at >= CURRENT_TIMESTAMP;
        """, (org_id,))
        lic = cur.fetchone()
        if lic:
            tier = (lic.get("tier") or "").lower()
            if tier in ("enterprise", "all_inclusive", "pro"):
                return True

    return False


def get_cloud_kpis(user_context: Optional[CloudUser] = None) -> Dict[str, Any]:
    """Return real control plane KPIs derived directly from PostgreSQL."""
    with tenant_context(user=user_context, is_platform_admin=True, enforce_rls=False) as cur:
        cur.execute("SELECT count(*) as count FROM organizations;")
        orgs_total = cur.fetchone()["count"]

        cur.execute("SELECT count(*) as count FROM organizations WHERE status = 'Active';")
        orgs_active = cur.fetchone()["count"]

        cur.execute("SELECT count(*) as count FROM module_catalog WHERE is_active = TRUE;")
        mods_total = cur.fetchone()["count"]

        cur.execute("SELECT count(*) as count FROM edge_sync_batches;")
        batches_total = cur.fetchone()["count"]

        cur.execute("SELECT count(*) as count FROM employees;")
        emps_total = cur.fetchone()["count"]

        cur.execute("SELECT count(*) as count FROM attendance_events;")
        events_total = cur.fetchone()["count"]

        return {
            "organizations_total": orgs_total,
            "organizations_active": orgs_active,
            "modules_total": mods_total,
            "builds_total": batches_total,
            "employees_total": emps_total,
            "events_total": events_total
        }


def get_cloud_staff_list(
    org_id: str,
    branch_id: Optional[str] = None,
    search: Optional[str] = None,
    department: Optional[str] = None,
    user_context: Optional[CloudUser] = None
) -> List[Dict[str, Any]]:
    """Fetch real employee roster from PostgreSQL with tenant & branch isolation."""
    with tenant_context(user=user_context, org_id=org_id, branch_id=branch_id) as cur:
        query = """
            SELECT 
                e.id,
                e.id as user_id,
                e.employee_code,
                e.name,
                e.email,
                e.phone,
                COALESCE(e.department, d.name, 'General') as department,
                COALESCE(e.position, des.title, 'Staff Member') as position,
                COALESCE(e.salary, 0.0) as salary,
                COALESCE(e.cnic, '') as cnic,
                CASE WHEN e.is_active THEN 1 ELSE 0 END as active,
                COALESCE(e.checkin_cameras, 'all') as checkin_cameras,
                COALESCE(e.checkout_cameras, 'all') as checkout_cameras,
                e.shift_id,
                e.created_at
            FROM employees e
            LEFT JOIN departments d ON e.organization_id = d.organization_id AND e.department_id = d.id
            LEFT JOIN designations des ON e.organization_id = des.organization_id AND e.designation_id = des.id
            WHERE e.organization_id = %s
        """
        params = [org_id]

        if branch_id:
            query += " AND e.branch_id = %s"
            params.append(branch_id)

        if department and department.lower() not in ("all", "all departments", ""):
            query += " AND (lower(e.department) = lower(%s) OR lower(d.name) = lower(%s))"
            params.extend([department, department])

        if search and search.strip():
            s_term = f"%{search.strip().lower()}%"
            query += " AND (lower(e.name) LIKE %s OR lower(e.employee_code) LIKE %s OR lower(COALESCE(e.email, '')) LIKE %s OR lower(COALESCE(e.cnic, '')) LIKE %s)"
            params.extend([s_term, s_term, s_term, s_term])

        query += " ORDER BY e.name ASC;"
        cur.execute(query, params)
        rows = [dict(r) for r in cur.fetchall()]
        for r in rows:
            r["salary"] = float(r["salary"] or 0)
        return rows


def get_detailed_cloud_attendance_report(
    org_id: str,
    branch_id: Optional[str] = None,
    target_date: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    department: Optional[str] = None,
    status_filter: Optional[str] = None,
    search_query: Optional[str] = None,
    limit: int = 500,
    user_context: Optional[CloudUser] = None
) -> Dict[str, Any]:
    """
    Generate comprehensive attendance report from real PostgreSQL attendance_events.
    Returns the exact structure and fields required by attendance_report.html and reports-view.
    """
    from datetime import date, timedelta

    # 1. Resolve date boundaries
    today = date.today().isoformat()
    if target_date == "today" or (not target_date and not start_date):
        start_date = today
        end_date = today
        is_multi_day = False
    elif target_date == "yesterday":
        yest = (date.today() - timedelta(days=1)).isoformat()
        start_date = yest
        end_date = yest
        is_multi_day = False
    elif target_date == "this_week":
        # last 7 days
        start_date = (date.today() - timedelta(days=6)).isoformat()
        end_date = today
        is_multi_day = True
    elif target_date == "this_month":
        start_date = date.today().replace(day=1).isoformat()
        end_date = today
        is_multi_day = (start_date != end_date)
    elif target_date and not start_date:
        start_date = target_date
        end_date = target_date
        is_multi_day = False
    else:
        start_date = start_date or today
        end_date = end_date or start_date
        is_multi_day = (start_date != end_date)

    with tenant_context(user=user_context, org_id=org_id, branch_id=branch_id) as cur:
        # 2. Fetch employee roster for this tenant/branch
        emp_query = """
            SELECT 
                e.id, 
                e.employee_code, 
                e.name, 
                COALESCE(e.department, d.name, 'General') as department,
                COALESCE(e.position, des.title, 'Staff Member') as position,
                COALESCE(e.salary, 0.0) as salary,
                e.shift_id,
                COALESCE(s.name, 'Default Shift') as shift_name
            FROM employees e
            LEFT JOIN departments d ON e.organization_id = d.organization_id AND e.department_id = d.id
            LEFT JOIN designations des ON e.organization_id = des.organization_id AND e.designation_id = des.id
            LEFT JOIN shifts s ON e.organization_id = s.organization_id AND e.shift_id = s.id
            WHERE e.organization_id = %s
        """
        emp_params = [org_id]
        if branch_id:
            emp_query += " AND e.branch_id = %s"
            emp_params.append(branch_id)

        if department and department.lower() not in ("all", "all departments", ""):
            emp_query += " AND (lower(e.department) = lower(%s) OR lower(d.name) = lower(%s))"
            emp_params.extend([department, department])

        if search_query and search_query.strip():
            s_term = f"%{search_query.strip().lower()}%"
            emp_query += " AND (lower(e.name) LIKE %s OR lower(e.employee_code) LIKE %s)"
            emp_params.extend([s_term, s_term])

        emp_query += " ORDER BY e.name ASC;"
        cur.execute(emp_query, emp_params)
        employees = [dict(r) for r in cur.fetchall()]
        emp_ids = [e["id"] for e in employees]

        # 3. Fetch attendance events in the period
        events_by_emp = {}
        if emp_ids:
            cur.execute("""
                SELECT 
                    id, organization_id, branch_id, employee_id, camera_id,
                    timestamp, event_type, confidence, source, location, device_id,
                    marked_by, notes, shift_id
                FROM attendance_events
                WHERE organization_id = %s 
                  AND timestamp >= %s::timestamptz 
                  AND timestamp <= (%s::date + interval '1 day')::timestamptz
                ORDER BY timestamp ASC;
            """, (org_id, start_date, end_date))
            for ev in cur.fetchall():
                e_id = ev["employee_id"]
                if e_id not in events_by_emp:
                    events_by_emp[e_id] = []
                events_by_emp[e_id].append(dict(ev))

        # 4. Construct records
        records = []
        present_count = 0
        late_count = 0
        absent_count = 0
        dept_stats = {}

        for emp in employees:
            e_id = emp["id"]
            d_name = emp["department"]
            if d_name not in dept_stats:
                dept_stats[d_name] = {"department": d_name, "total_staff": 0, "present": 0, "absent": 0, "late": 0}
            dept_stats[d_name]["total_staff"] += 1

            emp_events = events_by_emp.get(e_id, [])

            if emp_events:
                # Employee is present
                first_ev = emp_events[0]
                last_ev = emp_events[-1] if len(emp_events) > 1 else None

                first_seen_ts = first_ev["timestamp"].strftime("%I:%M %p") if isinstance(first_ev["timestamp"], datetime) else str(first_ev["timestamp"])
                last_seen_ts = last_ev["timestamp"].strftime("%I:%M %p") if (last_ev and isinstance(last_ev["timestamp"], datetime)) else ("-" if not last_ev else str(last_ev["timestamp"]))
                
                # Check status
                status = "PRESENT"
                if first_ev.get("event_type") == "CHECK_IN" and (not last_ev or last_ev == first_ev):
                    status = "CHECKED_IN"
                elif last_ev and last_ev.get("event_type") == "CHECK_OUT":
                    status = "CHECKED_OUT"

                present_count += 1
                dept_stats[d_name]["present"] += 1

                records.append({
                    "user_id": e_id,
                    "id": e_id,
                    "name": emp["name"],
                    "department": d_name,
                    "position": emp["position"],
                    "shift_name": emp["shift_name"],
                    "date": start_date,
                    "status": status,
                    "first_seen": first_seen_ts,
                    "last_seen": last_seen_ts,
                    "checkin_camera": first_ev.get("device_id") or first_ev.get("camera_id") or "Camera #1",
                    "checkout_camera": (last_ev.get("device_id") or last_ev.get("camera_id") or "-") if last_ev else "-",
                    "source": first_ev.get("source") or "auto",
                    "source_label": "Auto by System" if (first_ev.get("source") != "manual") else "Manual by Admin",
                    "marked_by": first_ev.get("marked_by") or "System",
                    "notes": first_ev.get("notes") or "",
                    "daily_records": []
                })
            else:
                absent_count += 1
                dept_stats[d_name]["absent"] += 1

                records.append({
                    "user_id": e_id,
                    "id": e_id,
                    "name": emp["name"],
                    "department": d_name,
                    "position": emp["position"],
                    "shift_name": emp["shift_name"],
                    "date": start_date,
                    "status": "ABSENT",
                    "first_seen": None,
                    "last_seen": None,
                    "checkin_camera": "-",
                    "checkout_camera": "-",
                    "source": "auto",
                    "source_label": "-",
                    "marked_by": "System",
                    "notes": "",
                    "daily_records": []
                })

        # Apply status filter if supplied
        if status_filter and status_filter.lower() not in ("all", "all statuses", ""):
            records = [r for r in records if r["status"].upper() == status_filter.upper()]

        total_enrolled = len(employees)
        att_rate = round((present_count / total_enrolled * 100), 1) if total_enrolled > 0 else 0.0

        dept_summary = list(dept_stats.values())
        for ds in dept_summary:
            tot = ds["total_staff"]
            ds["attendance_rate"] = round((ds["present"] / tot * 100), 1) if tot > 0 else 0.0

        date_label = start_date if not is_multi_day else f"{start_date} to {end_date}"

        return {
            "is_multi_day": is_multi_day,
            "date_label": date_label,
            "start_date": start_date,
            "end_date": end_date,
            "total_days": 1,
            "summary": {
                "total_enrolled": total_enrolled,
                "total_registered": total_enrolled,
                "total_matching": len(records),
                "attended_count": present_count,
                "total_present_days": present_count,
                "present": present_count,
                "total_late_days": late_count,
                "late": late_count,
                "total_absent_days": absent_count,
                "absent": absent_count,
                "attendance_rate": att_rate
            },
            "records": records,
            "rows": records,
            "departments": dept_summary
        }


def get_cloud_leave_requests(
    org_id: str,
    branch_id: Optional[str] = None,
    status: Optional[str] = None,
    user_context: Optional[CloudUser] = None
) -> List[Dict[str, Any]]:
    """Fetch real leave requests from PostgreSQL."""
    with tenant_context(user=user_context, org_id=org_id, branch_id=branch_id) as cur:
        query = """
            SELECT 
                lr.id,
                lr.employee_id,
                lr.employee_id as user_id,
                e.name as user_name,
                lr.leave_type,
                lr.start_date,
                lr.end_date,
                lr.reason,
                lr.status,
                lr.approved_by,
                lr.created_at
            FROM leave_requests lr
            LEFT JOIN employees e ON lr.organization_id = e.organization_id AND lr.employee_id = e.id
            WHERE lr.organization_id = %s
        """
        params = [org_id]
        if branch_id:
            query += " AND lr.branch_id = %s"
            params.append(branch_id)
        if status and status.lower() not in ("all", ""):
            query += " AND lr.status = %s"
            params.append(status)
        query += " ORDER BY lr.created_at DESC;"
        cur.execute(query, params)
        return [dict(r) for r in cur.fetchall()]


def get_cloud_overtime_records(
    org_id: str,
    branch_id: Optional[str] = None,
    status: Optional[str] = None,
    user_context: Optional[CloudUser] = None
) -> List[Dict[str, Any]]:
    """Fetch real overtime records from PostgreSQL."""
    with tenant_context(user=user_context, org_id=org_id, branch_id=branch_id) as cur:
        query = """
            SELECT 
                ot.id,
                ot.employee_id,
                ot.employee_id as user_id,
                e.name as user_name,
                ot.work_date as ot_date,
                ot.hours,
                ot.reason,
                ot.status,
                ot.approved_by,
                ot.created_at
            FROM overtime_records ot
            LEFT JOIN employees e ON ot.organization_id = e.organization_id AND ot.employee_id = e.id
            WHERE ot.organization_id = %s
        """
        params = [org_id]
        if branch_id:
            query += " AND ot.branch_id = %s"
            params.append(branch_id)
        if status and status.lower() not in ("all", ""):
            query += " AND ot.status = %s"
            params.append(status)
        query += " ORDER BY ot.work_date DESC;"
        cur.execute(query, params)
        rows = [dict(r) for r in cur.fetchall()]
        for r in rows:
            r["hours"] = float(r["hours"] or 0)
        return rows


def get_cloud_shifts(
    org_id: str,
    branch_id: Optional[str] = None,
    user_context: Optional[CloudUser] = None
) -> List[Dict[str, Any]]:
    """Fetch real shift schedules from PostgreSQL."""
    with tenant_context(user=user_context, org_id=org_id, branch_id=branch_id) as cur:
        query = """
            SELECT 
                id, organization_id, branch_id, name,
                start_time::text, end_time::text, grace_minutes as grace_period_minutes,
                working_days, is_default, is_active as active, created_at
            FROM shifts
            WHERE organization_id = %s
        """
        params = [org_id]
        if branch_id:
            query += " AND branch_id = %s"
            params.append(branch_id)
        query += " ORDER BY id ASC;"
        cur.execute(query, params)
        return [dict(r) for r in cur.fetchall()]


def get_cloud_payroll_runs(
    org_id: str,
    branch_id: Optional[str] = None,
    user_context: Optional[CloudUser] = None
) -> List[Dict[str, Any]]:
    """Fetch real finalized/draft payroll runs from PostgreSQL."""
    with tenant_context(user=user_context, org_id=org_id, branch_id=branch_id) as cur:
        query = """
            SELECT 
                id, organization_id, branch_id, payroll_month,
                period_start, period_end, employee_count,
                total_basic_salary, total_deduction, total_amount as total_net_salary,
                status, created_at
            FROM payroll_runs
            WHERE organization_id = %s
        """
        params = [org_id]
        if branch_id:
            query += " AND branch_id = %s"
            params.append(branch_id)
        query += " ORDER BY period_start DESC;"
        cur.execute(query, params)
        rows = [dict(r) for r in cur.fetchall()]
        for r in rows:
            r["total_basic_salary"] = float(r["total_basic_salary"] or 0)
            r["total_deduction"] = float(r["total_deduction"] or 0)
            r["total_net_salary"] = float(r["total_net_salary"] or 0)
        return rows


def get_cloud_payroll_records(
    org_id: str,
    run_id: str,
    branch_id: Optional[str] = None,
    user_context: Optional[CloudUser] = None
) -> List[Dict[str, Any]]:
    """Fetch real immutable employee payroll lines for a payroll run."""
    with tenant_context(user=user_context, org_id=org_id, branch_id=branch_id) as cur:
        query = """
            SELECT 
                pr.id, pr.payroll_run_id, pr.employee_id,
                e.name as employee_name,
                COALESCE(e.department, 'General') as department,
                COALESCE(e.position, 'Staff Member') as designation,
                pr.basic_salary, pr.allowances, pr.deductions, pr.net_salary,
                pr.scheduled_workdays, pr.present_days, pr.absent_days, pr.late_days,
                pr.approved_leave_days, pr.payable_days, pr.created_at
            FROM payroll_records pr
            LEFT JOIN employees e ON pr.organization_id = e.organization_id AND pr.employee_id = e.id
            WHERE pr.organization_id = %s AND pr.payroll_run_id = %s
        """
        params = [org_id, run_id]
        if branch_id:
            query += " AND pr.branch_id = %s"
            params.append(branch_id)
        query += " ORDER BY pr.id ASC;"
        cur.execute(query, params)
        rows = [dict(r) for r in cur.fetchall()]
        for r in rows:
            r["basic_salary"] = float(r["basic_salary"] or 0)
            r["allowances"] = float(r["allowances"] or 0)
            r["deductions"] = float(r["deductions"] or 0)
            r["net_salary"] = float(r["net_salary"] or 0)
            r["payable_days"] = float(r["payable_days"] or 0)
        return rows

