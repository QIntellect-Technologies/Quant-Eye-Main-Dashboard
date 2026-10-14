# QuantEye Main Dashboard

An independent frontend control panel for creating organizations, selecting modules, and testing customized application build workflows.

This project is intentionally separate from `QuantEye-System`. The dashboard frontend currently uses `localStorage` and a simulated build service. It does not generate a real Windows executable and does not call the backend. The local data layer is organized so it can later be replaced by an API client.

## Run

```powershell
python -m pip install -r requirements.txt
python app.py
```

Open <http://127.0.0.1:5100>.

When connecting this service to a Local Dashboard, configure `LOCAL_DASHBOARD_API_KEY` with the same private random value (at least 32 characters) on both services. Main-to-Local configuration synchronization fails closed if the key is missing or too short.

For production, set `FLASK_SECRET_KEY` to a private random value of at least 32 characters, set `LOCAL_DASHBOARD_API_KEY` to a separate private random value of at least 32 characters, and configure `LOCAL_DASHBOARD_URL` with an HTTPS URL. Production startup fails closed when these secrets are missing or too short, or when the Local Dashboard URL is not HTTPS. HTTP is accepted only for localhost in explicit development/test mode. Never place runtime secrets in source files, `.env.example`, Client Node configs, or build artifacts.

## Frontend demo behavior

- Organizations, branch configuration, branding, module selections, statuses, activity, invoices, payments, and build history persist in browser `localStorage`.
- Build jobs simulate queued, building, and succeeded states. Each new build is assigned to one branch and stores its organization, branch, application name, module snapshot, timestamp, status, progress, and a per-application version number scoped to that branch.
- The Application Builds landing page shows an organization list with a View action only. Viewing an organization opens a separate branch-history page with per-branch version counts, latest-build dates and statuses, status/search/sort filters, and one Create New Branch Build action.
- Creating a build from an organization's history opens directly to that organization and asks only for the target branch and application name. Organization-wide build targets are not available. Older unassigned build records are kept out of branch history until explicitly assigned to a branch; their version is advanced to the next branch version on assignment.
- Individual build-history records can be deleted after confirmation. Build history preserves prior versions if an organization or branch is later removed, unless an administrator explicitly deletes a build record.
- The download action produces a versioned demo JSON build manifest only; it is not an executable or deployable application package.
- Invoices support line items, tax, discount, due dates, status calculation, payment history, partial payments, and print / Save PDF through the browser print dialog.
- Invoice previews use the QIntellect logo, NTN `J756903`, and the configured UBL bank details. The invoice form shows live subtotal, discount, tax, and total calculations while line items are entered.
- Invoice company details include phone `+92 302 9633999`, email `info@qintellecttechnologies.com`, website `qintellecttechnologies.com`, and Main Branch, Rahim Yar Khan.
- The main dashboard and invoice branding use the supplied QIntellect logo and slogan: `DRIVING THE FUTURE TECHNOLOGY POWERED BY QUANTUM & AI`.
- The application opens with a QIntellect product-selection welcome screen. Selecting `QuantEye - AI Attendance System` opens this dashboard; `Restaurant Bot` is listed as a coming-soon product entry.
- The product portal includes detailed product capabilities, availability badges, responsive cards, and a loading transition while opening QuantEye.
- The selected product and active dashboard section are persisted in local storage, so browser refresh keeps the user on the same product and tab.
- Overview KPIs now use contextual supporting labels, and all dashboard data tables share the same bordered, responsive, striped-hover table system.
- The QuantEye sidebar includes a Back to Products control, and KPI values use centered, consistently aligned metric cards with unobtrusive icon placement.
- Dashboard icons use a locally served Font Awesome 6.4 asset bundle, so navigation, KPI, action, status, and notification icons render consistently without depending on a third-party CDN at runtime.
- All dashboard tables use the same professional system: rounded outer borders, gradient headers, clear row spacing, alternating rows, hover emphasis, aligned action controls, and horizontal overflow on smaller screens.
- Organization action controls are grouped in a consistent flex action cell so view, edit, activate/deactivate, build, payment, and download actions stay aligned across every table.
- The Organizations directory includes filter-aware organization, active, build-pending, and inactive/suspended counts; searches across profile and branch contact details; sorting; and a responsive all-records table with contact, module, branch, employee, status, and date summaries.
- The Organizations table displays the selected-module count as a number only. Opening organization details separates profile and contact fields to prevent long email, phone, or address values from overlapping. Branch counts link to branch details, including each branch's employee count.
- Organizations can be deleted from the directory after an in-app confirmation. Deletion removes only the organization profile; its demo invoices and build history remain available as historical records.
- Selecting an organization opens a dedicated Organization Details workspace rather than a modal. It shows organization status, separately labeled contact fields, branch names and effective access state, per-branch employee counts, branch keys linked to the organization key, branch invoices, enabled modules, and the generated frontend demo organization key.
- Branch staffing is a total employee count only; no individual employee profiles or rosters are stored. Each branch also has one point of contact with required name, phone, and role, plus optional email. Counts and contact details are entered while creating or editing an organization.
- New invoices must be assigned to one organization branch. Invoices retain the organization and branch names as historical snapshots, and legacy invoices without a branch assignment remain labeled as unassigned.
- Organization Details summarizes billed, recorded-paid, outstanding, and overdue invoice totals across the organization. Each branch shows its invoice count, outstanding balance, recorded payments, and individual invoice statuses; drafts and cancelled invoices are excluded from organization billing totals. Legacy invoices without a valid branch assignment are called out separately, remain in organization totals, and can be assigned to a branch from invoice details; only then do they affect branch billing and branch-access checks.
- The organization token section identifies the organization key ID and displays its validity/access status, including when expiry is not configured or the key is expiring soon. If the organization is inactive or its key is outside its validity period, the details page explicitly indicates that all branch access is blocked.
- Branch suspensions support the 2-day, 4-day, and one-week presets, custom start/end dates or any duration from 1 to 3650 days, and permanent suspension. Start date, end date, and duration are synchronized; suspensions remain in effect until manually reactivated. Reactivation requires an active organization, a currently valid organization key when validity dates are configured, and no outstanding non-draft invoice for that branch.
- Organization deactivation blocks effective access for every branch; branch keys inherit their parent organization's key ID and validity dates. Rotating the organization key synchronizes the new validity period to all branch keys.
- Organization token keys persist in local storage and can be copied or rotated from the detail workspace. They are placeholders for the future backend license/token validation service and do not provide security or a real 30-day license enforcement mechanism.
- Rotating a demo token lets the user set its start date, validity in days, and expiry date; the date and duration fields stay synchronized and are stored in localStorage and on every linked branch key. This is display/demo behavior only and does not enforce access expiry.
- Organization details use a denser five-metric summary, a dedicated token panel, branch access information, module cards, and organization contact information to avoid excessive empty space.
- The Organizations table includes a Total Branches column for every organization.
- Branch Access Controls now uses the same visual table language as the rest of the dashboard, with a gradient header row, aligned columns, consistent borders, hover treatment, and full-width action controls.
- Payments recorded in the frontend are explicitly unverified and do not provide real payment verification.
- Application Branding previews organization identity, enabled modules, branch access, grace periods, and the mandatory QIntellect footer.
- Branch suspension and reactivation are frontend demonstrations only. They are not security controls and must be enforced by the future backend.
- Use the browser's local storage controls to reset the demonstration data.
