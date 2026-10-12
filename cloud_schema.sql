-- ====================================================================
-- QUANTEYE CLOUD MULTI-TENANT ARCHITECTURE: PHASE 2.1 & 2.2 SCHEMA
-- PostgreSQL Cloud Database DDL with Strict Multi-Tenant Row Level Security
-- ====================================================================

-- 1. Create Application Runtime Role (NOBYPASSRLS)
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'app_runtime_role') THEN
        CREATE ROLE app_runtime_role WITH LOGIN NOBYPASSRLS NOSUPERUSER NOCREATEDB NOCREATEROLE;
    ELSE
        ALTER ROLE app_runtime_role WITH NOBYPASSRLS NOSUPERUSER NOCREATEDB NOCREATEROLE;
    END IF;
END $$;

-- Enable UUID extension if available
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- ====================================================================
-- CORE TENANT & TOPOLOGY TABLES
-- ====================================================================

-- 1. Organizations table
CREATE TABLE IF NOT EXISTS organizations (
    id VARCHAR(64) PRIMARY KEY,
    name VARCHAR(255) NOT NULL UNIQUE,
    industry VARCHAR(100) DEFAULT 'Technology',
    email VARCHAR(255),
    phone VARCHAR(50),
    address TEXT,
    status VARCHAR(50) NOT NULL DEFAULT 'Active' CHECK (status IN ('Active', 'Build Pending', 'Inactive', 'Suspended')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_organizations_status ON organizations(status);
CREATE INDEX IF NOT EXISTS idx_organizations_created_at ON organizations(created_at);

-- 2. Branches table
CREATE TABLE IF NOT EXISTS branches (
    id VARCHAR(64) NOT NULL,
    organization_id VARCHAR(64) NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    name VARCHAR(255) NOT NULL,
    city VARCHAR(100) DEFAULT '',
    address TEXT DEFAULT '',
    access VARCHAR(50) NOT NULL DEFAULT 'Active' CHECK (access IN ('Active', 'Suspended', 'Grace Period')),
    grace_days INT NOT NULL DEFAULT 0,
    employee_count INT NOT NULL DEFAULT 0,
    token_key TEXT,
    token_parent_id TEXT,
    token_started_at TIMESTAMPTZ,
    token_expires_at TIMESTAMPTZ,
    suspension_kind VARCHAR(50),
    suspension_days INT DEFAULT 0,
    suspension_started_at TIMESTAMPTZ,
    suspension_ends_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (organization_id, id)
);

CREATE INDEX IF NOT EXISTS idx_branches_org_access ON branches(organization_id, access);
CREATE INDEX IF NOT EXISTS idx_branches_token_key ON branches(token_key);

-- 3. Organization Users table
CREATE TABLE IF NOT EXISTS organization_users (
    id VARCHAR(64) NOT NULL,
    organization_id VARCHAR(64) NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    username VARCHAR(100) NOT NULL,
    email VARCHAR(255) NOT NULL,
    password_hash VARCHAR(255) NOT NULL,
    role VARCHAR(50) NOT NULL CHECK (role IN ('platform_admin', 'org_admin', 'branch_manager', 'auditor')),
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (organization_id, id),
    UNIQUE (organization_id, username),
    UNIQUE (organization_id, email)
);

CREATE INDEX IF NOT EXISTS idx_org_users_lookup ON organization_users(email, is_active);
CREATE INDEX IF NOT EXISTS idx_org_users_role ON organization_users(organization_id, role);

-- 4. User Branch Memberships table
CREATE TABLE IF NOT EXISTS user_branch_memberships (
    id VARCHAR(64) NOT NULL,
    organization_id VARCHAR(64) NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    user_id VARCHAR(64) NOT NULL,
    branch_id VARCHAR(64) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (organization_id, id),
    UNIQUE (organization_id, user_id, branch_id),
    FOREIGN KEY (organization_id, branch_id) REFERENCES branches(organization_id, id) ON DELETE CASCADE,
    FOREIGN KEY (organization_id, user_id) REFERENCES organization_users(organization_id, id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_ubm_user ON user_branch_memberships(organization_id, user_id);
CREATE INDEX IF NOT EXISTS idx_ubm_branch ON user_branch_memberships(organization_id, branch_id);

-- ====================================================================
-- MODULE & COMMERCIAL TABLES
-- ====================================================================

-- 5. Module Catalog (System-wide global catalog)
CREATE TABLE IF NOT EXISTS module_catalog (
    id VARCHAR(64) PRIMARY KEY,
    name VARCHAR(255) NOT NULL UNIQUE,
    description TEXT,
    category VARCHAR(100) DEFAULT 'core',
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_mod_cat_active ON module_catalog(is_active);

-- 6. Organization Modules
CREATE TABLE IF NOT EXISTS organization_modules (
    id VARCHAR(64) NOT NULL,
    organization_id VARCHAR(64) NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    module_id VARCHAR(64) NOT NULL REFERENCES module_catalog(id) ON DELETE CASCADE,
    is_enabled BOOLEAN NOT NULL DEFAULT TRUE,
    configured_settings JSONB DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (organization_id, id),
    UNIQUE (organization_id, module_id)
);

CREATE INDEX IF NOT EXISTS idx_org_mods ON organization_modules(organization_id, is_enabled);

-- 7. Organization Licenses
CREATE TABLE IF NOT EXISTS organization_licenses (
    id VARCHAR(64) NOT NULL,
    organization_id VARCHAR(64) NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    license_key VARCHAR(255) NOT NULL,
    tier VARCHAR(50) NOT NULL DEFAULT 'standard',
    max_branches INT NOT NULL DEFAULT 1,
    max_employees INT NOT NULL DEFAULT 50,
    starts_at TIMESTAMPTZ NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    status VARCHAR(50) NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'expired', 'revoked', 'trial')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (organization_id, id),
    UNIQUE (license_key)
);

CREATE INDEX IF NOT EXISTS idx_org_lic_status ON organization_licenses(organization_id, status);

-- 8. Organization Branding
CREATE TABLE IF NOT EXISTS organization_branding (
    organization_id VARCHAR(64) PRIMARY KEY REFERENCES organizations(id) ON DELETE CASCADE,
    company_name VARCHAR(255),
    logo_url TEXT,
    primary_color VARCHAR(50) DEFAULT '#4f46e5',
    accent_color VARCHAR(50) DEFAULT '#06b6d4',
    custom_css TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- ====================================================================
-- EDGE & CAMERA TOPOLOGY TABLES
-- ====================================================================

-- 9. Edge Nodes
CREATE TABLE IF NOT EXISTS edge_nodes (
    id VARCHAR(64) NOT NULL,
    organization_id VARCHAR(64) NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    branch_id VARCHAR(64) NOT NULL,
    node_name VARCHAR(255) NOT NULL,
    hardware_uuid VARCHAR(255),
    status VARCHAR(50) NOT NULL DEFAULT 'offline' CHECK (status IN ('online', 'offline', 'syncing', 'error')),
    last_heartbeat TIMESTAMPTZ,
    api_key_hash VARCHAR(255),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (organization_id, id),
    FOREIGN KEY (organization_id, branch_id) REFERENCES branches(organization_id, id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_edge_nodes_branch ON edge_nodes(organization_id, branch_id);
CREATE INDEX IF NOT EXISTS idx_edge_nodes_status ON edge_nodes(organization_id, status);

-- 10. Edge Sync Batches
CREATE TABLE IF NOT EXISTS edge_sync_batches (
    id VARCHAR(64) NOT NULL,
    organization_id VARCHAR(64) NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    branch_id VARCHAR(64) NOT NULL,
    edge_node_id VARCHAR(64) NOT NULL,
    batch_sequence BIGINT NOT NULL,
    record_count INT NOT NULL DEFAULT 0,
    status VARCHAR(50) NOT NULL DEFAULT 'received' CHECK (status IN ('received', 'processed', 'failed')),
    received_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    processed_at TIMESTAMPTZ,
    PRIMARY KEY (organization_id, id),
    FOREIGN KEY (organization_id, branch_id) REFERENCES branches(organization_id, id) ON DELETE CASCADE,
    FOREIGN KEY (organization_id, edge_node_id) REFERENCES edge_nodes(organization_id, id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_esb_lookup ON edge_sync_batches(organization_id, branch_id, status);

-- 11. Cameras
CREATE TABLE IF NOT EXISTS cameras (
    id VARCHAR(64) NOT NULL,
    organization_id VARCHAR(64) NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    branch_id VARCHAR(64) NOT NULL,
    camera_name VARCHAR(255) NOT NULL,
    rtsp_url TEXT NOT NULL,
    location VARCHAR(255) DEFAULT '',
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (organization_id, id),
    FOREIGN KEY (organization_id, branch_id) REFERENCES branches(organization_id, id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_cameras_branch ON cameras(organization_id, branch_id, is_active);

-- ====================================================================
-- HR STRUCTURE & OPERATIONS TABLES
-- ====================================================================

-- 12. Departments
CREATE TABLE IF NOT EXISTS departments (
    id VARCHAR(64) NOT NULL,
    organization_id VARCHAR(64) NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    branch_id VARCHAR(64) NOT NULL,
    name VARCHAR(255) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (organization_id, id),
    UNIQUE (organization_id, branch_id, name),
    FOREIGN KEY (organization_id, branch_id) REFERENCES branches(organization_id, id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_dept_branch ON departments(organization_id, branch_id);

-- 13. Designations
CREATE TABLE IF NOT EXISTS designations (
    id VARCHAR(64) NOT NULL,
    organization_id VARCHAR(64) NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    branch_id VARCHAR(64) NOT NULL,
    title VARCHAR(255) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (organization_id, id),
    UNIQUE (organization_id, branch_id, title),
    FOREIGN KEY (organization_id, branch_id) REFERENCES branches(organization_id, id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_desig_branch ON designations(organization_id, branch_id);

-- 14. Shifts
CREATE TABLE IF NOT EXISTS shifts (
    id VARCHAR(64) NOT NULL,
    organization_id VARCHAR(64) NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    branch_id VARCHAR(64) NOT NULL,
    name VARCHAR(255) NOT NULL,
    start_time TIME NOT NULL,
    end_time TIME NOT NULL,
    grace_minutes INT NOT NULL DEFAULT 15,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (organization_id, id),
    FOREIGN KEY (organization_id, branch_id) REFERENCES branches(organization_id, id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_shifts_branch ON shifts(organization_id, branch_id);

-- 15. Employees
CREATE TABLE IF NOT EXISTS employees (
    id VARCHAR(64) NOT NULL,
    organization_id VARCHAR(64) NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    branch_id VARCHAR(64) NOT NULL,
    employee_code VARCHAR(100) NOT NULL,
    name VARCHAR(255) NOT NULL,
    email VARCHAR(255),
    phone VARCHAR(50),
    department_id VARCHAR(64),
    designation_id VARCHAR(64),
    shift_id VARCHAR(64),
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    face_embedding_id VARCHAR(255),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (organization_id, id),
    UNIQUE (organization_id, branch_id, employee_code),
    FOREIGN KEY (organization_id, branch_id) REFERENCES branches(organization_id, id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_emp_lookup ON employees(organization_id, branch_id, is_active);
CREATE INDEX IF NOT EXISTS idx_emp_code ON employees(organization_id, employee_code);

-- 16. Attendance Events
CREATE TABLE IF NOT EXISTS attendance_events (
    id VARCHAR(64) NOT NULL,
    organization_id VARCHAR(64) NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    branch_id VARCHAR(64) NOT NULL,
    employee_id VARCHAR(64) NOT NULL,
    camera_id VARCHAR(64),
    timestamp TIMESTAMPTZ NOT NULL,
    event_type VARCHAR(50) NOT NULL CHECK (event_type IN ('CHECK_IN', 'CHECK_OUT', 'BREAK_IN', 'BREAK_OUT', 'RECOGNITION')),
    confidence REAL NOT NULL DEFAULT 1.0,
    sync_status VARCHAR(50) NOT NULL DEFAULT 'synced' CHECK (sync_status IN ('pending', 'synced', 'failed')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (organization_id, id),
    FOREIGN KEY (organization_id, branch_id) REFERENCES branches(organization_id, id) ON DELETE CASCADE,
    FOREIGN KEY (organization_id, employee_id) REFERENCES employees(organization_id, id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_att_lookup ON attendance_events(organization_id, branch_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_att_emp_time ON attendance_events(organization_id, employee_id, timestamp);

-- 17. Leave Requests
CREATE TABLE IF NOT EXISTS leave_requests (
    id VARCHAR(64) NOT NULL,
    organization_id VARCHAR(64) NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    branch_id VARCHAR(64) NOT NULL,
    employee_id VARCHAR(64) NOT NULL,
    leave_type VARCHAR(50) NOT NULL CHECK (leave_type IN ('annual', 'sick', 'casual', 'unpaid')),
    start_date DATE NOT NULL,
    end_date DATE NOT NULL,
    status VARCHAR(50) NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'rejected', 'cancelled')),
    reason TEXT,
    approved_by VARCHAR(64),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (organization_id, id),
    FOREIGN KEY (organization_id, branch_id) REFERENCES branches(organization_id, id) ON DELETE CASCADE,
    FOREIGN KEY (organization_id, employee_id) REFERENCES employees(organization_id, id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_leaves_branch ON leave_requests(organization_id, branch_id, status);

-- 18. Overtime Records
CREATE TABLE IF NOT EXISTS overtime_records (
    id VARCHAR(64) NOT NULL,
    organization_id VARCHAR(64) NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    branch_id VARCHAR(64) NOT NULL,
    employee_id VARCHAR(64) NOT NULL,
    work_date DATE NOT NULL,
    hours NUMERIC(8, 2) NOT NULL CHECK (hours > 0),
    reason TEXT,
    status VARCHAR(50) NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'rejected', 'cancelled', 'paid')),
    approved_by VARCHAR(64),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (organization_id, id),
    FOREIGN KEY (organization_id, branch_id) REFERENCES branches(organization_id, id) ON DELETE CASCADE,
    FOREIGN KEY (organization_id, employee_id) REFERENCES employees(organization_id, id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_ot_lookup ON overtime_records(organization_id, branch_id, work_date);
CREATE INDEX IF NOT EXISTS idx_ot_emp ON overtime_records(organization_id, employee_id);

-- 19. Payroll Runs
CREATE TABLE IF NOT EXISTS payroll_runs (
    id VARCHAR(64) NOT NULL,
    organization_id VARCHAR(64) NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    branch_id VARCHAR(64) NOT NULL,
    payroll_month VARCHAR(50),
    period_start DATE NOT NULL,
    period_end DATE NOT NULL,
    employee_count INT NOT NULL DEFAULT 0,
    total_basic_salary NUMERIC(14, 2) NOT NULL DEFAULT 0.00,
    total_deduction NUMERIC(14, 2) NOT NULL DEFAULT 0.00,
    status VARCHAR(50) NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'approved', 'paid', 'cancelled')),
    total_amount NUMERIC(14, 2) NOT NULL DEFAULT 0.00,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (organization_id, id),
    FOREIGN KEY (organization_id, branch_id) REFERENCES branches(organization_id, id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_payroll_runs_branch ON payroll_runs(organization_id, branch_id, period_start);

-- 20. Payroll Records
CREATE TABLE IF NOT EXISTS payroll_records (
    id VARCHAR(64) NOT NULL,
    organization_id VARCHAR(64) NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    branch_id VARCHAR(64) NOT NULL,
    payroll_run_id VARCHAR(64) NOT NULL,
    employee_id VARCHAR(64) NOT NULL,
    basic_salary NUMERIC(12, 2) NOT NULL DEFAULT 0.00,
    allowances NUMERIC(12, 2) NOT NULL DEFAULT 0.00,
    deductions NUMERIC(12, 2) NOT NULL DEFAULT 0.00,
    net_salary NUMERIC(12, 2) NOT NULL DEFAULT 0.00,
    scheduled_workdays INT NOT NULL DEFAULT 0,
    present_days INT NOT NULL DEFAULT 0,
    absent_days INT NOT NULL DEFAULT 0,
    late_days INT NOT NULL DEFAULT 0,
    approved_leave_days INT NOT NULL DEFAULT 0,
    payable_days NUMERIC(8, 2) NOT NULL DEFAULT 0.00,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (organization_id, id),
    FOREIGN KEY (organization_id, branch_id) REFERENCES branches(organization_id, id) ON DELETE CASCADE,
    FOREIGN KEY (organization_id, payroll_run_id) REFERENCES payroll_runs(organization_id, id) ON DELETE CASCADE,
    FOREIGN KEY (organization_id, employee_id) REFERENCES employees(organization_id, id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_payroll_rec_run ON payroll_records(organization_id, payroll_run_id);
CREATE INDEX IF NOT EXISTS idx_payroll_rec_emp ON payroll_records(organization_id, employee_id);

-- ====================================================================
-- ROW LEVEL SECURITY (RLS) POLICIES & ENFORCEMENT
-- ====================================================================

-- Enable and FORCE Row Level Security on all tenant tables
ALTER TABLE organizations ENABLE ROW LEVEL SECURITY;
ALTER TABLE organizations FORCE ROW LEVEL SECURITY;

ALTER TABLE branches ENABLE ROW LEVEL SECURITY;
ALTER TABLE branches FORCE ROW LEVEL SECURITY;

ALTER TABLE organization_users ENABLE ROW LEVEL SECURITY;
ALTER TABLE organization_users FORCE ROW LEVEL SECURITY;

ALTER TABLE user_branch_memberships ENABLE ROW LEVEL SECURITY;
ALTER TABLE user_branch_memberships FORCE ROW LEVEL SECURITY;

ALTER TABLE module_catalog ENABLE ROW LEVEL SECURITY;
ALTER TABLE module_catalog FORCE ROW LEVEL SECURITY;

ALTER TABLE organization_modules ENABLE ROW LEVEL SECURITY;
ALTER TABLE organization_modules FORCE ROW LEVEL SECURITY;

ALTER TABLE organization_licenses ENABLE ROW LEVEL SECURITY;
ALTER TABLE organization_licenses FORCE ROW LEVEL SECURITY;

ALTER TABLE organization_branding ENABLE ROW LEVEL SECURITY;
ALTER TABLE organization_branding FORCE ROW LEVEL SECURITY;

ALTER TABLE edge_nodes ENABLE ROW LEVEL SECURITY;
ALTER TABLE edge_nodes FORCE ROW LEVEL SECURITY;

ALTER TABLE edge_sync_batches ENABLE ROW LEVEL SECURITY;
ALTER TABLE edge_sync_batches FORCE ROW LEVEL SECURITY;

ALTER TABLE cameras ENABLE ROW LEVEL SECURITY;
ALTER TABLE cameras FORCE ROW LEVEL SECURITY;

ALTER TABLE departments ENABLE ROW LEVEL SECURITY;
ALTER TABLE departments FORCE ROW LEVEL SECURITY;

ALTER TABLE designations ENABLE ROW LEVEL SECURITY;
ALTER TABLE designations FORCE ROW LEVEL SECURITY;

ALTER TABLE shifts ENABLE ROW LEVEL SECURITY;
ALTER TABLE shifts FORCE ROW LEVEL SECURITY;

ALTER TABLE employees ENABLE ROW LEVEL SECURITY;
ALTER TABLE employees FORCE ROW LEVEL SECURITY;

ALTER TABLE attendance_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE attendance_events FORCE ROW LEVEL SECURITY;

ALTER TABLE leave_requests ENABLE ROW LEVEL SECURITY;
ALTER TABLE leave_requests FORCE ROW LEVEL SECURITY;

ALTER TABLE payroll_runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE payroll_runs FORCE ROW LEVEL SECURITY;

ALTER TABLE payroll_records ENABLE ROW LEVEL SECURITY;
ALTER TABLE payroll_records FORCE ROW LEVEL SECURITY;

-- --------------------------------------------------------------------
-- POLICIES: ORGANIZATIONS
-- --------------------------------------------------------------------
DROP POLICY IF EXISTS p_organizations_tenant_isolation ON organizations;
CREATE POLICY p_organizations_tenant_isolation ON organizations
    FOR ALL
    TO app_runtime_role
    USING (
        current_setting('app.is_platform_admin', true) = 'true'
        OR id = current_setting('app.current_org_id', true)
    )
    WITH CHECK (
        current_setting('app.is_platform_admin', true) = 'true'
        OR id = current_setting('app.current_org_id', true)
    );

-- --------------------------------------------------------------------
-- POLICIES: BRANCHES
-- --------------------------------------------------------------------
DROP POLICY IF EXISTS p_branches_tenant_isolation ON branches;
CREATE POLICY p_branches_tenant_isolation ON branches
    FOR ALL
    TO app_runtime_role
    USING (
        current_setting('app.is_platform_admin', true) = 'true'
        OR (
            organization_id = current_setting('app.current_org_id', true)
            AND (
                current_setting('app.current_branch_id', true) IS NULL
                OR current_setting('app.current_branch_id', true) = ''
                OR id = current_setting('app.current_branch_id', true)
            )
        )
    )
    WITH CHECK (
        current_setting('app.is_platform_admin', true) = 'true'
        OR (
            organization_id = current_setting('app.current_org_id', true)
            AND (
                current_setting('app.current_branch_id', true) IS NULL
                OR current_setting('app.current_branch_id', true) = ''
                OR id = current_setting('app.current_branch_id', true)
            )
        )
    );

-- --------------------------------------------------------------------
-- POLICIES: ORGANIZATION_USERS
-- --------------------------------------------------------------------
DROP POLICY IF EXISTS p_org_users_tenant_isolation ON organization_users;
CREATE POLICY p_org_users_tenant_isolation ON organization_users
    FOR ALL
    TO app_runtime_role
    USING (
        current_setting('app.is_platform_admin', true) = 'true'
        OR organization_id = current_setting('app.current_org_id', true)
    )
    WITH CHECK (
        current_setting('app.is_platform_admin', true) = 'true'
        OR organization_id = current_setting('app.current_org_id', true)
    );

-- --------------------------------------------------------------------
-- POLICIES: USER_BRANCH_MEMBERSHIPS
-- --------------------------------------------------------------------
DROP POLICY IF EXISTS p_ubm_tenant_isolation ON user_branch_memberships;
CREATE POLICY p_ubm_tenant_isolation ON user_branch_memberships
    FOR ALL
    TO app_runtime_role
    USING (
        current_setting('app.is_platform_admin', true) = 'true'
        OR organization_id = current_setting('app.current_org_id', true)
    )
    WITH CHECK (
        current_setting('app.is_platform_admin', true) = 'true'
        OR organization_id = current_setting('app.current_org_id', true)
    );

-- --------------------------------------------------------------------
-- POLICIES: MODULE_CATALOG (Global catalog readable by all, writable by platform admin)
-- --------------------------------------------------------------------
DROP POLICY IF EXISTS p_mod_cat_select ON module_catalog;
CREATE POLICY p_mod_cat_select ON module_catalog
    FOR SELECT
    TO app_runtime_role
    USING (true);

DROP POLICY IF EXISTS p_mod_cat_modify ON module_catalog;
CREATE POLICY p_mod_cat_modify ON module_catalog
    FOR ALL
    TO app_runtime_role
    USING (current_setting('app.is_platform_admin', true) = 'true')
    WITH CHECK (current_setting('app.is_platform_admin', true) = 'true');

-- --------------------------------------------------------------------
-- POLICIES: ORGANIZATION_MODULES
-- --------------------------------------------------------------------
DROP POLICY IF EXISTS p_org_mods_tenant_isolation ON organization_modules;
CREATE POLICY p_org_mods_tenant_isolation ON organization_modules
    FOR ALL
    TO app_runtime_role
    USING (
        current_setting('app.is_platform_admin', true) = 'true'
        OR organization_id = current_setting('app.current_org_id', true)
    )
    WITH CHECK (
        current_setting('app.is_platform_admin', true) = 'true'
        OR organization_id = current_setting('app.current_org_id', true)
    );

-- --------------------------------------------------------------------
-- POLICIES: ORGANIZATION_LICENSES
-- --------------------------------------------------------------------
DROP POLICY IF EXISTS p_org_lic_tenant_isolation ON organization_licenses;
CREATE POLICY p_org_lic_tenant_isolation ON organization_licenses
    FOR ALL
    TO app_runtime_role
    USING (
        current_setting('app.is_platform_admin', true) = 'true'
        OR organization_id = current_setting('app.current_org_id', true)
    )
    WITH CHECK (
        current_setting('app.is_platform_admin', true) = 'true'
        OR organization_id = current_setting('app.current_org_id', true)
    );

-- --------------------------------------------------------------------
-- POLICIES: ORGANIZATION_BRANDING
-- --------------------------------------------------------------------
DROP POLICY IF EXISTS p_org_branding_tenant_isolation ON organization_branding;
CREATE POLICY p_org_branding_tenant_isolation ON organization_branding
    FOR ALL
    TO app_runtime_role
    USING (
        current_setting('app.is_platform_admin', true) = 'true'
        OR organization_id = current_setting('app.current_org_id', true)
    )
    WITH CHECK (
        current_setting('app.is_platform_admin', true) = 'true'
        OR organization_id = current_setting('app.current_org_id', true)
    );

-- --------------------------------------------------------------------
-- POLICIES: EDGE_NODES
-- --------------------------------------------------------------------
DROP POLICY IF EXISTS p_edge_nodes_tenant_isolation ON edge_nodes;
CREATE POLICY p_edge_nodes_tenant_isolation ON edge_nodes
    FOR ALL
    TO app_runtime_role
    USING (
        current_setting('app.is_platform_admin', true) = 'true'
        OR (
            organization_id = current_setting('app.current_org_id', true)
            AND (
                current_setting('app.current_branch_id', true) IS NULL
                OR current_setting('app.current_branch_id', true) = ''
                OR branch_id = current_setting('app.current_branch_id', true)
            )
        )
    )
    WITH CHECK (
        current_setting('app.is_platform_admin', true) = 'true'
        OR (
            organization_id = current_setting('app.current_org_id', true)
            AND (
                current_setting('app.current_branch_id', true) IS NULL
                OR current_setting('app.current_branch_id', true) = ''
                OR branch_id = current_setting('app.current_branch_id', true)
            )
        )
    );

-- --------------------------------------------------------------------
-- POLICIES: EDGE_SYNC_BATCHES
-- --------------------------------------------------------------------
DROP POLICY IF EXISTS p_esb_tenant_isolation ON edge_sync_batches;
CREATE POLICY p_esb_tenant_isolation ON edge_sync_batches
    FOR ALL
    TO app_runtime_role
    USING (
        current_setting('app.is_platform_admin', true) = 'true'
        OR (
            organization_id = current_setting('app.current_org_id', true)
            AND (
                current_setting('app.current_branch_id', true) IS NULL
                OR current_setting('app.current_branch_id', true) = ''
                OR branch_id = current_setting('app.current_branch_id', true)
            )
        )
    )
    WITH CHECK (
        current_setting('app.is_platform_admin', true) = 'true'
        OR (
            organization_id = current_setting('app.current_org_id', true)
            AND (
                current_setting('app.current_branch_id', true) IS NULL
                OR current_setting('app.current_branch_id', true) = ''
                OR branch_id = current_setting('app.current_branch_id', true)
            )
        )
    );

-- --------------------------------------------------------------------
-- POLICIES: CAMERAS
-- --------------------------------------------------------------------
DROP POLICY IF EXISTS p_cameras_tenant_isolation ON cameras;
CREATE POLICY p_cameras_tenant_isolation ON cameras
    FOR ALL
    TO app_runtime_role
    USING (
        current_setting('app.is_platform_admin', true) = 'true'
        OR (
            organization_id = current_setting('app.current_org_id', true)
            AND (
                current_setting('app.current_branch_id', true) IS NULL
                OR current_setting('app.current_branch_id', true) = ''
                OR branch_id = current_setting('app.current_branch_id', true)
            )
        )
    )
    WITH CHECK (
        current_setting('app.is_platform_admin', true) = 'true'
        OR (
            organization_id = current_setting('app.current_org_id', true)
            AND (
                current_setting('app.current_branch_id', true) IS NULL
                OR current_setting('app.current_branch_id', true) = ''
                OR branch_id = current_setting('app.current_branch_id', true)
            )
        )
    );

-- --------------------------------------------------------------------
-- POLICIES: DEPARTMENTS
-- --------------------------------------------------------------------
DROP POLICY IF EXISTS p_departments_tenant_isolation ON departments;
CREATE POLICY p_departments_tenant_isolation ON departments
    FOR ALL
    TO app_runtime_role
    USING (
        current_setting('app.is_platform_admin', true) = 'true'
        OR (
            organization_id = current_setting('app.current_org_id', true)
            AND (
                current_setting('app.current_branch_id', true) IS NULL
                OR current_setting('app.current_branch_id', true) = ''
                OR branch_id = current_setting('app.current_branch_id', true)
            )
        )
    )
    WITH CHECK (
        current_setting('app.is_platform_admin', true) = 'true'
        OR (
            organization_id = current_setting('app.current_org_id', true)
            AND (
                current_setting('app.current_branch_id', true) IS NULL
                OR current_setting('app.current_branch_id', true) = ''
                OR branch_id = current_setting('app.current_branch_id', true)
            )
        )
    );

-- --------------------------------------------------------------------
-- POLICIES: DESIGNATIONS
-- --------------------------------------------------------------------
DROP POLICY IF EXISTS p_designations_tenant_isolation ON designations;
CREATE POLICY p_designations_tenant_isolation ON designations
    FOR ALL
    TO app_runtime_role
    USING (
        current_setting('app.is_platform_admin', true) = 'true'
        OR (
            organization_id = current_setting('app.current_org_id', true)
            AND (
                current_setting('app.current_branch_id', true) IS NULL
                OR current_setting('app.current_branch_id', true) = ''
                OR branch_id = current_setting('app.current_branch_id', true)
            )
        )
    )
    WITH CHECK (
        current_setting('app.is_platform_admin', true) = 'true'
        OR (
            organization_id = current_setting('app.current_org_id', true)
            AND (
                current_setting('app.current_branch_id', true) IS NULL
                OR current_setting('app.current_branch_id', true) = ''
                OR branch_id = current_setting('app.current_branch_id', true)
            )
        )
    );

-- --------------------------------------------------------------------
-- POLICIES: SHIFTS
-- --------------------------------------------------------------------
DROP POLICY IF EXISTS p_shifts_tenant_isolation ON shifts;
CREATE POLICY p_shifts_tenant_isolation ON shifts
    FOR ALL
    TO app_runtime_role
    USING (
        current_setting('app.is_platform_admin', true) = 'true'
        OR (
            organization_id = current_setting('app.current_org_id', true)
            AND (
                current_setting('app.current_branch_id', true) IS NULL
                OR current_setting('app.current_branch_id', true) = ''
                OR branch_id = current_setting('app.current_branch_id', true)
            )
        )
    )
    WITH CHECK (
        current_setting('app.is_platform_admin', true) = 'true'
        OR (
            organization_id = current_setting('app.current_org_id', true)
            AND (
                current_setting('app.current_branch_id', true) IS NULL
                OR current_setting('app.current_branch_id', true) = ''
                OR branch_id = current_setting('app.current_branch_id', true)
            )
        )
    );

-- --------------------------------------------------------------------
-- POLICIES: EMPLOYEES
-- --------------------------------------------------------------------
DROP POLICY IF EXISTS p_employees_tenant_isolation ON employees;
CREATE POLICY p_employees_tenant_isolation ON employees
    FOR ALL
    TO app_runtime_role
    USING (
        current_setting('app.is_platform_admin', true) = 'true'
        OR (
            organization_id = current_setting('app.current_org_id', true)
            AND (
                current_setting('app.current_branch_id', true) IS NULL
                OR current_setting('app.current_branch_id', true) = ''
                OR branch_id = current_setting('app.current_branch_id', true)
            )
        )
    )
    WITH CHECK (
        current_setting('app.is_platform_admin', true) = 'true'
        OR (
            organization_id = current_setting('app.current_org_id', true)
            AND (
                current_setting('app.current_branch_id', true) IS NULL
                OR current_setting('app.current_branch_id', true) = ''
                OR branch_id = current_setting('app.current_branch_id', true)
            )
        )
    );

-- --------------------------------------------------------------------
-- POLICIES: ATTENDANCE_EVENTS
-- --------------------------------------------------------------------
DROP POLICY IF EXISTS p_attendance_tenant_isolation ON attendance_events;
CREATE POLICY p_attendance_tenant_isolation ON attendance_events
    FOR ALL
    TO app_runtime_role
    USING (
        current_setting('app.is_platform_admin', true) = 'true'
        OR (
            organization_id = current_setting('app.current_org_id', true)
            AND (
                current_setting('app.current_branch_id', true) IS NULL
                OR current_setting('app.current_branch_id', true) = ''
                OR branch_id = current_setting('app.current_branch_id', true)
            )
        )
    )
    WITH CHECK (
        current_setting('app.is_platform_admin', true) = 'true'
        OR (
            organization_id = current_setting('app.current_org_id', true)
            AND (
                current_setting('app.current_branch_id', true) IS NULL
                OR current_setting('app.current_branch_id', true) = ''
                OR branch_id = current_setting('app.current_branch_id', true)
            )
        )
    );

-- --------------------------------------------------------------------
-- POLICIES: LEAVE_REQUESTS
-- --------------------------------------------------------------------
DROP POLICY IF EXISTS p_leave_requests_tenant_isolation ON leave_requests;
CREATE POLICY p_leave_requests_tenant_isolation ON leave_requests
    FOR ALL
    TO app_runtime_role
    USING (
        current_setting('app.is_platform_admin', true) = 'true'
        OR (
            organization_id = current_setting('app.current_org_id', true)
            AND (
                current_setting('app.current_branch_id', true) IS NULL
                OR current_setting('app.current_branch_id', true) = ''
                OR branch_id = current_setting('app.current_branch_id', true)
            )
        )
    )
    WITH CHECK (
        current_setting('app.is_platform_admin', true) = 'true'
        OR (
            organization_id = current_setting('app.current_org_id', true)
            AND (
                current_setting('app.current_branch_id', true) IS NULL
                OR current_setting('app.current_branch_id', true) = ''
                OR branch_id = current_setting('app.current_branch_id', true)
            )
        )
    );

-- --------------------------------------------------------------------
-- POLICIES: PAYROLL_RUNS
-- --------------------------------------------------------------------
DROP POLICY IF EXISTS p_payroll_runs_tenant_isolation ON payroll_runs;
CREATE POLICY p_payroll_runs_tenant_isolation ON payroll_runs
    FOR ALL
    TO app_runtime_role
    USING (
        current_setting('app.is_platform_admin', true) = 'true'
        OR (
            organization_id = current_setting('app.current_org_id', true)
            AND (
                current_setting('app.current_branch_id', true) IS NULL
                OR current_setting('app.current_branch_id', true) = ''
                OR branch_id = current_setting('app.current_branch_id', true)
            )
        )
    )
    WITH CHECK (
        current_setting('app.is_platform_admin', true) = 'true'
        OR (
            organization_id = current_setting('app.current_org_id', true)
            AND (
                current_setting('app.current_branch_id', true) IS NULL
                OR current_setting('app.current_branch_id', true) = ''
                OR branch_id = current_setting('app.current_branch_id', true)
            )
        )
    );

-- --------------------------------------------------------------------
-- POLICIES: PAYROLL_RECORDS
-- --------------------------------------------------------------------
DROP POLICY IF EXISTS p_payroll_records_tenant_isolation ON payroll_records;
CREATE POLICY p_payroll_records_tenant_isolation ON payroll_records
    FOR ALL
    TO app_runtime_role
    USING (
        current_setting('app.is_platform_admin', true) = 'true'
        OR (
            organization_id = current_setting('app.current_org_id', true)
            AND (
                current_setting('app.current_branch_id', true) IS NULL
                OR current_setting('app.current_branch_id', true) = ''
                OR branch_id = current_setting('app.current_branch_id', true)
            )
        )
    )
    WITH CHECK (
        current_setting('app.is_platform_admin', true) = 'true'
        OR (
            organization_id = current_setting('app.current_org_id', true)
            AND (
                current_setting('app.current_branch_id', true) IS NULL
                OR current_setting('app.current_branch_id', true) = ''
                OR branch_id = current_setting('app.current_branch_id', true)
            )
        )
    );

-- --------------------------------------------------------------------
-- POLICIES: OVERTIME_RECORDS
-- --------------------------------------------------------------------
DROP POLICY IF EXISTS p_overtime_records_tenant_isolation ON overtime_records;
CREATE POLICY p_overtime_records_tenant_isolation ON overtime_records
    FOR ALL
    TO app_runtime_role
    USING (
        current_setting('app.is_platform_admin', true) = 'true'
        OR (
            organization_id = current_setting('app.current_org_id', true)
            AND (
                current_setting('app.current_branch_id', true) IS NULL
                OR current_setting('app.current_branch_id', true) = ''
                OR branch_id = current_setting('app.current_branch_id', true)
            )
        )
    )
    WITH CHECK (
        current_setting('app.is_platform_admin', true) = 'true'
        OR (
            organization_id = current_setting('app.current_org_id', true)
            AND (
                current_setting('app.current_branch_id', true) IS NULL
                OR current_setting('app.current_branch_id', true) = ''
                OR branch_id = current_setting('app.current_branch_id', true)
            )
        )
    );

-- Grant appropriate permissions to app_runtime_role
GRANT USAGE ON SCHEMA public TO app_runtime_role;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO app_runtime_role;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO app_runtime_role;

