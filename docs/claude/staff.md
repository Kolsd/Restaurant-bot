# Staff HQ, admin dashboard, staff endpoints, POS

> Moved verbatim from CLAUDE.md (2026-09-12) so it isn't loaded on every turn.

## Staff HQ Module (`/staff-hq`)

Unified operational portal for all non-admin staff. Replaces the separate per-role pages.

- **Unified login**: `login.html` with 3 views: login form, restaurant selector, role selector. `?r=X` for staff PIN login. `staff-portal.html` was removed (server-side redirect).
- **Usernames**: staff use `firstname.lastname` (auto-generated at creation). Duplicates resolved with a numeric suffix. Login accepts username or full name.
- **Auth token**: JWT with claim `staff:<uuid>`. Stored in `localStorage` as `rb_staff_token` and also aliased as `rb_token`. Sessions via a SHA-256 hash (`sessions_repo`).
- **Sections**: Clock card (clock-in/out/break), weekly timecard with deduction badges, Biometrics (register/manage FIDO2 credentials).

### WebAuthn Biometrics (`staff_webauthn.py`)
- Registration: requires a staff Bearer token → `POST /api/staff/webauthn/register-options` + `register-complete`.
- Biometric clock-in/out (public kiosk): `POST /api/staff/webauthn/auth-options` + `auth-complete`.
- `auth-complete` accepts `action: clock_in | clock_out | break` — includes break-toggle logic.
- `RP_ID` is read from the `APP_DOMAIN` env var or from the request hostname.

## Admin Dashboard (`/dashboard`)

### Main navigation
| Section | Nav key | Loader |
|---------|---------|--------|
| Team | `staff` | `loadStaffSection()` |
| Payroll and Tips | `payroll` | `loadPayrollSection()` |
| Menu | `menu` | — |
| Stats | `stats` | — |
| ... | ... | ... |

### Team section — sub-tabs
- **Team**: roster with search, role filters, active/on-shift status cards.
- **Shifts**: visual weekly editor `_renderShiftsEditor`. Click a cell → create/edit modal. Multi-select → bulk modal. "Copy previous week" button → `POST /api/staff/schedules/bulk`. Compliance badges: ✓ / ⚠ / ✗.

### Payroll section — sub-tabs
- **Payroll**: period + presets → `GET /api/staff/payroll/calculate`. Per-employee table. Per-role tip % config (`PATCH /api/staff/tip-distribution`). Automatic tips card (`GET /api/staff/tips/auto`). Save draft / approve run.
- **Overtime**: pending list with Approve/Reject (`PATCH /api/staff/payroll/overtime/{id}`).
- **Contracts**: template CRUD. Monetary fields as `Decimal` in Pydantic.

## Staff Endpoints (`/api/staff/...`)

```
# Roster
GET    /api/staff
POST   /api/staff
PATCH  /api/staff/{id}
DELETE /api/staff/{id}

# Self (Bearer token staff:<uuid>)
GET    /api/staff/self/profile
POST   /api/staff/self/clock-in
POST   /api/staff/self/clock-out
POST   /api/staff/self/break-start
POST   /api/staff/self/break-end
GET    /api/staff/self/timecard          → ?week_start=YYYY-MM-DD

# Shifts and schedules
GET    /api/staff/open-shifts
GET    /api/staff/shifts                 → ?date_from=&date_to=
POST   /api/staff/clock-in              → admin (body: staff_id)
POST   /api/staff/clock-out             → admin (body: staff_id)
GET    /api/staff/schedules
POST   /api/staff/schedules
POST   /api/staff/schedules/bulk        → body: {entries: [{staff_id, day_of_week, start_time, end_time}]}
DELETE /api/staff/schedules/{id}

# Tips
GET    /api/staff/tips/auto             → ?period_start=&period_end=&branch_id=
PATCH  /api/staff/tip-distribution      → body: {config: {role: pct}}
GET    /api/staff/tip-distributions     → history (legacy)

# Manual deductions
GET    /api/staff/{id}/deductions
POST   /api/staff/{id}/deductions
PATCH  /api/staff/deductions/{item_id}
DELETE /api/staff/deductions/{item_id}

# Payroll
GET    /api/staff/payroll/calculate     → ?period_start=&period_end=
POST   /api/staff/payroll/runs          → body: {period_start, period_end, snapshot, ...}
GET    /api/staff/payroll/runs
PUT    /api/staff/payroll/runs/{id}/approve  → marks draft → approved (No-v2 sprint)
GET    /api/staff/payroll/runs/{id}/export   → CSV download
GET    /api/staff/payroll/overtime      → ?week_start=&status=
PATCH  /api/staff/payroll/overtime/{id} → body: {status: approved|rejected, notes}
GET    /api/staff/payroll/contracts
POST   /api/staff/payroll/contracts
PATCH  /api/staff/payroll/contracts/{id}
DELETE /api/staff/payroll/contracts/{id}
PATCH  /api/staff/{id}/contract         → body: {template_id, overrides, contract_start}

# Biometric WebAuthn
POST   /api/staff/webauthn/register-options
POST   /api/staff/webauthn/register-complete
POST   /api/staff/webauthn/auth-options    → body: {restaurant_id, action}
POST   /api/staff/webauthn/auth-complete   → body: {action, credential_id, ...}
GET    /api/staff/webauthn/credentials
DELETE /api/staff/webauthn/credentials/{id}
```

## Staff, POS and Operations

- **Valid roles**: `owner`, `admin`, `gerente` (manager), `mesero` (waiter), `caja` (cashier), `cocina` (kitchen), `bar`, `domiciliario` (courier), `otro` (other).
- **Cashier (Super Caja)**: 3 views: Tables (local POS), Pending Deliveries, Chats (verify proof of payment).
- **Split Checks**: `table_checks` allows mixed payments. All math in `Decimal`. A table is fully closed → `factura_entregada` (bill delivered) once every check is `invoiced`/`cancelled`.
- **Tips on checks**: `table_checks.tip_amount` validated: `tip_amount <= money_mul(check_total, Decimal("0.5"))`.
- **Shifts**: a partial unique index guarantees 1 open row per staff member.
- **Overtime**: compares `billable_minutes` against `contract_templates.weekly_hours`. Status `pending` for approval.
