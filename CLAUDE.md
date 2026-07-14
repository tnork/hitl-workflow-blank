# HITL Workflow — Dev Guide

## What This Is

A generic human-in-the-loop document review starter kit. LandingAI ADE parses PDFs into structured Markdown + bounding box grounding data; a Flask task board surfaces extracted fields for structured review, correction, and sign-off. **All domain-specific configuration lives in `schema.py`** — that is the only file a new developer needs to edit to adapt this to a new use case.

Currently running one pipeline: **docs**, carrying a single doc type — `shipping_label`. See [Pipelines](#pipelines) below.

## Theming (this is the unbranded build)

The UI ships neutral: white / grey / black, one typeface (Inter), and a generic
document mark. All colour lives in **one place per template** — the `:root` block at
the top of `templates/index.html` and `templates/login.html`. Nothing else in the CSS
hardcodes a brand colour.

To skin it for a customer:
1. Change the `--brand`, `--brand-dark`, `--brand-tint`, `--forest` and neutral values
   in both `:root` blocks.
2. Replace `static/logo-mark.png` (square, transparent PNG).
3. Change the wordmark text: `.app-wordmark` in `index.html`, `.wordmark` in
   `login.html`, and the `<title>` of each.

Status colours (`--success`, `--warning`, `--danger`) and the search-hit yellow carry
meaning, not branding — leave them unless you have a reason.

## Key Commands

```bash
python3 web_app.py --parse --all   # parse all PDFs in docs_inbox/ via LandingAI ADE
python3 extract_docs.py            # extract structured fields from parse results
python3 web_app.py                 # launch review UI on :8080
```

**Always use `python3`, not `python`.**

## Where to Start (New Developer)

1. **Read `schema.py`** — defines `DOC_TYPES`, `FIELD_LABELS`, `EXTRACTION_SCHEMAS`, `ENTITY_NAME_FIELDS`, `GROUP_PREFIX_PARTS`. This is the entire domain model.
2. **Understand the file naming convention** — stems follow `<group_key>_<doc_type>.pdf`. The group key determines which documents are reviewed together. Configured by `GROUP_PREFIX_PARTS`.
3. **Understand the data flow** — see the diagram below. `web_app.py` owns parsing; `extract_docs.py` owns field extraction; `web_app.py` also owns the Flask API and serves the UI.
4. **The UI is self-contained** — `templates/index.html` is a single file with all CSS and JS inline. The JS is structured as a module with clear state variables documented in comments at the top of the script block.

## Data Flow (parts pipeline)

```
docs_inbox/*.pdf                           — incoming PDFs (place here before parse)
  ↓  python3 web_app.py --parse --all
parse_results/docs/*.txt                   — ADE raw output (CHUNKS, GROUNDING, MARKDOWN)
  ↓  python3 extract_docs.py
extract_results/docs/<doc_type>/*.json     — extracted fields + metadata + grounding
  ↓  GET /api/docs, GET /api/doc/<stem>
Flask UI (templates/index.html)             — renders task board + review overlay
  ↓  POST /api/review/<stem> {verified:true}
Supabase reviews table (optional)           — persisted review decisions + field audit log
docs_outbox/                               — verified doc is moved here automatically
```

Documents are moved from `docs_inbox/` to `docs_outbox/` by the server when a review is saved with `verified: true` — and **only after the database write is confirmed**. If persistence fails the route returns 503 and the file stays in the inbox.

There is no auto-verification. A document whose fields are all green is shown as *finished*, and a reviewer must click **Verify** to sign it off. See [Verification](#verification-server-authoritative).

The `/pdf/<stem>` route and `/api/doc/<stem>` check both `docs_inbox/` and `docs_outbox/` so the viewer continues to work after a file is moved.

## Pipelines

The `PIPELINE` constant in `web_app.py` and `extract_docs.py` controls which pipeline is active. Both files must use the same value.

**Current pipeline: `docs`**
- Inbox: `docs_inbox/`
- Outbox: `docs_outbox/`
- Parse results: `parse_results/docs/*.txt`
- Extract results: `extract_results/docs/<doc_type>/*.json`
- Schema: `schema.py` (DOC_TYPES, FIELD_LABELS, EXTRACTION_SCHEMAS all define shipping_label)

**To add a new pipeline (e.g. `jobs`):**
1. Add `jobs_*` doc types, field labels, and extraction schemas to `schema.py`
2. Create `jobs_inbox/`, place PDFs there
3. Set `PIPELINE = "jobs"` in `web_app.py` and run `--parse --all`
4. Set `PIPELINE = "jobs"` in `extract_docs.py` and run it
5. Set `PIPELINE = "jobs"` back in `web_app.py` to serve the jobs review UI

Each pipeline uses independent subdirectories in `parse_results/` and `extract_results/`. Running two pipelines in parallel means running two server instances on different ports.

## File Map

| File | Purpose | Edit to… |
|---|---|---|
| `schema.py` | Domain config | Define your doc types, fields, ADE schemas |
| `core.py` | Shared config, paths, doc state, persistence | Change the pipeline; rarely otherwise |
| `extract_docs.py` | ADE Extract runner | Rarely — logic reads from schema.py |
| `web_app.py` | Flask app: parse + API + serve | Add routes, change parse behavior |
| `templates/index.html` | Review UI (all-in-one) | Change UI behavior or layout |
| `supabase_client.py` | Persistence layer | Add Supabase features |
| `app.py` | gunicorn WSGI entry | Not needed for local dev |
| `supabase/migrations/001_initial.sql` | DB schema | Extend with new tables |
| `supabase/migrations/002_transactional_review.sql` | Transactional review+audit fn | Rarely |
| `tests/` | pytest suite | Add a test for every new route |
| `static/vendor/` | Vendored marked + PDF.js | Re-vendor to upgrade |

## web_app.py Sections

| # | Name | Key entry point |
|---|---|---|
| 1 | ADE Batch Parsing | `run_parse()` |
| 2 | Flask Web App | `run_web()`, Flask routes |
| 3 | Entry Point | `main()` |

## API Routes

| Method | Route | Auth | Description |
|--------|-------|------|-------------|
| GET | `/health` | public | `{"status":"ok"}` |
| GET/POST | `/login` | public | Login page / form submit |
| POST | `/logout` | any | Clear session |
| GET | `/api/docs` | any | All docs with status |
| GET | `/api/doc/<stem>` | any | Full doc + `field_high_conf`, `threshold` |
| POST | `/api/review/<stem>` | any | Save review; on verify moves file to `{PIPELINE}_outbox/` |
| POST | `/api/reset` | admin | Clear all review decisions |
| GET | `/pdf/<stem>` | any | Serve raw file (checks `{PIPELINE}_inbox/` then `{PIPELINE}_outbox/`) |
| GET | `/api/settings` | any | `{"thresholds": {doc_type: float}}` |
| POST | `/api/settings` | admin | Update threshold for one doc type |
| GET | `/api/users` | admin | List users (no password_hash) |
| POST | `/api/users` | admin | Create user: `{username, password, role}` |
| DELETE | `/api/users/<uid>` | admin | Delete user (guards: self, last admin) |
| POST | `/api/users/<uid>/reset-password` | admin | Set password: `{password}` |
| POST | `/api/change-password` | any | Change own password: `{current_password, new_password}` |

## UI State Variables (`templates/index.html`)

Key JS module-level variables:
- `fieldApprovals` — `{fieldKey: bool}` — per-field approval for current doc
- `groundedFields` — `Set<fieldKey>` — fields with ADE grounding (pre-approved on render)
- `verifiedStems` — `Set<stem>` — stems verified this session
- `userInputs` — `{fieldKey: string | object[]}` — user overrides; scalar fields store a string, array fields store the full item array
- `reviewPrefix`, `reviewClientName`, `reviewReviewer` — current review session context

## Per-Field Approval Logic

Each field card has a circle checkmark button (`.rv-field-approve-btn`):
- **Light green** (`.grounded`) — ADE-grounded field; pre-approved automatically on render
- **Dark green** (`.approved`) — manually confirmed by clicking
- **Gray** — ungrounded or unfilled; requires manual click

`updateVerifyAffordance()` fires after every approval toggle. When all `fieldApprovals[key]` are true:
1. The document reads as **finished** and the sign-off panel (`#rv-verify-overlay`) becomes visible.
2. A confirmation dialog (`#rv-confirm-backdrop`) opens once: *"All N fields are approved. Verify it and move it to the outbox?"* — **Verify document** / **Not yet**.
3. **Not yet** dismisses the dialog and leaves the Verify button on screen, so the reviewer can sign off later.
4. On success the dashboard behind the overlay is refreshed (`initTaskBoard()`), so the Incomplete / In Review / Complete counters and the row's status pill update immediately.

It never saves on its own. `verifyPromptedStem` ensures the dialog asks once per document; un-approving a field re-arms it.

## Array Field Rendering (e.g. spare_parts list)

Array-typed extracted fields render as a collapsible card, auto-expanded on load:
- Click the "N items ▼" summary header to collapse/re-expand
- Each item has a numbered badge (`.rv-item-num`) above its fields grid
- All fields for each item render in a 2-column grid (`.rv-item-primary`) — no primary/detail split
- Field order follows `KEY_ORDER` in the render block, with any remaining keys appended
- Edits update `userInputs[key]` as a full array of objects — serialized as JSON in `field_overrides` when the review is saved
- CSS classes: `.rv-array-summary`, `.rv-array-body`, `.rv-array-item`, `.rv-item-header`, `.rv-item-num`, `.rv-item-primary`, `.rv-item-field`, `.rv-item-inp`

> Note: `KEY_ORDER` in the render block is hardcoded to the spare_parts schema fields. If you add a new array field type with different keys, update `KEY_ORDER` in `buildItems()` or make it dynamic.

## Document Search

A search field in the viewer toolbar (`#rv-search`). Type a term and press **Enter** to find it in the document.

- **Enter** — next match (re-running the same query steps forward rather than re-searching)
- **Shift+Enter** — previous match · **Escape** — clear · **Cmd/Ctrl+F** — focus the field
- An *n/m* counter, plus prev/next buttons
- Matches are highlighted in **yellow** (`--gold`), deliberately distinct from the colours used for grounding and approval, and scrolled into view

Backed by `chunk_index` in the `/api/doc/<stem>` payload — every parsed chunk with its text and grounding box. `ref_boxes` alone only covers chunks a *field* references, which is a fraction of the page.

> **Ceiling:** matches are chunk-level, so a hit highlights the whole chunk (a text block, a table row) rather than the exact word. The PDF renders to a `<canvas>` with no selectable text; word-level boxes would require a PDF.js text layer.

JS: `runSearch()`, `gotoMatch()`, `drawSearchMatches()` (called from `drawPageOverlay`), `resetSearch()`.

## Collapsible Sidebar

The document list sidebar (`#rv-sidebar`) collapses to width 0 by default:
- Toggle button (`#rv-sidebar-toggle`) inside the sidebar header collapses it
- Show button (`#rv-sidebar-show`) in the viewer toolbar expands it
- Collapse/expand uses CSS `transition: width 0.2s ease` with `overflow:hidden`
- When collapsed, `rv-sidebar-collapsed` class is applied; `#rv-sidebar-show` becomes visible
- JS: `initSidebarToggle()` IIFE at bottom of script block

## Resize Handle

A 5px drag handle (`#rv-resize-handle`) sits between the viewer and the fields panel:
- Drag left to widen the fields panel (clamped 240px–700px)
- CSS: `cursor:col-resize`; turns blue on hover/drag (`.rv-dragging`)
- JS: `initResizeHandle()` IIFE — sets `fieldsPanel.style.width` on `mousemove`; cleans up on `mouseup`
- Layout is `display:flex` on `#rv-app` so `flex:1` on `#rv-viewer` fills remaining space

## Magnifier Lens

A circular magnifying glass lens follows the cursor when hovering over the document viewer (`#rv-pdf-scroll`):
- 200px diameter, 1.6× zoom (shows ~125px of source per side — enough context to read table rows)
- Centered on the cursor; cursor is hidden (replaced by the lens center crosshair) while active
- Works for both canvas (PDF pages) and `<img>` elements (images)
- Source sampling does not clamp at document edges — lens shows white for areas beyond the document boundary
- HTML: `<div id="rv-magnifier">` + `<canvas id="rv-magnifier-canvas">` (fixed position, pointer-events:none)
- JS: `initMagnifier()` IIFE at bottom of script block, listens on `mousemove`/`mouseleave` of `pdfScroll`

## Authentication

All routes except `/health` and `/login` require a logged-in session.

- **Login**: `GET/POST /login` — form login; on success sets `session["user_id"]`, `session["username"]`, `session["role"]`
- **Logout**: `POST /logout` — clears session
- Users and passwords live in `users.json` (gitignored). On first run an `admin` account is created using `$ADMIN_PASSWORD`; if that is unset a random password is generated and printed **once**. There is no default credential.
- SECRET_KEY: read from env var `SECRET_KEY`, then `.secret_key` file, then auto-generated and saved to `.secret_key`
- Roles: `admin` (full access) or `user` (review only, can change own password)

Decorators inside `make_flask_app()`:
- `login_required` — redirects to `/login` for HTML routes, returns 401 JSON for `/api/` routes
- `admin_required` — implies login; returns 403 for non-admins

## Hamburger Menu (index.html)

`#app-menu-btn` in the header opens a left slide-in drawer (`#app-drawer`, `#app-drawer-overlay`):
- **Admins** see: Settings, Users
- **All users** see: Change Password
- JS: `openAppDrawer()`, `closeAppDrawer()`, `window.__APP_USER__.role` controls visibility of `#drawer-admin-items`

## Settings (Admin Only)

`GET /api/settings` → `{"thresholds": {"<doc_type>": float}}`
`POST /api/settings` → body `{doc_type, threshold}` — validates doc_type exists in schema, threshold ∈ [0,1]

The confidence threshold controls per-field auto-approval:
- `/api/doc/<stem>` response includes `field_high_conf: {field_key: bool}` — True if any ref box confidence ≥ threshold
- `renderFields` uses `data.field_high_conf[key]` (not local computation) to pre-approve fields
- Stored in `settings.json` (gitignored); defaults to 0.95 per doc type

UI: schema selector → threshold slider + number input → Save. Opens from hamburger → Settings.

## Users (Admin Only)

`GET /api/users` → list (no password_hash)
`POST /api/users` → `{username, password, role}` → creates user, 409 if username taken
`DELETE /api/users/<uid>` → guards: can't delete yourself, can't delete last admin
`POST /api/users/<uid>/reset-password` → `{password}` — min length from `core.PASSWORD_MIN_LEN` (12); bumps `session_version`, revoking that user's live sessions

Any user: `POST /api/change-password` → `{current_password, new_password}` — same policy; revokes the user's *other* sessions

User storage format (users.json): `{uid: {username, password_hash, role}}`

## Verification (server-authoritative)

`POST /api/review/<stem>` does **not** trust the client.

- `verified_by` comes from `session["username"]`; `verified_at` from the server clock. Both are ignored if sent by the client.
- `field_overrides` keys are whitelisted against `schema.FIELD_LABELS[doc_type]`. Unknown keys → 400.
- To set `verified: true`, the client also sends `approvals: {field_key: bool}`. The server independently recomputes, for every schema field, whether it is *auto-approvable* (ADE-grounded at or above the doc type's threshold, or overridden by the reviewer). Any field that is neither auto-approvable nor explicitly approved → **409** with a `missing` list. A forged payload cannot verify a document whose fields were never reviewed.
- The source file moves to the outbox only after the persistence write is confirmed.

`core.compute_doc_state()` is the single implementation of this. `/api/docs`, `/api/doc/<stem>`, and `/api/review/<stem>` all call it, so the task board, the review panel, and verification can never disagree about readiness.

## core.py

Shared configuration and logic, imported by both `web_app.py` and `extract_docs.py`:

| Concern | Function |
|---|---|
| Pipeline + paths | `PIPELINE`, `DOCS_DIR`, `EXTRACT_RESULTS`, … (set `PIPELINE` here, or via env — no longer duplicated in two files) |
| Startup validation | `validate_config()` — fails fast if `schema.py` is inconsistent |
| Document state | `compute_doc_state(stem, overrides)` — fields, grounding boxes, `field_high_conf`, `auto_ok`, counts |
| Atomic persistence | `atomic_write_json()`, `load_users()`, `save_users()`, `load_settings()`, `save_settings()` |
| Password policy | `PASSWORD_MIN_LEN`, `validate_password()` — one rule, used by every route and surfaced to the UI via `GET /api/password-policy` |

Users and settings are written via a lock + `os.replace`, so concurrent requests cannot interleave a read-modify-write, and an interrupted write cannot truncate the file. Corrupt JSON is quarantined to `<name>.corrupt.<hex>` rather than silently becoming `{}`.

**Ceiling:** the file lock, the login throttle, and the in-memory review fallback are all per-process. Keep gunicorn at `--workers 1`, or move users/settings/reviews into Postgres.

## Security

| Control | Where |
|---|---|
| CSRF | `csrf_protect()` before-request — Origin/Referer check on every unsafe method, plus a double-submit token (`X-CSRF-Token` header, or `csrf_token` form field). Login is protected by the Origin check alone (no session yet). |
| Session revalidation | `_current_user()` reloads the user on every protected request and compares `session_version`. Deleting a user, resetting their password, or changing their role revokes live cookies immediately. Sessions also expire (`SESSION_LIFETIME_HOURS`, default 12). |
| Login throttling | Exponential backoff per (IP, username) after 5 failures, capped at 15 min. |
| CSP | Set in `set_security_headers()`. `marked` and PDF.js are vendored to `static/vendor/`, so no third-party origin may execute script. `'unsafe-inline'` is still needed for the inline `<script>`/`<style>` blocks in the templates — removing it means extracting them to files. |
| Initial admin | `$ADMIN_PASSWORD`, else a random generated password printed once. Never `admin/admin`. |

## Tests

```bash
pip install -r requirements.txt -r requirements-dev.txt
pytest -q
```

`tests/` runs against a temp directory tree — it never touches the real `users.json`, `settings.json`, or inbox. Covers verification forgery, persistence-failure rollback, file movement, reset restoring the inbox, CSRF, session revocation, password policy, board/panel agreement, atomic writes, and corrupt-file quarantine.

## Supabase migrations

Run `001_initial.sql` then `002_transactional_review.sql`. The latter adds `save_review_with_audit()`, which writes the review row and its audit entries in **one transaction** — previously a failed audit insert left the review committed with no trail. `supabase_client.save_review()` calls it via RPC and returns `False` on any failure, which the route turns into a 503.

## Environment

```
VISION_AGENT_API_KEY       — LandingAI ADE (parsing + extraction)
SUPABASE_URL               — Supabase project URL (optional)
SUPABASE_SERVICE_ROLE_KEY  — Supabase service role key (optional)
SECRET_KEY                 — Flask session secret (auto-generated to .secret_key if not set)
ADMIN_PASSWORD             — password for the initial admin account (else randomly generated + printed once)
SESSION_LIFETIME_HOURS     — session expiry, default 12
SESSION_COOKIE_SECURE      — set to 1 when serving over HTTPS
PIPELINE                   — override the active pipeline (default "docs")
LOG_LEVEL                  — default INFO
```

## Supabase Persistence (optional)

When `SUPABASE_URL` + `SUPABASE_SERVICE_ROLE_KEY` are set:
- Reviews → `reviews` table (upserted per doc)
- Field changes → `field_audit_log` (append-only, one row per changed field)
- PDFs → `docs` Storage bucket (lazy-uploaded on first serve)
- Extract JSONs → `extract-results` Storage bucket (lazy-uploaded on first access)

Run `supabase/migrations/001_initial.sql` to create the schema. Falls back to in-memory `_reviews` dict without env vars — no code changes needed.

## Persistence Requirements (for production)

Out of the box the app runs entirely without a database:
- **Users** → `users.json` (local file, gitignored, PBKDF2-hashed passwords via werkzeug)
- **Settings** → `settings.json` (local file, gitignored)
- **Review decisions** → in-memory `_reviews` dict (lost on restart)

For cloud / stateless deployments you need:
1. **Supabase** (or Postgres equivalent) for review decisions and field audit log — see [Supabase Persistence](#supabase-persistence-optional) above
2. **User storage in DB** — `users.json` is ephemeral on containers; rewrite the user CRUD in `web_app.py` to a `users` DB table
3. **Per-user audit trail** — `field_audit_log` exists but doesn't capture `verified_by` per field; pass `session["username"]` through the audit writes

## Dependency Notes

- All dependencies are unpinned — pin them in your own fork once stable
- Flask server runs on port **8080** locally; gunicorn binds to **7860** for HF Spaces (Dockerfile)
- Health check: `GET /health` → `{"status":"ok"}`
