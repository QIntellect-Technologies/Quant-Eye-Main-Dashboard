from flask import Flask, jsonify, redirect, render_template, request, session
import os
import json
import secrets
import uuid
import urllib.request
import urllib.error
import threading
from functools import wraps
from datetime import datetime
from urllib.parse import urlencode, urlsplit
from typing import Any, Dict
from werkzeug.security import check_password_hash
from jinja2 import ChoiceLoader, FileSystemLoader, PrefixLoader
import database as db
import logging
logger = logging.getLogger("quanteye.main_dashboard")

try:
    import cloud_database as cloud_db
except Exception as _cdb_err:
    cloud_db = None
    logger.warning("Cloud PostgreSQL module not loaded: %s", _cdb_err)

app = Flask(__name__)
_production_runtime = (
    bool(os.environ.get("RAILWAY_ENVIRONMENT"))
    or os.environ.get("APP_ENV", "").lower() == "production"
    or os.environ.get("FLASK_ENV", "").lower() == "production"
)
_configured_secret_key = os.environ.get("FLASK_SECRET_KEY")
if _production_runtime and len((_configured_secret_key or "").strip()) < 32:
    raise RuntimeError("FLASK_SECRET_KEY must contain at least 32 characters in production.")
app.secret_key = _configured_secret_key or secrets.token_urlsafe(48)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax'
)

# Configure Jinja template loader to reuse existing pages without creating new ones
_main_templates_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")
_local_templates_dir = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "Local-Dashboard", "templates"))
_loaders = [FileSystemLoader(_main_templates_dir)]
if os.path.isdir(_local_templates_dir):
    _loaders.append(FileSystemLoader(_local_templates_dir))
    _loaders.append(PrefixLoader({"local": FileSystemLoader(_local_templates_dir)}))
app.jinja_loader = ChoiceLoader(_loaders)

# Ensure database schema and default control plane users are initialized
db.init_db()
db.ensure_default_users()

DEFAULT_CLIENT_URL = os.environ.get("CLIENT_DASHBOARD_URL") or os.environ.get("LOCAL_DASHBOARD_URL") or "https://quanteye-ai-attendence-production.up.railway.app"
LOCAL_DASHBOARD_URL = os.environ.get("LOCAL_DASHBOARD_URL", DEFAULT_CLIENT_URL)
LOCAL_HTTP_TIMEOUT = float(os.environ.get("LOCAL_HTTP_TIMEOUT", "3.0"))


def _validate_service_url(url: str, name: str) -> str:
    parsed = urlsplit(url)
    local_http = parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    development = (
        app.testing
        or os.environ.get("FLASK_ENV", "").lower() in {"development", "testing"}
        or os.environ.get("APP_ENV", "").lower() in {"development", "test"}
    )
    if parsed.scheme != "https" and not (
        not _production_runtime and development and local_http
    ):
        raise ValueError(f"{name} must use HTTPS outside explicit local development/test mode.")
    if not parsed.hostname or parsed.username or parsed.password:
        raise ValueError(f"{name} is not a valid service URL.")
    return url.rstrip("/")


if _production_runtime:
    if len(os.environ.get("LOCAL_DASHBOARD_API_KEY", "").strip()) < 32:
        raise RuntimeError("LOCAL_DASHBOARD_API_KEY must contain at least 32 characters in production.")
    _validate_service_url(LOCAL_DASHBOARD_URL, "LOCAL_DASHBOARD_URL")


_postgres_sync_wakeup = threading.Event()
_postgres_sync_worker_lock = threading.Lock()
_postgres_sync_worker_started = False


def _persist_attendance_event_to_postgres(
    event_payload: Dict[str, Any],
    source_event_id: str,
    organization_id: str,
    branch_id: str,
) -> None:
    if cloud_db is None:
        raise RuntimeError("PostgreSQL persistence module is unavailable.")
    with cloud_db.tenant_context(
        org_id=organization_id,
        branch_id=branch_id,
        is_platform_admin=True,
        enforce_rls=False,
    ) as cur:
        employee_value = str(event_payload.get("user_id") or "1")
        cur.execute(
            "SELECT id FROM employees WHERE organization_id = %s AND (id = %s OR employee_code = %s);",
            (organization_id, employee_value, employee_value),
        )
        employee_row = cur.fetchone()
        employee_id = employee_row["id"] if employee_row else employee_value
        if not employee_row:
            cur.execute(
                """INSERT INTO employees (
                       id, organization_id, branch_id, employee_code, name, is_active
                   ) VALUES (%s, %s, %s, %s, %s, TRUE)
                   ON CONFLICT (organization_id, id) DO NOTHING;""",
                (
                    employee_id,
                    organization_id,
                    branch_id,
                    employee_value,
                    event_payload.get("name", "Employee"),
                ),
            )
        cur.execute(
            """INSERT INTO attendance_events (
                   id, organization_id, branch_id, employee_id, camera_id,
                   timestamp, event_type, confidence, sync_status
               ) VALUES (%s, %s, %s, %s, %s, %s, 'CHECK_IN', %s, 'synced')
               ON CONFLICT (organization_id, id) DO NOTHING;""",
            (
                f"EVT-{source_event_id}",
                organization_id,
                branch_id,
                employee_id,
                event_payload.get("camera_id") or "edge_node",
                event_payload.get("timestamp") or datetime.now().isoformat(),
                float(event_payload.get("confidence") or 1.0),
            ),
        )


def _postgres_attendance_sync_worker() -> None:
    while True:
        try:
            pending_rows = db.get_due_attendance_postgres_sync()
            if not pending_rows:
                _postgres_sync_wakeup.wait(timeout=5)
                _postgres_sync_wakeup.clear()
                continue
            for pending in pending_rows:
                event_id = pending["source_event_id"]
                try:
                    _persist_attendance_event_to_postgres(
                        json.loads(pending["payload_json"]),
                        event_id,
                        pending["organization_id"],
                        pending["branch_id"],
                    )
                    db.mark_attendance_postgres_sync_succeeded(event_id)
                except Exception as error:
                    db.mark_attendance_postgres_sync_failed(event_id, str(error))
                    logger.warning(
                        "PostgreSQL attendance replica for event %s remains pending: %s",
                        event_id,
                        error,
                    )
        except Exception:
            logger.exception("PostgreSQL attendance retry worker failed.")
            _postgres_sync_wakeup.wait(timeout=5)
            _postgres_sync_wakeup.clear()


def _ensure_postgres_attendance_sync_worker() -> None:
    global _postgres_sync_worker_started
    with _postgres_sync_worker_lock:
        if not _postgres_sync_worker_started:
            threading.Thread(
                target=_postgres_attendance_sync_worker,
                name="attendance-postgres-sync",
                daemon=True,
            ).start()
            _postgres_sync_worker_started = True
    _postgres_sync_wakeup.set()


@app.get("/api/config")
def get_system_config():
    """Return cloud deployment endpoints and configuration."""
    return jsonify({
        "client_dashboard_url": DEFAULT_CLIENT_URL,
        "main_dashboard_url": os.environ.get("MAIN_DASHBOARD_URL", "https://quanteye-main-dashboard-production.up.railway.app")
    })


# ==============================================================================
# AUTHENTICATION & ACCESS CONTROL DECORATORS (STEP 6)
# ==============================================================================

def get_current_user():
    """Return the active session user, or None when no valid session exists."""
    user_id = session.get('user_id')
    if user_id:
        user = db.get_user_by_id(user_id)
        if user and user.get('active'):
            return user
    return None

def require_auth(allowed_roles=None):
    """Decorator ensuring request is from an active authenticated user."""
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            user = get_current_user()
            if not user:
                return jsonify({"success": False, "error": "Authentication required. Please sign in."}), 401
            if allowed_roles and user.get('role') not in allowed_roles:
                return jsonify({"success": False, "error": "Access forbidden: insufficient permissions."}), 403
            return f(*args, **kwargs)
        return decorated_function
    return decorator

def check_org_permission(target_org_id):
    """
    Check if current user is authorized for target_org_id.
    platform_admin has access to everything.
    organization_admin has access ONLY to their own organization_id.
    """
    user = get_current_user()
    if not user:
        return False, ("Authentication required", 401)
    if user.get('role') == 'platform_admin':
        return True, None
    if user.get('role') == 'organization_admin':
        user_org_id = user.get('organization_id')
        if user_org_id and user_org_id == target_org_id:
            return True, None
        return False, ("Forbidden: You do not have permission to access another organization.", 403)
    return False, ("Forbidden", 403)


# ==============================================================================
# AUTHENTICATION ENDPOINTS (STEP 6)
# ==============================================================================

@app.post("/api/auth/login")
def auth_login():
    """Authenticate control plane user and create secure session."""
    payload = request.get_json(silent=True) or {}
    email = str(payload.get("email", "")).strip()
    password = str(payload.get("password", "")).strip()

    if not email or not password:
        return jsonify({"success": False, "error": "Email and password are required."}), 400

    user = db.get_user_by_email(email)
    if not user or not user.get('active'):
        return jsonify({"success": False, "error": "Invalid email or account inactive."}), 401

    if not check_password_hash(user['password_hash'], password):
        return jsonify({"success": False, "error": "Incorrect password."}), 401

    # Establish session
    session.clear()
    session['user_id'] = user['id']
    session['role'] = user['role']
    session['org_id'] = user.get('organization_id')
    session['email'] = user['email']
    session['name'] = user['name']

    db.update_user_last_login(user['id'])

    safe_user = {
        "id": user['id'],
        "name": user['name'],
        "email": user['email'],
        "role": user['role'],
        "organization_id": user.get('organization_id')
    }
    return jsonify({"success": True, "message": "Sign in successful", "user": safe_user}), 200

@app.post("/api/auth/logout")
def auth_logout():
    """Clear session and log user out."""
    session.clear()
    return jsonify({"success": True, "message": "Logged out successfully"}), 200

@app.get("/api/auth/me")
def auth_me():
    """Return currently authenticated control plane user profile."""
    user = get_current_user()
    if not user:
        return jsonify({"authenticated": False}), 401
    return jsonify({
        "authenticated": True,
        "user": {
            "id": user['id'],
            "name": user['name'],
            "email": user['email'],
            "role": user['role'],
            "organization_id": user.get('organization_id')
        }
    }), 200


# ==============================================================================
# PHASE 2.3: CLOUD EDGE AUTHENTICATION & BATCH INGESTION ENDPOINTS
# ==============================================================================

@app.post("/api/cloud/edge/auth")
def cloud_edge_auth():
    """Authenticate Edge Node and return short-lived Edge JWT."""
    payload = request.get_json(silent=True) or {}
    node_id = str(payload.get("edge_node_id", "")).strip()
    secret = str(payload.get("secret", "")).strip()

    if not node_id or not secret:
        return jsonify({"success": False, "error": "Missing edge_node_id or secret"}), 400

    try:
        import cloud_database as cloud_db
        auth_res = cloud_db.authenticate_edge_node(node_id, secret)
        if not auth_res:
            return jsonify({"success": False, "error": "Invalid edge credentials or edge node inactive/revoked"}), 401
        return jsonify({"success": True, **auth_res}), 200
    except Exception as e:
        return jsonify({"success": False, "error": f"Authentication server error: {e}"}), 500

@app.post("/api/cloud/sync/batch")
def cloud_sync_batch():
    """
    Ingest attendance event batches from authenticated Edge Node.
    NEVER trusts organization_id or branch_id from client payload.
    """
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return jsonify({"success": False, "error": "Missing or malformed Authorization header"}), 401

    token = auth_header.split(" ", 1)[1].strip()

    try:
        import cloud_database as cloud_db
        node_auth = cloud_db.verify_edge_token(token)
        if not node_auth:
            return jsonify({"success": False, "error": "Unauthorized: invalid, expired, or revoked edge token"}), 401

        payload = request.get_json(silent=True) or {}
        batch_seq = int(payload.get("batch_sequence", 1))
        events = payload.get("events", [])

        if not isinstance(events, list):
            return jsonify({"success": False, "error": "Invalid payload: events must be a list"}), 400

        result = cloud_db.ingest_edge_sync_batch(
            edge_node_auth=node_auth,
            batch_sequence=batch_seq,
            events=events
        )

        return jsonify(result), 200
    except Exception as e:
        return jsonify({"success": False, "error": f"Batch ingestion failed: {e}"}), 500


@app.get("/api/cloud/edge/config")
def cloud_edge_config():
    """
    Authoritative configuration retrieval endpoint for authenticated Edge Nodes.
    Server strictly derives organization_id and branch_id from the verified Edge JWT.
    Returns: config_version, cameras, modules, license status.
    """
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return jsonify({"success": False, "error": "Missing or malformed Authorization header"}), 401

    token = auth_header.split(" ", 1)[1].strip()

    try:
        import cloud_database as cloud_db
        node_auth = cloud_db.verify_edge_token(token)
        if not node_auth:
            return jsonify({"success": False, "error": "Unauthorized: invalid, expired, or revoked edge token"}), 401

        config = cloud_db.get_edge_node_configuration(node_auth)
        return jsonify(config), 200
    except Exception as e:
        return jsonify({"success": False, "error": f"Configuration retrieval failed: {e}"}), 500


@app.post("/api/cloud/edge/heartbeat")
def cloud_edge_heartbeat():
    """
    Heartbeat and configuration version check endpoint for authenticated Edge Nodes.
    Updates last_heartbeat timestamp and online status in PostgreSQL.
    """
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return jsonify({"success": False, "error": "Missing or malformed Authorization header"}), 401

    token = auth_header.split(" ", 1)[1].strip()

    try:
        import cloud_database as cloud_db
        node_auth = cloud_db.verify_edge_token(token)
        if not node_auth:
            return jsonify({"success": False, "error": "Unauthorized: invalid, expired, or revoked edge token"}), 401

        payload = request.get_json(silent=True) or {}
        status = str(payload.get("status") or "online").strip().lower()
        if status not in ("online", "offline", "syncing", "error"):
            status = "online"

        sw_ver = payload.get("software_version") or payload.get("version")

        hb_res = cloud_db.update_edge_node_heartbeat(
            edge_node_auth=node_auth,
            status=status,
            software_version=sw_ver
        )
        return jsonify(hb_res), 200
    except Exception as e:
        return jsonify({"success": False, "error": f"Heartbeat update failed: {e}"}), 500


# ==============================================================================
# PAGE ROUTES (REUSING EXISTING PAGES — ZERO NEW PAGES CREATED)
# ==============================================================================

@app.get("/")
def dashboard():
    is_authenticated = get_current_user() is not None
    return render_template(
        "index.html",
        modules=db.get_module_catalog() if is_authenticated else [],
        organizations=db.get_all_organizations() if is_authenticated else [],
        is_authenticated=is_authenticated,
        client_dashboard_url=DEFAULT_CLIENT_URL,
    )


@app.get("/attendance-report")
def attendance_report_page():
    """Serve the existing Attendance Report page connected to Cloud PostgreSQL."""
    return render_template("attendance_report.html")


@app.get("/client/portal")
@app.get("/client-portal")
def client_portal_page():
    """Send client portal visitors to the configured portal with their requested tenant context."""
    tenant_context = {}
    for key in ("organization_id", "branch_id"):
        value = request.args.get(key, "").strip()
        if value:
            tenant_context[key] = value
    portal_url = f"{DEFAULT_CLIENT_URL.rstrip('/')}/"
    if tenant_context:
        portal_url = f"{portal_url}?{urlencode(tenant_context)}"
    return redirect(portal_url, code=302)


@app.get("/api/dashboard/stats")
def api_dashboard_stats():
    """Return real system KPIs derived directly from PostgreSQL, falling back safely to SQLite control plane stats."""
    try:
        user = get_current_user()
        stats = cloud_db.get_cloud_kpis(user_context=None)
        return jsonify({
            "success": True,
            "stats": stats
        })
    except Exception as e:
        logger.warning("Could not query PostgreSQL KPIs, using control plane fallback: %s", e)
        try:
            orgs = db.get_all_organizations()
            total_orgs = len(orgs)
            active_orgs = sum(1 for o in orgs if o.get('status') == 'active')
            total_branches = sum(len(o.get('branches', [])) for o in orgs)
            builds = db.get_all_builds()
            return jsonify({
                "success": True,
                "stats": {
                    "total_organizations": total_orgs,
                    "active_organizations": active_orgs,
                    "total_branches": total_branches,
                    "total_builds": len(builds),
                    "total_users": 0,
                    "total_cameras": 0,
                    "today_attendance": 0,
                    "sync_rate": 100.0
                }
            }), 200
        except Exception as inner_e:
            return jsonify({"success": False, "error": str(inner_e)}), 500


# ==============================================================================
# MODULE CATALOG
# ==============================================================================

@app.get("/api/modules")
@require_auth()
def list_modules():
    try:
        user = get_current_user()
        modules = db.get_module_catalog()
        # If organization_admin, show which modules are licensed/enabled for their organization
        if user.get('role') == 'organization_admin':
            org = db.get_organization_by_id(user.get('organization_id'))
            enabled_set = set(org.get('modules', [])) if org else set()
            for m in modules:
                m['licensed_for_org'] = m['id'] in enabled_set
        return jsonify(modules)
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


# ==============================================================================
# ORGANIZATIONS ENDPOINTS
# ==============================================================================

@app.get("/api/organizations")
@require_auth()
def get_organizations():
    try:
        user = get_current_user()
        if user.get('role') == 'organization_admin':
            org = db.get_organization_by_id(user.get('organization_id'))
            return jsonify([org] if org else [])
        orgs = db.get_all_organizations()
        return jsonify(orgs)
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.get("/api/organizations/<org_id>")
@require_auth()
def get_organization(org_id):
    allowed, err = check_org_permission(org_id)
    if not allowed:
        return jsonify({"success": False, "error": err[0]}), err[1]

    try:
        org = db.get_organization_by_id(org_id)
        if not org:
            return jsonify({"success": False, "error": f"Organization '{org_id}' not found."}), 404
        return jsonify(org)
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.post("/api/organizations")
@require_auth(allowed_roles=['platform_admin'])
def create_organization():
    payload = request.get_json(silent=True) or {}
    name = str(payload.get("name", "")).strip()
    if not name:
        return jsonify({"success": False, "error": "Organization name is required."}), 400

    try:
        org = db.create_organization(payload)
        return jsonify(org), 201
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.put("/api/organizations/<org_id>")
@require_auth()
def update_organization(org_id):
    allowed, err = check_org_permission(org_id)
    if not allowed:
        return jsonify({"success": False, "error": err[0]}), err[1]

    payload = request.get_json(silent=True) or {}
    try:
        updated = db.update_organization(org_id, payload)
        if not updated:
            return jsonify({"success": False, "error": f"Organization '{org_id}' not found."}), 404
        return jsonify(updated)
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.delete("/api/organizations/<org_id>")
@require_auth(allowed_roles=['platform_admin'])
def delete_organization(org_id):
    try:
        # Delete from SQLite (local fallback DB)
        success = db.delete_organization(org_id)

        # Delete from PostgreSQL cloud DB (prevents orgs from reappearing after restart)
        if cloud_db is not None:
            try:
                cloud_db.delete_organization(org_id)
            except Exception as ce:
                logger.warning(f"cloud_db.delete_organization failed for {org_id}: {ce}")

        if not success and cloud_db is None:
            return jsonify({"success": False, "error": f"Organization '{org_id}' not found."}), 404

        # Also purge organization's data from Client Portal (Local Dashboard)
        try:
            import urllib.request
            portal_url = os.environ.get("CLIENT_PORTAL_URL", "https://quanteye-ai-attendence-production.up.railway.app").rstrip('/')
            purge_req = urllib.request.Request(
                f"{portal_url}/api/organizations/{org_id}/purge",
                data=b"{}",
                headers={"Content-Type": "application/json", "X-Client-Auth": "true"},
                method="POST"
            )
            urllib.request.urlopen(purge_req, timeout=5.0)
        except Exception as pe:
            logger.debug(f"Note notifying client portal to purge organization {org_id}: {pe}")

        return jsonify({"success": True, "message": f"Organization '{org_id}' deleted."})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.post("/api/organizations/<org_id>/toggle")
@require_auth(allowed_roles=['platform_admin'])
def toggle_organization_status(org_id):
    try:
        new_status = db.toggle_organization_status(org_id)
        if not new_status:
            return jsonify({"success": False, "error": f"Organization '{org_id}' not found."}), 404
        return jsonify({"success": True, "status": new_status})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


# ==============================================================================
# BRANCHES ENDPOINTS
# ==============================================================================

@app.get("/api/branches")
@require_auth()
def list_branches():
    org_id = request.args.get("organization_id")
    user = get_current_user()
    if user.get('role') == 'organization_admin':
        org_id = user.get('organization_id')

    try:
        orgs = db.get_all_organizations()
        branches = []
        for o in orgs:
            if not org_id or o["id"] == org_id:
                for b in o.get("branches", []):
                    branches.append({**b, "organizationId": o["id"], "organizationName": o["name"]})
        return jsonify(branches)
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.post("/api/organizations/<org_id>/branches/<branch_id>/access")
@require_auth()
def update_branch_access(org_id, branch_id):
    allowed, err = check_org_permission(org_id)
    if not allowed:
        return jsonify({"success": False, "error": err[0]}), err[1]
    payload = request.get_json(silent=True) or {}
    access = payload.get("access", "Suspended")
    suspension = payload.get("suspension")
    try:
        success = db.update_branch_access(org_id, branch_id, access, suspension)
        return jsonify({"success": success})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.post("/api/organizations/<org_id>/branches/<branch_id>/reactivate")
@require_auth()
def reactivate_branch(org_id, branch_id):
    allowed, err = check_org_permission(org_id)
    if not allowed:
        return jsonify({"success": False, "error": err[0]}), err[1]
    try:
        success = db.reactivate_branch(org_id, branch_id)
        return jsonify({"success": success})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.post("/api/organizations/<org_id>/branches/<branch_id>/grace")
@require_auth()
def update_branch_grace(org_id, branch_id):
    allowed, err = check_org_permission(org_id)
    if not allowed:
        return jsonify({"success": False, "error": err[0]}), err[1]
    payload = request.get_json(silent=True) or {}
    grace_days = int(payload.get("graceDays", 0) or 0)
    try:
        success = db.update_branch_grace(org_id, branch_id, grace_days)
        return jsonify({"success": success})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


# ==============================================================================
# LICENSES & TOKENS ENDPOINTS
# ==============================================================================

@app.post("/api/organizations/<org_id>/rotate-token")
@require_auth()
def rotate_token(org_id):
    allowed, err = check_org_permission(org_id)
    if not allowed:
        return jsonify({"success": False, "error": err[0]}), err[1]
    payload = request.get_json(silent=True) or {}
    starts_at = payload.get("startsAt", "")
    expires_at = payload.get("expiresAt", "")
    validity_days = int(payload.get("validityDays", 30) or 30)
    token_key = payload.get("tokenKey")
    try:
        result = db.rotate_organization_token(org_id, starts_at, expires_at, validity_days, token_key)
        if not result:
            return jsonify({"success": False, "error": f"Organization '{org_id}' not found."}), 404
        return jsonify(result)
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


# ==============================================================================
# BRANDING ENDPOINTS
# ==============================================================================

@app.post("/api/organizations/<org_id>/branding")
@require_auth()
def update_branding(org_id):
    allowed, err = check_org_permission(org_id)
    if not allowed:
        return jsonify({"success": False, "error": err[0]}), err[1]
    payload = request.get_json(silent=True) or {}
    app_title = str(payload.get("appTitle", "")).strip()
    logo_url = str(payload.get("logo", "")).strip()
    try:
        success = db.update_organization_branding(org_id, app_title, logo_url)
        return jsonify({"success": success})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


# ==============================================================================
# BUILDS ENDPOINTS
# ==============================================================================

@app.get("/api/builds")
@require_auth()
def list_builds():
    try:
        user = get_current_user()
        builds = db.get_all_builds()
        if user.get("role") == "organization_admin":
            org_id = user.get("organization_id")
            builds = [b for b in builds if b.get("organizationId") == org_id]
        return jsonify(builds)
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.post("/api/builds")
@require_auth()
def create_build():
    payload = request.get_json(silent=True) or {}
    user = get_current_user()
    if user.get("role") == "organization_admin":
        payload["organizationId"] = user.get("organization_id")
    try:
        build = db.create_build(payload)
        return jsonify(build), 201
    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 400
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.put("/api/builds/<build_id>")
@require_auth()
def update_build(build_id):
    payload = request.get_json(silent=True) or {}
    user = get_current_user()
    if user.get("role") == "organization_admin":
        builds = db.get_all_builds()
        existing = next((b for b in builds if b.get("id") == build_id), None)
        if not existing or existing.get("organizationId") != user.get("organization_id"):
            return jsonify({"success": False, "error": "Access denied: build belongs to another organization"}), 403
    try:
        success = db.update_build(build_id, payload)
        return jsonify({"success": success})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.delete("/api/builds/<build_id>")
@require_auth()
def delete_build(build_id):
    user = get_current_user()
    if user.get("role") == "organization_admin":
        builds = db.get_all_builds()
        existing = next((b for b in builds if b.get("id") == build_id), None)
        if not existing or existing.get("organizationId") != user.get("organization_id"):
            return jsonify({"success": False, "error": "Access denied: build belongs to another organization"}), 403
    try:
        success = db.delete_build(build_id)
        return jsonify({"success": success})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


# ==============================================================================
# INVOICES & PAYMENTS ENDPOINTS
# ==============================================================================

@app.get("/api/invoices")
@require_auth()
def list_invoices():
    try:
        user = get_current_user()
        invoices = db.get_all_invoices()
        if user.get("role") == "organization_admin":
            org_id = user.get("organization_id")
            invoices = [inv for inv in invoices if inv.get("organizationId") == org_id]
        return jsonify(invoices)
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.post("/api/invoices")
@require_auth(allowed_roles=['platform_admin'])
def create_invoice():
    payload = request.get_json(silent=True) or {}
    try:
        invoice = db.create_invoice(payload)
        return jsonify(invoice), 201
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.put("/api/invoices/<invoice_id>")
@require_auth(allowed_roles=['platform_admin'])
def update_invoice(invoice_id):
    payload = request.get_json(silent=True) or {}
    try:
        success = db.update_invoice(invoice_id, payload)
        return jsonify({"success": success})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.post("/api/invoices/<invoice_id>/payments")
@require_auth()
def record_payment(invoice_id):
    payload = request.get_json(silent=True) or {}
    user = get_current_user()
    if user.get("role") == "organization_admin":
        invoices = db.get_all_invoices()
        existing = next((inv for inv in invoices if inv.get("id") == invoice_id), None)
        if not existing or existing.get("organizationId") != user.get("organization_id"):
            return jsonify({"success": False, "error": "Access denied: invoice belongs to another organization"}), 403
    try:
        success = db.record_payment(invoice_id, payload)
        return jsonify({"success": success})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


# ==============================================================================
# PLATFORM ACTIVITY / AUDIT LOGS ENDPOINTS
# ==============================================================================

@app.get("/api/activity")
@require_auth()
def list_activity():
    try:
        user = get_current_user()
        limit = int(request.args.get("limit", 50))
        logs = db.get_all_audit_logs(limit)
        if user.get("role") == "organization_admin":
            org_id = user.get("organization_id")
            logs = [l for l in logs if l.get("organizationId") == org_id]
        return jsonify(logs)
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.post("/api/activity")
@require_auth()
def log_activity():
    payload = request.get_json(silent=True) or {}
    message = str(payload.get("message", "")).strip()
    org_id = payload.get("organizationId")
    branch_id = payload.get("branchId")
    user = get_current_user()
    if user.get("role") == "organization_admin":
        org_id = user.get("organization_id")
    if not message:
        return jsonify({"success": False, "error": "Message is required."}), 400
    try:
        db.record_audit_log(message, org_id, branch_id)
        return jsonify({"success": True}), 201
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


# ==============================================================================
# ONE-TIME SAFE LOCALSTORAGE MIGRATION ENDPOINT
# ==============================================================================

@app.post("/api/sync-localstorage")
@require_auth(allowed_roles=['platform_admin'])
def sync_localstorage():
    """Import existing browser localStorage data if dashboard.db only has default seed."""
    payload = request.get_json(silent=True) or {}
    imported = {"organizations": 0, "builds": 0, "invoices": 0}
    try:
        # Import orgs if provided
        client_orgs = payload.get("organizations", [])
        if client_orgs:
            for co in client_orgs:
                existing = db.get_organization_by_id(co.get("id"))
                if not existing and co.get("name"):
                    db.create_organization(co)
                    imported["organizations"] += 1
                elif existing:
                    db.update_organization(co["id"], co)

        # Import builds if provided
        client_builds = payload.get("builds", [])
        existing_builds = {b["id"] for b in db.get_all_builds()}
        for cb in client_builds:
            if cb.get("id") not in existing_builds:
                db.create_build(cb)
                imported["builds"] += 1

        # Import invoices if provided
        client_invoices = payload.get("invoices", [])
        existing_invoices = {inv["id"] for inv in db.get_all_invoices()}
        for ci in client_invoices:
            if ci.get("id") not in existing_invoices:
                db.create_invoice(ci)
                imported["invoices"] += 1

        return jsonify({"success": True, "imported": imported})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


# ==============================================================================
# STEP 4: LOCAL DASHBOARD INTEGRATION CLIENT ENDPOINTS
# ==============================================================================

def query_local_dashboard(endpoint, method="GET", payload=None):
    """Safely query Local Dashboard via HTTP. Never crash Main Dashboard on failure."""
    try:
        local_base_url = _validate_service_url(LOCAL_DASHBOARD_URL, "LOCAL_DASHBOARD_URL")
    except ValueError as error:
        return {"ok": False, "status_code": 503, "error": str(error)}
    url = f"{local_base_url}{endpoint}"
    data = None
    headers = {"Content-Type": "application/json"}
    endpoint_path = urlsplit(endpoint).path.rstrip("/")
    if endpoint_path in {
        "/api/integration/sync",
        "/api/integration/edge-sync-state",
        "/api/integration/cameras",
        "/api/integration/employees",
        "/api/attendance/edge-mark",
    } or endpoint_path.startswith("/api/integration/employees/"):
        integration_key = os.environ.get("LOCAL_DASHBOARD_API_KEY", "").strip()
        if len(integration_key) < 32:
            return {
                "ok": False,
                "status_code": 503,
                "error": "Local Dashboard integration authentication is not securely configured."
            }
        headers["X-Integration-Key"] = integration_key
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")

    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=LOCAL_HTTP_TIMEOUT) as response:
            res_body = response.read().decode("utf-8")
            return {
                "ok": True,
                "status_code": response.status,
                "data": json.loads(res_body) if res_body else {}
            }
    except urllib.error.HTTPError as e:
        error_body = ""
        try:
            error_body = e.read().decode("utf-8")
        except Exception:
            pass
        return {
            "ok": False,
            "status_code": e.code,
            "error": f"HTTP Error {e.code}: {e.reason}",
            "details": error_body
        }
    except Exception as e:
        return {
            "ok": False,
            "status_code": None,
            "error": f"Connection failed: {str(e)}"
        }


@app.get("/api/integration/local/status")
@require_auth()
def get_local_dashboard_status():
    """
    Check Local Dashboard health and integration status.
    Returns connected=False with safe error if Local Dashboard is offline.
    """
    # 1. Heartbeat check on existing /api/health
    health_resp = query_local_dashboard("/api/health", method="GET")
    if not health_resp.get("ok"):
        return jsonify({
            "connected": False,
            "local_status": "disconnected",
            "error": health_resp.get("error", "Local Dashboard unavailable"),
            "target_url": LOCAL_DASHBOARD_URL,
            "timestamp": datetime.now().isoformat()
        }), 200

    # 2. Detailed integration contract status
    status_resp = query_local_dashboard("/api/integration/status", method="GET")
    if not status_resp.get("ok"):
        return jsonify({
            "connected": True,
            "local_status": "healthy",
            "linked": False,
            "error": "Integration status endpoint unreachable",
            "timestamp": datetime.now().isoformat()
        }), 200

    local_data = status_resp.get("data", {})
    return jsonify({
        "connected": True,
        "local_status": local_data.get("local_status", "healthy"),
        "linked": local_data.get("linked", False),
        "organization_id": local_data.get("organization_id"),
        "organization_name": local_data.get("organization_name"),
        "branch_id": local_data.get("branch_id"),
        "branch_name": local_data.get("branch_name"),
        "branch_status": local_data.get("branch_status"),
        "license_id": local_data.get("license_id"),
        "license_status": local_data.get("license_status"),
        "license_start": local_data.get("license_start"),
        "license_expiry": local_data.get("license_expiry"),
        "licensed_modules": local_data.get("licensed_modules", []),
        "last_sync_timestamp": local_data.get("last_sync_timestamp"),
        "local_instance_id": local_data.get("local_instance_id"),
        "timestamp": datetime.now().isoformat()
    }), 200


@app.post("/api/integration/local/sync")
@require_auth()
def sync_to_local_dashboard():
    """
    Push organization, branch, and license entitlement from Main Dashboard to Local Dashboard.
    """
    user = get_current_user()
    payload = request.get_json(silent=True) or {}
    org_id = payload.get("organization_id")
    if user.get("role") == "organization_admin":
        if org_id and org_id != user.get("organization_id"):
            return jsonify({"success": False, "error": "Access denied: cannot sync another organization's branch"}), 403
        org_id = user.get("organization_id")

    branch_id = payload.get("branch_id")

    # If org_id or branch_id provided, fetch details from Main DB dashboard.db
    org_name = payload.get("organization_name")
    branch_name = payload.get("branch_name")
    branch_status = payload.get("branch_status", "active")
    modules = payload.get("licensed_modules")

    if org_id:
        org = db.get_organization_by_id(org_id)
        if org:
            org_name = org.get("name")
            if modules is None:
                modules = org.get("modules", [])
            if branch_id:
                for b in org.get("branches", []):
                    if b.get("id") == branch_id:
                        branch_name = b.get("name")
                        branch_status = b.get("status", "active")
                        break

    # Prepare sync package
    sync_package = {
        "organization_id": org_id,
        "organization_name": org_name,
        "branch_id": branch_id,
        "branch_name": branch_name,
        "branch_status": branch_status,
        "license_id": payload.get("license_id", "LIC-DEFAULT"),
        "license_status": payload.get("license_status", "active"),
        "license_start": payload.get("license_start"),
        "license_expiry": payload.get("license_expiry"),
        "licensed_modules": modules if modules is not None else [
            "attendance", "employees", "leave", "overtime",
            "shifts", "reports", "face-recognition", "payroll"
        ],
        "local_instance_id": payload.get("local_instance_id", "LOC-INST-001")
    }

    sync_resp = query_local_dashboard("/api/integration/sync", method="POST", payload=sync_package)
    if not sync_resp.get("ok"):
        return jsonify({
            "success": False,
            "error": sync_resp.get("error", "Failed to sync to Local Dashboard"),
            "details": sync_resp.get("details")
        }), 502

    # Log activity in Main Dashboard dashboard.db
    try:
        db.record_audit_log(f"Synchronized branch {branch_id} ({branch_name}) with Local Dashboard", org_id, branch_id)
    except Exception:
        pass

    return jsonify({
        "success": True,
        "synced": sync_package,
        "response": sync_resp.get("data")
    }), 200


# ==============================================================================
# CLOUD CAMERA CONFIGURATION & REAL-TIME EVENT INGESTION
# ==============================================================================

@app.get("/api/branches/<branch_id>/employees")
def get_employees_for_branch(branch_id):
    """Fetch cloud-registered employees for a specific branch for Client Node synchronization."""
    branch_key = request.headers.get("X-Branch-Key", "").strip()
    if branch_key:
        branch_info = db.get_branch_by_token_key(branch_key)
        if not branch_info or str(branch_info.get("branch_id")) != str(branch_id):
            return jsonify({"success": False, "error": "Valid branch credentials are required."}), 401
        if branch_info.get("organization_status") != "Active" or branch_info.get("branch_access") != "Active":
            return jsonify({"success": False, "error": "Branch access is suspended or inactive."}), 403
        org_id = str(branch_info["organization_id"])
    else:
        user = get_current_user()
        if not user:
            return jsonify({"success": False, "error": "Branch credentials or dashboard authentication are required."}), 401
        org_id = request.args.get("organization_id", "")
        if user.get("role") == "organization_admin":
            org_id = user.get("organization_id")
        elif user.get("role") != "platform_admin":
            return jsonify({"success": False, "error": "Access forbidden."}), 403
        organization = db.get_organization_by_id(org_id) if org_id else None
        branches = organization.get("branches", []) if organization else []
        if not any(str(branch.get("id")) == str(branch_id) for branch in branches):
            return jsonify({"success": False, "error": "Branch was not found for this organization."}), 404

    portal_response = query_local_dashboard(
        f"/api/integration/employees?organization_id={org_id}&branch_id={branch_id}"
    )
    if not portal_response.get("ok"):
        logger.warning(
            "Client Portal employee relay failed for branch %s (HTTP %s).",
            branch_id,
            portal_response.get("status_code"),
        )
        return jsonify({
            "success": False,
            "error": "Employee profiles are temporarily unavailable; cached branch profiles are preserved."
        }), 503

    portal_data = portal_response.get("data") or {}
    if (
        portal_data.get("success") is not True
        or str(portal_data.get("organization_id")) != org_id
        or str(portal_data.get("branch_id")) != str(branch_id)
        or not isinstance(portal_data.get("employees"), list)
    ):
        return jsonify({"success": False, "error": "Client Portal returned an invalid employee response."}), 502
    employees = portal_data["employees"]
    return jsonify({
        "success": True,
        "organization_id": org_id,
        "branch_id": branch_id,
        "employees": employees,
        "count": len(employees),
    }), 200


def _normalize_branch_cameras(cameras):
    normalized = []
    for camera in cameras:
        camera_id = str(camera.get("camera_id") or camera.get("id") or "").strip()
        rtsp_url = str(camera.get("rtsp_url") or "").strip()
        active = str(camera.get("active", 1)).strip().lower()
        if not camera_id or not rtsp_url or active in {"0", "false", "inactive", "disabled"}:
            continue
        camera_name = camera.get("name") or camera.get("camera_name") or f"Camera {camera_id}"
        normalized.append({
            **camera,
            "id": camera_id,
            "camera_id": camera_id,
            "name": camera_name,
            "camera_name": camera_name,
            "rtsp_url": rtsp_url,
            "room_name": camera.get("room_name") or camera.get("location") or "AI ROOM",
            "active": 1,
        })
    return normalized


def _load_branch_cameras(org_id, branch_id):
    """Load branch cameras from the client portal, then cloud DB or local cache."""
    query = urlencode({"organization_id": org_id, "branch_id": branch_id, "active_only": "true"})
    portal_result = query_local_dashboard(f"/api/integration/cameras?{query}")
    if portal_result.get("ok"):
        portal_data = portal_result.get("data") or {}
        if (
            portal_data.get("success")
            and str(portal_data.get("organization_id")) == str(org_id)
            and str(portal_data.get("branch_id")) == str(branch_id)
            and isinstance(portal_data.get("cameras"), list)
        ):
            cameras = _normalize_branch_cameras(portal_data["cameras"])
            try:
                db.set_branch_cameras(org_id, branch_id, cameras)
            except Exception:
                logger.exception("Could not refresh local branch camera cache.")
            if cloud_db is not None:
                try:
                    cloud_db.set_cloud_cameras(org_id, branch_id, cameras)
                except Exception as error:
                    logger.debug("Could not mirror portal branch cameras to PostgreSQL: %s", error)
            return cameras
        logger.warning("Client portal returned an invalid branch camera response for %s/%s.", org_id, branch_id)
    else:
        logger.warning(
            "Could not load branch cameras from Client Portal (HTTP %s): %s",
            portal_result.get("status_code"),
            portal_result.get("error"),
        )

    if cloud_db is not None:
        try:
            return _normalize_branch_cameras(cloud_db.get_cloud_cameras(org_id, branch_id))
        except Exception as error:
            logger.warning("Could not query PostgreSQL branch cameras: %s", error)

    cached_cameras = db.get_branch_cameras(org_id, branch_id)
    return _normalize_branch_cameras(cached_cameras) if cached_cameras else None


@app.get("/api/branches/<branch_id>/cameras")
def get_cameras_for_branch(branch_id):
    """Fetch cloud-configured camera streams for a client node or dashboard."""
    branch_key = request.headers.get("X-Branch-Key", "")
    if branch_key:
        branch_info = db.get_branch_by_token_key(branch_key)
        if not branch_info or branch_info.get("branch_id") != branch_id:
            return jsonify({"success": False, "error": "Valid branch credentials are required."}), 401
        if branch_info.get("organization_status") != "Active" or branch_info.get("branch_access") != "Active":
            return jsonify({"success": False, "error": "Branch access is suspended or inactive."}), 403
        org_id = branch_info["organization_id"]
    else:
        user = get_current_user()
        if not user:
            return jsonify({"success": False, "error": "Branch credentials or dashboard authentication are required."}), 401
        org_id = request.args.get("organization_id", "")
        if user.get("role") == "organization_admin":
            org_id = user.get("organization_id")
        elif user.get("role") != "platform_admin":
            return jsonify({"success": False, "error": "Access forbidden."}), 403
        organization = db.get_organization_by_id(org_id) if org_id else None
        branches = organization.get("branches", []) if organization else []
        if not any(str(branch.get("id")) == str(branch_id) for branch in branches):
            return jsonify({"success": False, "error": "Branch was not found for this organization."}), 404

    cameras = _load_branch_cameras(org_id, branch_id)
    if cameras is None:
        return jsonify({"success": False, "error": "Branch camera configuration is temporarily unavailable."}), 503
    return jsonify({
        "success": True,
        "organization_id": org_id,
        "branch_id": branch_id,
        "cameras": cameras,
        "count": len(cameras)
    }), 200


@app.post("/api/branches/<branch_id>/cameras")
@require_auth()
def update_cameras_for_branch(branch_id):
    """Save cloud-configured cameras for a branch and increment config_version."""
    payload = request.get_json(silent=True) or {}
    org_id = payload.get("organization_id")
    user = get_current_user()
    if user and user.get("role") == "organization_admin":
        org_id = user.get("organization_id")
    elif not org_id:
        org_id = "ORG-001"

    cameras = payload.get("cameras", [])

    # 1. Update in PostgreSQL and increment config_version
    new_version = 1
    try:
        import cloud_database as cloud_db
        new_version = cloud_db.set_cloud_cameras(org_id, branch_id, cameras)
    except Exception as e:
        logger.warning("Could not persist cameras to PostgreSQL: %s", e)

    # 2. Keep local SQLite mirror synchronized
    try:
        db.set_branch_cameras(org_id, branch_id, cameras)
        db.record_audit_log(f"Updated {len(cameras)} cameras for Branch {branch_id} (version {new_version})", org_id, branch_id)
    except Exception:
        pass

    return jsonify({
        "success": True,
        "message": f"Successfully updated {len(cameras)} cameras for Branch {branch_id}",
        "config_version": new_version,
        "cameras": cameras
    }), 200


@app.post("/api/sync/attendance-event")
def ingest_attendance_event():
    """
    Real-time push ingestion endpoint.
    Called immediately by QuantEye-Client-Node whenever a face is recognized.
    """
    event_payload = request.get_json(silent=True) or {}
    if not isinstance(event_payload, dict):
        return jsonify({"success": False, "error": "Invalid attendance event"}), 400
    branch_key = request.headers.get("X-Branch-Key", "").strip()
    if not branch_key:
        return jsonify({"success": False, "error": "Branch authentication required"}), 401
    branch_info = db.get_branch_by_token_key(branch_key)
    if not branch_info:
        return jsonify({"success": False, "error": "Invalid branch credential"}), 401
    if (
        branch_info.get("organization_status") != "Active"
        or branch_info.get("branch_access") != "Active"
    ):
        return jsonify({"success": False, "error": "Organization or branch is inactive"}), 403
    expires_at = branch_info.get("token_expires_at")
    if expires_at:
        try:
            expiry = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
            now = datetime.now(expiry.tzinfo) if expiry.tzinfo else datetime.now()
            if expiry <= now:
                return jsonify({"success": False, "error": "Branch credential has expired"}), 401
        except ValueError:
            logger.error("Invalid expiry timestamp for branch credential %s", branch_info.get("branch_id"))
            return jsonify({"success": False, "error": "Branch credential configuration is invalid"}), 403

    source_event_id = str(event_payload.get("event_id") or "").strip()
    try:
        source_event_id = str(uuid.UUID(source_event_id))
    except (ValueError, TypeError, AttributeError):
        return jsonify({"success": False, "error": "A valid event_id is required"}), 400
    if not event_payload.get("name") and not event_payload.get("user_id"):
        return jsonify({"success": False, "error": "Missing employee identification"}), 400

    org_id = branch_info["organization_id"]
    branch_id = branch_info["branch_id"]
    event_payload["organization_id"] = org_id
    event_payload["branch_id"] = branch_id
    event_payload["event_id"] = source_event_id
    event_payload.pop("bypass_shift_check", None)

    current_hash = db._live_attendance_event_hash(event_payload)
    previous_event = db.get_live_attendance_sync_event_by_source_id(source_event_id)
    if previous_event and previous_event.get("payload_hash") != current_hash:
        return jsonify({
            "success": False,
            "error": "event_id was already used with a different payload",
        }), 409

    local_result = query_local_dashboard(
        "/api/attendance/edge-mark", method="POST", payload=event_payload
    )
    if not local_result.get("ok"):
        logger.warning(
            "Local attendance relay failed for event %s (HTTP %s): %s",
            source_event_id,
            local_result.get("status_code"),
            local_result.get("error"),
        )
        relay_status = local_result.get("status_code")
        if isinstance(relay_status, int) and 400 <= relay_status < 500:
            return jsonify({
                "success": False,
                "event_id": source_event_id,
                "error": "Local Dashboard rejected the attendance event",
            }), relay_status
        return jsonify({
            "success": False,
            "event_id": source_event_id,
            "error": "Local Dashboard attendance relay is unavailable",
        }), 502

    try:
        event_id, created, payload_matches = db.record_live_attendance_sync_event_once(
            event_payload, source_event_id
        )
    except Exception:
        logger.exception("Could not persist incoming attendance event.")
        return jsonify({"success": False, "error": "Attendance event could not be persisted"}), 500
    if not payload_matches:
        return jsonify({
            "success": False,
            "error": "event_id was already used with a different payload",
        }), 409

    postgres_sync_status = "synced"
    try:
        _persist_attendance_event_to_postgres(
            event_payload, source_event_id, org_id, branch_id
        )
    except Exception as pg_error:
        try:
            db.enqueue_attendance_postgres_sync(
                source_event_id, org_id, branch_id, event_payload
            )
            _ensure_postgres_attendance_sync_worker()
            postgres_sync_status = "pending"
            logger.warning(
                "PostgreSQL attendance replica for event %s is queued for retry: %s",
                source_event_id,
                pg_error,
            )
        except Exception:
            logger.exception(
                "PostgreSQL attendance sync queue could not persist event %s.",
                source_event_id,
            )
            return jsonify({
                "success": False,
                "event_id": source_event_id,
                "error": "Attendance was recorded locally, but cloud persistence could not be queued.",
            }), 503

    return jsonify({
        "success": True,
        "event_id": source_event_id,
        "duplicate": not created,
        "message": f"Attendance event for '{event_payload.get('name')}' accepted by Local Dashboard",
        "postgres_sync_status": postgres_sync_status,
        "timestamp": event_payload.get("timestamp") or datetime.now().isoformat()
    }), 201 if created else 200


@app.get("/api/edge/sync-state")
def get_edge_sync_state():
    """Return branch-bound attendance overrides for an authenticated edge node."""
    branch_key = request.headers.get("X-Branch-Key", "").strip()
    if not branch_key:
        return jsonify({"success": False, "error": "Branch authentication required"}), 401

    branch_info = db.get_branch_by_token_key(branch_key)
    if not branch_info:
        return jsonify({"success": False, "error": "Invalid branch credential"}), 401
    if (
        branch_info.get("organization_status") != "Active"
        or branch_info.get("branch_access") != "Active"
    ):
        return jsonify({"success": False, "error": "Organization or branch is inactive"}), 403

    expires_at = branch_info.get("token_expires_at")
    if expires_at:
        try:
            expiry = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
            now = datetime.now(expiry.tzinfo) if expiry.tzinfo else datetime.now()
            if expiry <= now:
                return jsonify({"success": False, "error": "Branch credential has expired"}), 401
        except ValueError:
            logger.error("Invalid expiry timestamp for branch credential %s", branch_info.get("branch_id"))
            return jsonify({"success": False, "error": "Branch credential configuration is invalid"}), 403

    organization_id = str(branch_info["organization_id"])
    branch_id = str(branch_info["branch_id"])
    local_result = query_local_dashboard(
        f"/api/integration/edge-sync-state?organization_id={organization_id}&branch_id={branch_id}",
        method="GET",
    )
    if not local_result.get("ok"):
        logger.warning(
            "Could not retrieve branch attendance state from Local Dashboard (HTTP %s).",
            local_result.get("status_code"),
        )
        return jsonify({
            "success": False,
            "error": "Attendance state is temporarily unavailable",
        }), 502

    local_state = local_result.get("data") or {}
    if (
        not local_state.get("success")
        or local_state.get("organization_id") != organization_id
        or local_state.get("branch_id") != branch_id
        or not isinstance(local_state.get("manual_absent_user_ids"), list)
    ):
        logger.error("Local Dashboard attendance state did not match the authenticated edge branch.")
        return jsonify({"success": False, "error": "Attendance state binding is invalid"}), 502

    return jsonify({
        "success": True,
        "organization_id": organization_id,
        "branch_id": branch_id,
        "date": local_state.get("date"),
        "manual_absent_user_ids": local_state["manual_absent_user_ids"],
        "synced_at": datetime.now().isoformat(),
    }), 200


@app.post("/api/edge/branches/<branch_id>/employees/<int:user_id>/embeddings")
def sync_edge_employee_embeddings(branch_id, user_id):
    """Relay a trained branch employee's face embeddings to the Client Portal."""
    branch_key = request.headers.get("X-Branch-Key", "").strip()
    if not branch_key:
        return jsonify({"success": False, "error": "Branch authentication required"}), 401

    branch_info = db.get_branch_by_token_key(branch_key)
    if not branch_info or str(branch_info.get("branch_id")) != str(branch_id):
        return jsonify({"success": False, "error": "Invalid branch credential"}), 401
    if (
        branch_info.get("organization_status") != "Active"
        or branch_info.get("branch_access") != "Active"
    ):
        return jsonify({"success": False, "error": "Organization or branch is inactive"}), 403

    expires_at = branch_info.get("token_expires_at")
    if expires_at:
        try:
            expiry = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
            now = datetime.now(expiry.tzinfo) if expiry.tzinfo else datetime.now()
            if expiry <= now:
                return jsonify({"success": False, "error": "Branch credential has expired"}), 401
        except ValueError:
            logger.error("Invalid expiry timestamp for branch credential %s", branch_id)
            return jsonify({"success": False, "error": "Branch credential configuration is invalid"}), 403

    payload = request.get_json(silent=True) or {}
    relay_path = (
        f"/api/integration/employees/{user_id}/embeddings"
        f"?organization_id={branch_info['organization_id']}&branch_id={branch_id}"
    )
    result = query_local_dashboard(relay_path, method="POST", payload=payload)
    if not result.get("ok"):
        logger.warning(
            "Client Portal rejected face embedding sync for employee %s in branch %s (HTTP %s).",
            user_id,
            branch_id,
            result.get("status_code"),
        )
        status = result.get("status_code")
        return jsonify({
            "success": False,
            "error": "Client Portal biometric synchronization is temporarily unavailable.",
        }), status if isinstance(status, int) and 400 <= status < 500 else 502

    response = result.get("data") or {}
    if response.get("success") is not True:
        return jsonify({
            "success": False,
            "error": "Client Portal did not confirm biometric synchronization.",
        }), 502
    return jsonify(response), 200


@app.get("/api/edge/branches/<branch_id>/employees/<int:user_id>/embeddings")
def get_edge_employee_embeddings(branch_id, user_id):
    """Relay trained face templates to the authenticated Client Node for its branch."""
    branch_key = request.headers.get("X-Branch-Key", "").strip()
    if not branch_key:
        return jsonify({"success": False, "error": "Branch authentication required"}), 401

    branch_info = db.get_branch_by_token_key(branch_key)
    if not branch_info or str(branch_info.get("branch_id")) != str(branch_id):
        return jsonify({"success": False, "error": "Invalid branch credential"}), 401
    if (
        branch_info.get("organization_status") != "Active"
        or branch_info.get("branch_access") != "Active"
    ):
        return jsonify({"success": False, "error": "Organization or branch is inactive"}), 403

    expires_at = branch_info.get("token_expires_at")
    if expires_at:
        try:
            expiry = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
            now = datetime.now(expiry.tzinfo) if expiry.tzinfo else datetime.now()
            if expiry <= now:
                return jsonify({"success": False, "error": "Branch credential has expired"}), 401
        except ValueError:
            logger.error("Invalid expiry timestamp for branch credential %s", branch_id)
            return jsonify({"success": False, "error": "Branch credential configuration is invalid"}), 403

    relay_path = (
        f"/api/integration/employees/{user_id}/embeddings"
        f"?organization_id={branch_info['organization_id']}&branch_id={branch_id}"
    )
    result = query_local_dashboard(relay_path, method="GET")
    if not result.get("ok"):
        logger.warning(
            "Could not retrieve face templates for employee %s in branch %s (HTTP %s).",
            user_id,
            branch_id,
            result.get("status_code"),
        )
        status = result.get("status_code")
        return jsonify({
            "success": False,
            "error": "Client Portal biometric templates are temporarily unavailable.",
        }), status if isinstance(status, int) and 400 <= status < 500 else 502

    response = result.get("data") or {}
    vectors = response.get("embeddings")
    if (
        response.get("success") is not True
        or str(response.get("organization_id")) != str(branch_info["organization_id"])
        or str(response.get("branch_id")) != str(branch_id)
        or int(response.get("user_id", -1)) != int(user_id)
        or not isinstance(vectors, list)
        or len(vectors) > 20
    ):
        logger.error("Client Portal returned mismatched face templates for employee %s.", user_id)
        return jsonify({
            "success": False,
            "error": "Client Portal returned invalid biometric templates.",
        }), 502

    return jsonify(response), 200


# ==============================================================================
# PHASE 2.4: CLOUD REPORTING & DATA ENDPOINTS (REAL POSTGRESQL DATA ONLY)
# ==============================================================================

@app.get("/api/cloud/reports/attendance")
@app.get("/api/reports/attendance")
def api_cloud_reports_attendance():
    """Return comprehensive attendance report from real PostgreSQL attendance_events."""
    user = get_current_user()
    org_id = request.args.get("organization_id") or request.args.get("org_id")
    if user and user.get("role") == "organization_admin":
        org_id = user.get("organization_id")
    elif not org_id:
        org_id = "ORG-257281"

    ok, err = check_org_permission(org_id)
    if not ok:
        return jsonify({"success": False, "error": err[0]}), err[1]

    if not cloud_db.check_module_entitlement(org_id, "attendance"):
        return jsonify({"success": False, "error": "Module 'attendance' is not licensed for this organization."}), 403

    branch_id = request.args.get("branch_id") or None
    target_date = request.args.get("date", "").strip() or None
    start_date = request.args.get("start_date", "").strip() or None
    end_date = request.args.get("end_date", "").strip() or None
    department = request.args.get("department", "").strip() or None
    status = request.args.get("status", "").strip() or None
    search = request.args.get("search", "").strip() or None

    report = cloud_db.get_detailed_cloud_attendance_report(
        org_id=org_id,
        branch_id=branch_id,
        target_date=target_date,
        start_date=start_date,
        end_date=end_date,
        department=department,
        status_filter=status,
        search_query=search
    )
    return jsonify(report), 200


@app.get("/api/cloud/staff")
@app.get("/api/users")
@app.get("/api/staff")
def api_cloud_staff_list():
    """Return real employee roster from PostgreSQL with tenant isolation."""
    user = get_current_user()
    org_id = request.args.get("organization_id") or request.args.get("org_id")
    if user and user.get("role") == "organization_admin":
        org_id = user.get("organization_id")
    elif not org_id:
        org_id = "ORG-257281"

    ok, err = check_org_permission(org_id)
    if not ok:
        return jsonify({"success": False, "error": err[0]}), err[1]

    branch_id = request.args.get("branch_id") or None
    search = request.args.get("search", "").strip() or None
    department = request.args.get("department", "").strip() or None

    employees = cloud_db.get_cloud_staff_list(
        org_id=org_id,
        branch_id=branch_id,
        search=search,
        department=department
    )
    return jsonify(employees), 200


@app.get("/api/cloud/leaves")
@app.get("/api/leaves")
def api_cloud_leaves_list():
    """Return real leave requests from PostgreSQL."""
    user = get_current_user()
    org_id = request.args.get("organization_id") or request.args.get("org_id")
    if user and user.get("role") == "organization_admin":
        org_id = user.get("organization_id")
    elif not org_id:
        org_id = "ORG-257281"

    ok, err = check_org_permission(org_id)
    if not ok:
        return jsonify({"success": False, "error": err[0]}), err[1]

    if not cloud_db.check_module_entitlement(org_id, "leave"):
        return jsonify({"success": False, "error": "Module 'leave' is not licensed for this organization."}), 403

    branch_id = request.args.get("branch_id") or None
    status = request.args.get("status", "").strip() or None

    leaves = cloud_db.get_cloud_leave_requests(
        org_id=org_id,
        branch_id=branch_id,
        status=status
    )
    return jsonify(leaves), 200


@app.get("/api/cloud/overtime")
@app.get("/api/overtime")
def api_cloud_overtime_list():
    """Return real overtime records from PostgreSQL."""
    user = get_current_user()
    org_id = request.args.get("organization_id") or request.args.get("org_id")
    if user and user.get("role") == "organization_admin":
        org_id = user.get("organization_id")
    elif not org_id:
        org_id = "ORG-257281"

    ok, err = check_org_permission(org_id)
    if not ok:
        return jsonify({"success": False, "error": err[0]}), err[1]

    if not cloud_db.check_module_entitlement(org_id, "overtime"):
        return jsonify({"success": False, "error": "Module 'overtime' is not licensed for this organization."}), 403

    branch_id = request.args.get("branch_id") or None
    status = request.args.get("status", "").strip() or None

    overtime_list = cloud_db.get_cloud_overtime_records(
        org_id=org_id,
        branch_id=branch_id,
        status=status
    )
    return jsonify(overtime_list), 200


@app.get("/api/cloud/shifts")
@app.get("/api/shifts")
def api_cloud_shifts_list():
    """Return real shifts from PostgreSQL."""
    user = get_current_user()
    org_id = request.args.get("organization_id") or request.args.get("org_id")
    if user and user.get("role") == "organization_admin":
        org_id = user.get("organization_id")
    elif not org_id:
        org_id = "ORG-257281"

    ok, err = check_org_permission(org_id)
    if not ok:
        return jsonify({"success": False, "error": err[0]}), err[1]

    if not cloud_db.check_module_entitlement(org_id, "shifts"):
        return jsonify({"success": False, "error": "Module 'shifts' is not licensed for this organization."}), 403

    branch_id = request.args.get("branch_id") or None

    shifts = cloud_db.get_cloud_shifts(
        org_id=org_id,
        branch_id=branch_id
    )
    return jsonify(shifts), 200


@app.get("/api/cloud/payroll/runs")
@app.get("/api/payroll/runs")
def api_cloud_payroll_runs():
    """Return real payroll runs from PostgreSQL."""
    user = get_current_user()
    org_id = request.args.get("organization_id") or request.args.get("org_id")
    if user and user.get("role") == "organization_admin":
        org_id = user.get("organization_id")
    elif not org_id:
        org_id = "ORG-257281"

    ok, err = check_org_permission(org_id)
    if not ok:
        return jsonify({"success": False, "error": err[0]}), err[1]

    if not cloud_db.check_module_entitlement(org_id, "payroll"):
        return jsonify({"success": False, "error": "Module 'payroll' is not licensed for this organization."}), 403

    branch_id = request.args.get("branch_id") or None

    runs = cloud_db.get_cloud_payroll_runs(
        org_id=org_id,
        branch_id=branch_id
    )
    return jsonify(runs), 200


@app.get("/api/cloud/payroll/runs/<run_id>/records")
@app.get("/api/payroll/runs/<run_id>/records")
def api_cloud_payroll_records(run_id):
    """Return real employee lines for a payroll run from PostgreSQL."""
    user = get_current_user()
    org_id = request.args.get("organization_id") or request.args.get("org_id")
    if user and user.get("role") == "organization_admin":
        org_id = user.get("organization_id")
    elif not org_id:
        org_id = "ORG-257281"

    ok, err = check_org_permission(org_id)
    if not ok:
        return jsonify({"success": False, "error": err[0]}), err[1]

    if not cloud_db.check_module_entitlement(org_id, "payroll"):
        return jsonify({"success": False, "error": "Module 'payroll' is not licensed for this organization."}), 403

    branch_id = request.args.get("branch_id") or None

    records = cloud_db.get_cloud_payroll_records(
        org_id=org_id,
        run_id=run_id,
        branch_id=branch_id
    )
    return jsonify(records), 200


@app.get("/api/cloud/stats")
@app.get("/api/stats")
def api_cloud_stats():
    """Return tenant stats summary."""
    user = get_current_user()
    org_id = request.args.get("organization_id") or request.args.get("org_id")
    if user and user.get("role") == "organization_admin":
        org_id = user.get("organization_id")
    elif not org_id:
        org_id = "ORG-257281"

    employees = cloud_db.get_cloud_staff_list(org_id=org_id)
    return jsonify({
        "total_users": len(employees),
        "total_staff": len(employees),
        "total_departments": len(set(e.get("department") for e in employees if e.get("department"))),
        "organization_id": org_id
    }), 200


@app.get("/api/sync/live-feed")
def get_live_attendance_feed():
    """Fetch real-time synced attendance stream for Cloud Dashboard UI."""
    user = get_current_user()
    org_id = request.args.get("organization_id")
    if user and user.get("role") == "organization_admin":
        org_id = user.get("organization_id")
    elif not org_id:
        org_id = "ORG-001"


    branch_id = request.args.get("branch_id")
    limit = int(request.args.get("limit", 30))
    events = db.get_live_attendance_sync_events(org_id, branch_id=branch_id, limit=limit)
    return jsonify({
        "success": True,
        "organization_id": org_id,
        "branch_id": branch_id,
        "events": events,
        "count": len(events)
    }), 200


@app.post("/api/edge/validate-key")
def validate_edge_branch_key():
    """
    Dynamic Edge Activation endpoint.
    Called by Standalone AI Attendance EXE to authenticate against any organization/branch.
    Works dynamically for ANY newly created organization or branch without hardcoding.
    """
    payload = request.get_json(silent=True) or {}
    branch_key = payload.get("branch_key") or payload.get("key")
    if not branch_key:
        return jsonify({"success": False, "error": "Branch key is required"}), 400

    branch_info = db.get_branch_by_token_key(branch_key)
    if not branch_info:
        return jsonify({"success": False, "error": "Invalid Branch Key. Please verify key from your dashboard."}), 404

    if branch_info.get("organization_status") != "Active" or branch_info.get("branch_access") != "Active":
        return jsonify({
            "success": False,
            "error": "Branch access is suspended or inactive. Please contact your organization administrator."
        }), 403

    org_id = branch_info["organization_id"]
    b_id = branch_info["branch_id"]

    cameras = _load_branch_cameras(org_id, b_id)
    camera_sync_available = cameras is not None
    if cameras is None:
        cameras = []

    return jsonify({
        "success": True,
        "organization_id": org_id,
        "organization_name": branch_info["organization_name"],
        "branch_id": b_id,
        "branch_name": branch_info["branch_name"],
        "token_expires_at": branch_info.get("token_expires_at"),
        "cameras": cameras,
        "camera_count": len(cameras),
        "camera_sync_available": camera_sync_available,
        "message": f"Successfully authenticated {branch_info['organization_name']} - {branch_info['branch_name']}"
    }), 200


@app.post("/api/client/authenticate")
def client_authenticate():
    """
    Authenticate a provisioned client build or a branch activation key.
    When both credentials and a branch key are supplied, they must identify the same branch.
    """
    data = request.get_json(silent=True) or {}
    ident = str(data.get("username") or data.get("email") or "").strip().lower()
    password = data.get("password") or ""
    branch_key = str(data.get("branch_key") or "").strip().upper()
    if not isinstance(password, str):
        return jsonify({"success": False, "error": "Password must be a string."}), 400

    matched_org = None
    matched_branch = None
    authenticated_build = None
    if ident or password:
        if not ident or not password:
            return jsonify({"success": False, "error": "Username and password are both required."}), 400
        authenticated_build = db.authenticate_client_build(ident, password)
        if not authenticated_build:
            return jsonify({"success": False, "error": "Invalid client credentials."}), 401

        matched_org = db.get_organization_by_id(authenticated_build["organization_id"])
        if matched_org:
            matched_branch = next(
                (branch for branch in matched_org.get("branches", [])
                 if branch.get("id") == authenticated_build.get("branch_id")),
                None,
            )
        if not matched_org or not matched_branch:
            return jsonify({"success": False, "error": "Client build is not bound to a valid organization branch."}), 401

        configured_branch_key = str(
            matched_branch.get("tokenKey") or matched_branch.get("token_key") or ""
        ).strip().upper()
        if branch_key and (
            not configured_branch_key
            or not secrets.compare_digest(branch_key, configured_branch_key)
        ):
            return jsonify({"success": False, "error": "Branch key does not match the authenticated client build."}), 401
    elif branch_key:
        branch_info = db.get_branch_by_token_key(branch_key)
        if not branch_info:
            return jsonify({"success": False, "error": "Invalid branch key."}), 401
        matched_org = db.get_organization_by_id(branch_info["organization_id"])
        if matched_org:
            matched_branch = next(
                (branch for branch in matched_org.get("branches", [])
                 if branch.get("id") == branch_info.get("branch_id")),
                None,
            )
        if not matched_org or not matched_branch:
            return jsonify({"success": False, "error": "Branch is not bound to a valid organization."}), 401
    else:
        return jsonify({"success": False, "error": "Client credentials or a branch key are required."}), 400

    if (
        str(matched_org.get("status") or "").strip().lower() != "active"
        or str(matched_branch.get("access") or "").strip().lower() != "active"
    ):
        return jsonify({"success": False, "error": "Organization or branch is inactive."}), 403

    token_expires_at = matched_branch.get("tokenExpiresAt") or matched_branch.get("token_expires_at")
    if token_expires_at:
        try:
            if datetime.strptime(str(token_expires_at)[:10], "%Y-%m-%d").date() < datetime.now().date():
                return jsonify({"success": False, "error": "Branch activation key has expired."}), 403
        except ValueError:
            logger.warning(
                "Rejecting malformed branch key expiration for organization %s branch %s",
                matched_org.get("id"),
                matched_branch.get("id"),
            )
            return jsonify({"success": False, "error": "Branch activation key expiration is invalid."}), 403

    branch_name = matched_branch.get("name") if matched_branch else "Main Branch"
    branch_id = matched_branch.get("id") if matched_branch else "BR-01"
    b_key = (matched_branch.get("tokenKey") or matched_branch.get("token_key") or matched_org.get("tokenKey") or "") if matched_branch else (matched_org.get("tokenKey") or "")
    modules = (
        authenticated_build.get("modules", [])
        if authenticated_build
        else matched_org.get("modules", [])
    )

    return jsonify({
        "success": True,
        "organization_id": matched_org["id"],
        "organization_name": matched_org["name"],
        "branch_id": branch_id,
        "branch_name": branch_name,
        "branch_key": b_key,
        "username": ident or matched_org.get("email", ""),
        "modules": modules,
        "token_expires_at": matched_branch.get("tokenExpiresAt") if matched_branch else None
    }), 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5100"))
    host = os.environ.get("HOST", "0.0.0.0")
    debug = os.environ.get("FLASK_DEBUG", "0") == "1"
    app.run(host=host, port=port, debug=debug)
