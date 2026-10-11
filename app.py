from flask import Flask, jsonify, render_template, request

app = Flask(__name__)

MODULES = [
    {"id": "attendance", "name": "Attendance Management", "icon": "fa-calendar-check", "description": "Track daily attendance, presence, and check-in activity.", "required": True},
    {"id": "employees", "name": "Employee Management", "icon": "fa-users", "description": "Manage employee profiles, departments, and access.", "required": True},
    {"id": "leave", "name": "Leave Management", "icon": "fa-plane-departure", "description": "Handle leave requests, balances, approvals, and policies.", "required": False},
    {"id": "overtime", "name": "Overtime Management", "icon": "fa-clock", "description": "Review overtime hours, approvals, and summaries.", "required": False},
    {"id": "shifts", "name": "Shift Management", "icon": "fa-business-time", "description": "Configure shifts, schedules, grace periods, and rosters.", "required": False},
    {"id": "reports", "name": "Reports & Analytics", "icon": "fa-chart-line", "description": "Generate connected attendance and workforce reports.", "required": False},
    {"id": "face-recognition", "name": "Face Recognition", "icon": "fa-camera", "description": "Enable biometric enrollment and camera recognition.", "required": False},
    {"id": "payroll", "name": "Payroll", "icon": "fa-money-check-dollar", "description": "Prepare payroll inputs from attendance, leave, and overtime.", "required": False},
]

organizations = [
    {
        "id": "ORG-001",
        "name": "Demo Organization",
        "industry": "Technology",
        "status": "Active",
        "modules": ["attendance", "employees", "leave", "reports"],
        "created": "10 Oct 2026",
    }
]


@app.get("/")
def dashboard():
    return render_template("index.html", modules=MODULES, organizations=organizations)


@app.get("/api/modules")
def list_modules():
    return jsonify(MODULES)


@app.post("/api/organizations")
def create_organization():
    payload = request.get_json(silent=True) or {}
    name = str(payload.get("name", "")).strip()
    if not name:
        return jsonify({"error": "Organization name is required."}), 400

    selected = list(dict.fromkeys(payload.get("modules") or ["attendance", "employees"]))
    selected = [module["id"] for module in MODULES if module["id"] in selected or module["required"]]
    organization = {
        "id": f"ORG-{len(organizations) + 1:03d}",
        "name": name,
        "industry": str(payload.get("industry", "Not specified")).strip() or "Not specified",
        "status": "Build Pending",
        "modules": selected,
        "created": "10 Oct 2026",
    }
    organizations.append(organization)
    return jsonify(organization), 201


if __name__ == "__main__":
    app.run(debug=True, port=5100)
