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

            emp_pk = f"EMP-{org_id}-{branch_id}-{u_id}"
            # Ensure employee placeholder exists so foreign key is satisfied
            cur.execute("""
                INSERT INTO employees (id, organization_id, branch_id, employee_code, name)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (organization_id, id) DO NOTHING;
            """, (emp_pk, org_id, branch_id, f"EMP-{u_id}", u_name))

            # Idempotently insert into attendance_events
            # Unique identification: (organization_id, id)
            cur.execute("""
                INSERT INTO attendance_events (
                    id, organization_id, branch_id, employee_id,
                    event_type, confidence, sync_status, timestamp
                )
                VALUES (%s, %s, %s, %s, %s, %s, 'synced', %s)
                ON CONFLICT (organization_id, id) DO NOTHING;
            """, (ev_id, org_id, branch_id, emp_pk, ev_type, conf, ev_ts))

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
