"""
HITL Document Review Workflow
==============================
ADE parsing + Flask web UI for human-in-the-loop review of any document type.
Configure document types, extraction schemas, and field labels in schema.py.

Modes:
    python3 web_app.py                  # launch web UI (default)
    python3 web_app.py --parse          # parse 2 pending docs via LandingAI ADE
    python3 web_app.py --parse --all    # parse all pending docs
    python3 web_app.py --parse --limit 5  # parse next N docs
    python3 web_app.py --parse --force  # force re-parse (delete existing results)

Input:  docs_inbox/{stem}.pdf  (or .png/.jpg)
Parse:  parse_results/docs/{stem}.txt
On verify: file is moved to docs_outbox/

Environment:
    VISION_AGENT_API_KEY       — for ADE parsing only (.env)
    SUPABASE_URL               — Supabase project URL (optional; enables persistence)
    SUPABASE_SERVICE_ROLE_KEY  — Supabase service role key (optional)
"""

import json
import logging
import os
import re
import secrets
import shutil
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path

from werkzeug.security import generate_password_hash, check_password_hash
from dotenv import load_dotenv
import supabase_client as sb
import schema
import core
from core import (
    BASE_DIR, PIPELINE, DOCS_DIR, DOCS_OUTBOX_DIR, PARSE_RESULTS, EXTRACT_RESULTS,
    SETTINGS_FILE, USERS_FILE, SECRET_KEY_FILE, SEP, FILE_EXTS as _FILE_EXTS,
    PASSWORD_MIN_LEN, validate_password,
)

load_dotenv(override=True)

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)-7s %(name)s — %(message)s",
)
log = logging.getLogger("hitl.web")

# Fail fast on a malformed schema.py rather than 500ing at request time.
core.validate_config()

# Field labels and doc type labels come from schema.py — edit that file to
# configure your document types. These are aliased here for internal use.
_REVIEW_FIELD_LABELS    = schema.FIELD_LABELS
_REVIEW_DOC_TYPE_LABELS = schema.DOC_TYPES

# Parse-result readers now live in core.py (single implementation, shared with
# extract_docs.py). Thin aliases retained for readability at call sites.
_review_get_section_json = core.get_section_json
_review_load_parse       = core.load_parse

# ══════════════════════════════════════════════════════════════════════════════
# SECTION 1 — ADE BATCH PARSING
# ══════════════════════════════════════════════════════════════════════════════

def _build_parse_text(parse_response, pdf_path: Path) -> str:
    full = parse_response.model_dump()
    return "\n".join([
        "SOURCE",   SEP, str(pdf_path), SEP, "",
        "METADATA", SEP, json.dumps(full["metadata"], indent=2, default=str), SEP, "",
        "CHUNKS",   SEP, json.dumps(full.get("chunks", []), indent=2, default=str), SEP, "",
        "GROUNDING", SEP, json.dumps(full.get("grounding", {}), indent=2, default=str), SEP, "",
        "SPLITS",   SEP, json.dumps(full.get("splits", []), indent=2, default=str), SEP, "",
        "MARKDOWN", SEP, parse_response.markdown or "", SEP,
    ])


MAX_PAGES = 90


def _pdf_page_count(pdf_path: Path) -> int:
    import fitz
    doc = fitz.open(pdf_path)
    n   = len(doc)
    doc.close()
    return n


def _split_pdf(pdf_path: Path, chunk_size: int = MAX_PAGES) -> list:
    import fitz, tempfile
    doc   = fitz.open(pdf_path)
    parts = []
    for start in range(0, len(doc), chunk_size):
        end    = min(start + chunk_size, len(doc))
        # Use NamedTemporaryFile to avoid mktemp() TOCTOU race condition
        import os as _os
        fd, tmp_str = tempfile.mkstemp(suffix=f"_p{start}-{end-1}.pdf")
        _os.close(fd)
        tmp    = Path(tmp_str)
        subdoc = fitz.open()
        subdoc.insert_pdf(doc, from_page=start, to_page=end - 1)
        subdoc.save(tmp)
        subdoc.close()
        parts.append((start, tmp))
    doc.close()
    return parts


def _parse_one(client, category: str, pdf_path: Path) -> bool:
    stem        = pdf_path.stem
    result_dir  = PARSE_RESULTS / category
    result_file = result_dir / f"{stem}.txt"

    result_dir.mkdir(parents=True, exist_ok=True)

    if result_file.exists():
        print(f"  [skip] {category}/{stem}")
        return True

    page_count = _pdf_page_count(pdf_path)

    if page_count > MAX_PAGES:
        print(f"  Parsing (split): {category}/{pdf_path.name} ({page_count} pages) ...", flush=True)
        return _parse_split(client, category, pdf_path, page_count, result_file)

    print(f"  Parsing: {category}/{pdf_path.name} ...", flush=True)
    try:
        parse_response = client.parse(document=pdf_path, model="dpt-2-latest")
    except Exception as e:
        print(f"  [error] {pdf_path.name}: {e}")
        return False

    pages  = getattr(getattr(parse_response, "metadata", None), "page_count", "?")
    chunks = parse_response.model_dump().get("chunks", [])
    result_file.write_text(_build_parse_text(parse_response, pdf_path), encoding="utf-8")
    print(f"  ✓ {pages} pages | {len(chunks)} chunks")
    return True


def _parse_split(client, category: str, pdf_path: Path, total_pages: int,
                 result_file: Path) -> bool:
    parts         = _split_pdf(pdf_path)
    all_chunks    = []
    all_grounding = {}
    all_markdown  = []

    try:
        for part_idx, (page_offset, tmp_path) in enumerate(parts):
            part_pages = _pdf_page_count(tmp_path)
            print(f"    Part {part_idx+1}/{len(parts)}: pages {page_offset}–{page_offset+part_pages-1}", flush=True)
            try:
                resp = client.parse(document=tmp_path, model="dpt-2-latest")
            except Exception as e:
                print(f"    [error] part {part_idx+1}: {e}")
                tmp_path.unlink(missing_ok=True)
                continue

            part_data = resp.model_dump()
            for chunk in part_data.get("chunks", []):
                grounding = chunk.get("grounding")
                if grounding and "page" in grounding:
                    grounding["page"] += page_offset
                all_chunks.append(chunk)

            for uid, gdata in (part_data.get("grounding") or {}).items():
                entry = dict(gdata) if gdata else {}
                if "page" in entry and entry["page"] is not None:
                    entry["page"] = entry["page"] + page_offset
                all_grounding[uid] = entry

            if resp.markdown:
                all_markdown.append(resp.markdown)
            tmp_path.unlink(missing_ok=True)
            time.sleep(1)

    finally:
        for _, tmp_path in parts:
            tmp_path.unlink(missing_ok=True)

    merged = {
        "metadata":  {"page_count": total_pages, "source": str(pdf_path), "split": True},
        "chunks":    all_chunks,
        "grounding": all_grounding,
        "splits":    [],
    }

    class _FakeResp:
        def model_dump(self): return merged
        markdown = "\n\n---\n\n".join(all_markdown)

    result_file.write_text(_build_parse_text(_FakeResp(), pdf_path), encoding="utf-8")
    print(f"  ✓ {total_pages} pages (split) | {len(all_chunks)} chunks total")
    return True


def run_parse(parse_all: bool = False, limit: int = 2, force: bool = False):
    """Parse PDFs from <pipeline>_inbox/ through LandingAI ADE.

    Results land in  parse_results/<pipeline>/<stem>.txt
    Place your PDF files in the {PIPELINE}_inbox/ directory before running.
    """
    from landingai_ade import LandingAIADE

    CATEGORY = PIPELINE

    if not DOCS_DIR.is_dir():
        print(f"{PIPELINE}_inbox/ directory not found. Create it and add your PDFs.")
        sys.exit(1)

    _SUPPORTED = {".pdf", ".png", ".jpg", ".jpeg"}
    pdfs = sorted(p for p in DOCS_DIR.iterdir() if p.suffix.lower() in _SUPPORTED)
    if not pdfs:
        print(f"No documents found in {DOCS_DIR}. Supported: PDF, PNG, JPG.")
        sys.exit(1)

    if force:
        import shutil
        deleted = 0
        for p in pdfs:
            r = PARSE_RESULTS / CATEGORY / f"{p.stem}.txt"
            if r.exists():
                r.unlink()
                deleted += 1
        if deleted:
            print(f"  [force] Deleted {deleted} existing parse result(s) — will re-parse")

    pending = [p for p in pdfs if not (PARSE_RESULTS / CATEGORY / f"{p.stem}.txt").exists()]
    batch   = pdfs if parse_all else pending[:limit]
    label   = f"all {len(batch)}" if parse_all else f"{len(batch)} of {len(pending)} pending"

    print("=" * 60)
    print(f"Document Parser — {label} file(s)")
    print("=" * 60)

    (PARSE_RESULTS / CATEGORY).mkdir(parents=True, exist_ok=True)
    client = LandingAIADE()

    success = fail = skipped = 0
    for i, pdf_path in enumerate(batch, 1):
        print(f"\n[{i}/{len(batch)}]")
        if (PARSE_RESULTS / CATEGORY / f"{pdf_path.stem}.txt").exists():
            skipped += 1
            print(f"  [skip] already parsed: {pdf_path.name}")
            continue
        ok = _parse_one(client, CATEGORY, pdf_path)
        if ok:
            success += 1
        else:
            fail += 1
        if i < len(batch):
            time.sleep(1)

    print("\n" + "=" * 60)
    print(f"DONE  parsed={success}  skipped={skipped}  failed={fail}")
    print(f"Results: {PARSE_RESULTS / CATEGORY}/")
    print("=" * 60)


# Settings and users are persisted via core.py, which serializes concurrent
# read-modify-write cycles behind a lock and writes atomically (os.replace), so
# an interrupted write can no longer truncate users.json or settings.json.
_load_settings     = core.load_settings
_save_settings_file = core.save_settings
_get_threshold     = core.get_threshold
_load_users        = core.load_users
_save_users        = core.save_users


# ─── Login throttling (item #6) ───────────────────────────────────────────────
# In-memory exponential backoff per (ip, username). Resets on success.
#
# Ceiling: per-process and non-persistent — it does not survive a restart and
# does not coordinate across gunicorn workers. Adequate for a single-worker
# deployment; upgrade path is a shared store (Redis / Postgres) keyed the same way.

_LOGIN_MAX_ATTEMPTS = 5
_LOGIN_LOCKOUT_BASE = 30       # seconds; doubles per failure past the threshold
_LOGIN_LOCKOUT_MAX  = 15 * 60

_login_failures: dict = {}     # key -> {"count": int, "until": datetime|None}
_login_lock = threading.Lock()

# X-Forwarded-For is attacker-controlled unless a trusted proxy sets it. Honouring
# it unconditionally hands an attacker a fresh throttle bucket per request — they
# just rotate the header and the lockout never fires. Only trust it when the
# operator declares they are behind a proxy.
_TRUST_PROXY = os.environ.get("TRUST_PROXY_HEADERS", "").lower() in ("1", "true", "yes")


def _client_ip() -> str:
    from flask import request
    if _TRUST_PROXY:
        fwd = request.headers.get("X-Forwarded-For", "")
        if fwd:
            return fwd.split(",")[0].strip()
    return request.remote_addr or "?"


def _login_keys(username: str) -> list[str]:
    """Throttle on BOTH (ip, username) and username alone.

    The per-IP bucket stops one host brute-forcing an account. The username-only
    bucket still protects that account when the attacker rotates source IPs
    (botnet, or a spoofable XFF behind a misconfigured proxy).
    """
    u = username.lower()
    return [f"ip:{_client_ip()}|{u}", f"user:{u}"]


def _login_locked_for(keys: list[str]) -> int:
    """Seconds remaining in lockout across any bucket, or 0 if none are locked."""
    worst = 0
    with _login_lock:
        now = datetime.now(timezone.utc)
        for key in keys:
            rec = _login_failures.get(key)
            if not rec or not rec.get("until"):
                continue
            remaining = (rec["until"] - now).total_seconds()
            if remaining <= 0:
                rec["until"] = None
                continue
            worst = max(worst, int(remaining) + 1)
    return worst


def _login_record_failure(keys: list[str]) -> None:
    with _login_lock:
        for key in keys:
            rec = _login_failures.setdefault(key, {"count": 0, "until": None})
            rec["count"] += 1
            if rec["count"] >= _LOGIN_MAX_ATTEMPTS:
                over = rec["count"] - _LOGIN_MAX_ATTEMPTS
                delay = min(_LOGIN_LOCKOUT_BASE * (2 ** over), _LOGIN_LOCKOUT_MAX)
                rec["until"] = datetime.now(timezone.utc) + timedelta(seconds=delay)


def _login_record_success(keys: list[str]) -> None:
    with _login_lock:
        for key in keys:
            _login_failures.pop(key, None)


# ─── In-memory review store — fallback when Supabase is not configured ────────
# When Supabase is enabled this is a write-through cache only; the DB is the
# source of truth. Module-level so it survives app re-creation and can be
# inspected by tests.
#
# Ceiling: per-process and lost on restart. Configure Supabase for durability.
_reviews: dict = {}


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 2 — FLASK WEB APP
# ══════════════════════════════════════════════════════════════════════════════

def make_flask_app():
    from flask import (Flask, render_template, send_file, make_response,
                       session, request, jsonify, redirect, url_for, abort)

    flask_app = Flask(__name__, template_folder=str(BASE_DIR / "templates"))

    # Secret key for sessions
    _sk = os.environ.get("SECRET_KEY", "")
    if not _sk:
        if SECRET_KEY_FILE.exists():
            _sk = SECRET_KEY_FILE.read_text().strip()
        if not _sk:
            import secrets as _secrets
            _sk = _secrets.token_hex(32)
            SECRET_KEY_FILE.write_text(_sk)
            print(f"[AUTH] Generated new SECRET_KEY → {SECRET_KEY_FILE}")
    flask_app.secret_key = _sk
    flask_app.config["SESSION_COOKIE_HTTPONLY"] = True
    flask_app.config["SESSION_COOKIE_SAMESITE"] = "Strict"
    # Sessions expire rather than living forever (item #5).
    flask_app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(
        hours=int(os.environ.get("SESSION_LIFETIME_HOURS", "12"))
    )
    # Enable Secure flag only when running behind HTTPS (set SESSION_COOKIE_SECURE=1 in prod)
    if os.environ.get("SESSION_COOKIE_SECURE", "").lower() in ("1", "true", "yes"):
        flask_app.config["SESSION_COOKIE_SECURE"] = True

    # Content-Security-Policy (item #10). marked and PDF.js are now vendored under
    # static/vendor/, so no third-party origin may execute script here at all.
    # Google Fonts remains the only external origin (stylesheet + font files).
    #
    # 'unsafe-inline' is still required for script/style because the templates
    # carry large inline <script>/<style> blocks; removing it means extracting
    # ~1500 lines of inline JS to static files, which is a separate refactor.
    _CSP = "; ".join([
        "default-src 'self'",
        "script-src 'self' 'unsafe-inline' blob:",
        "worker-src 'self' blob:",
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
        "font-src 'self' data: https://fonts.gstatic.com",
        "img-src 'self' data: blob:",
        "connect-src 'self' blob:",
        "object-src 'none'",
        "base-uri 'none'",
        "form-action 'self'",
        "frame-ancestors 'none'",
    ])

    @flask_app.after_request
    def set_security_headers(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault("Content-Security-Policy", _CSP)
        if request.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    # ── CSRF (item #9) ────────────────────────────────────────────────────────
    # Double-submit token + strict Origin/Referer check on every unsafe method.
    # SameSite=Strict remains as defence in depth.

    _SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}

    def _csrf_token() -> str:
        tok = session.get("csrf_token")
        if not tok:
            tok = secrets.token_urlsafe(32)
            session["csrf_token"] = tok
        return tok

    def _same_origin() -> bool:
        origin = request.headers.get("Origin")
        if origin:
            return origin.rstrip("/") == request.host_url.rstrip("/")
        # Some browsers omit Origin on same-origin form POSTs; fall back to Referer.
        referer = request.headers.get("Referer")
        if referer:
            return referer.startswith(request.host_url)
        return False   # fail closed

    @flask_app.before_request
    def csrf_protect():
        if request.method in _SAFE_METHODS or request.path == "/health":
            return None
        if not _same_origin():
            log.warning("CSRF: cross-origin %s %s rejected", request.method, request.path)
            return jsonify({"error": "Cross-origin request rejected"}), 403
        # The login form has no session yet, so it is protected by the Origin
        # check alone; every other unsafe route requires the token.
        if request.path == "/login":
            return None
        expected = session.get("csrf_token")
        sent = request.headers.get("X-CSRF-Token") or (request.form.get("csrf_token") or "")
        if not expected or not sent or not secrets.compare_digest(str(expected), str(sent)):
            log.warning("CSRF: bad token on %s %s", request.method, request.path)
            return jsonify({"error": "Invalid CSRF token"}), 403
        return None

    @flask_app.context_processor
    def inject_csrf():
        return {"csrf_token": _csrf_token()}

    # ── Session revalidation (item #5) ────────────────────────────────────────
    # Never trust role/username from the cookie. Reload the user on every
    # protected request and compare session_version, so deleting a user, changing
    # their role, or resetting their password immediately invalidates live cookies.

    def _current_user():
        uid = session.get("user_id")
        if not uid:
            return None
        user = _load_users().get(uid)
        if not user:
            return None
        if int(session.get("session_version", 0)) != int(user.get("session_version", 1)):
            return None
        return user

    def _reject(html_redirect=True):
        session.clear()
        if request.path.startswith("/api/"):
            return jsonify({"error": "Unauthorized"}), 401
        return redirect(url_for("login", error="session_expired")) if html_redirect else (
            jsonify({"error": "Unauthorized"}), 401
        )

    def login_required(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            user = _current_user()
            if user is None:
                return _reject()
            # Refresh derived values from the DB of record, not the cookie.
            session["username"] = user["username"]
            session["role"]     = user["role"]
            return f(*args, **kwargs)
        return decorated

    def admin_required(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            user = _current_user()
            if user is None:
                return _reject()
            session["username"] = user["username"]
            session["role"]     = user["role"]
            if user.get("role") != "admin":
                if request.path.startswith("/api/"):
                    return jsonify({"error": "Forbidden"}), 403
                abort(403)
            return f(*args, **kwargs)
        return decorated

    @flask_app.route("/")
    @login_required
    def index():
        doc_types = {k: v for k, v in schema.DOC_TYPES.items()}
        r = make_response(render_template(
            "index.html",
            username=session["username"],
            user_role=session["role"],
            doc_types=doc_types,
        ))
        r.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        return r

    @flask_app.route("/health")
    def health():
        return {"status": "ok"}, 200

    _LOGIN_ERRORS = {
        "invalid": "Invalid username or password.",
        "session_expired": "Your session expired. Please sign in again.",
        "locked": "Too many failed attempts. Try again shortly.",
    }

    @flask_app.route("/login", methods=["GET"])
    def login():
        if "user_id" in session:
            return redirect(url_for("index"))
        error_key = request.args.get("error", "")
        error = _LOGIN_ERRORS.get(error_key, "")
        return render_template("login.html", error=error)

    @flask_app.route("/login", methods=["POST"])
    def login_post():
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""

        keys = _login_keys(username)
        locked = _login_locked_for(keys)
        if locked:
            # Never reveal whether the account exists — same message either way.
            log.warning("Login blocked (throttled) for %s — %ss remaining",
                        keys[0], locked)
            return redirect(url_for("login", error="locked"))

        users = _load_users()
        matched_uid = None
        matched_user = None
        for uid, u in users.items():
            if u["username"] == username:
                matched_uid = uid
                matched_user = u
                break
        # Always run check_password_hash to prevent timing-based username enumeration
        _dummy_hash = generate_password_hash("__dummy__")
        target_hash = matched_user["password_hash"] if matched_user else _dummy_hash
        if matched_user and check_password_hash(target_hash, password):
            _login_record_success(keys)
            session.clear()
            session.permanent = True
            session["user_id"]        = matched_uid
            session["username"]       = matched_user["username"]
            session["role"]           = matched_user["role"]
            session["session_version"] = int(matched_user.get("session_version", 1))
            log.info("Login OK: user=%s role=%s", matched_user["username"], matched_user["role"])
            return redirect(url_for("index"))

        _login_record_failure(keys)
        log.warning("Login FAILED for username=%r from %s", username, _client_ip())
        return redirect(url_for("login", error="invalid"))

    @flask_app.route("/logout", methods=["POST"])
    def logout():
        session.clear()
        return redirect(url_for("login"))

    @flask_app.route("/readme")
    @login_required
    def readme():
        content = (BASE_DIR / "README.md").read_text(encoding="utf-8")
        if content.startswith("---"):
            end = content.find("---", 3)
            if end != -1:
                content = content[end + 3:].lstrip("\n")
        return content, 200, {"Content-Type": "text/plain; charset=utf-8"}

    def _get_review(stem: str) -> dict:
        """Return review state for stem from Supabase (preferred) or _reviews."""
        if sb.is_enabled():
            return sb.get_review(stem)
        return _reviews.get(stem) or {}

    def _get_reviews_bulk(stems: list) -> dict:
        """Bulk-fetch {stem: review_dict} from Supabase or _reviews."""
        if sb.is_enabled():
            return sb.get_reviews_bulk(stems)
        return {s: _reviews.get(s) or {} for s in stems}

    def _save_review(stem: str, review: dict, old_review: dict, actor: str) -> bool:
        """Persist a review. Returns True only if the write is durable.

        Previously this ignored sb.save_review()'s return value, so a failed
        database write still reported success and the caller still moved the
        source document to the outbox — losing the review with no trace.

        `actor` is the logged-in user making THIS change. It is distinct from
        `verified_by`, which is only set when a document is signed off. The audit
        log used to record the reviewer from `verified_by`, so a field edit saved
        without verifying was attributed to nobody (or worse, to whoever verified
        the document last time). Every audit row now names the user who caused it.
        """
        if sb.is_enabled():
            ok = sb.save_review(
                stem            = stem,
                verified        = review.get("verified", False),
                verified_at     = review.get("verified_at", ""),
                verified_by     = review.get("verified_by", ""),
                actor           = actor,
                field_overrides = review.get("field_overrides", {}),
                old_overrides   = old_review.get("field_overrides", {}),
                approved_fields = review.get("human_approved", []),
            )
            if not ok:
                # Do NOT touch the in-memory cache: a later read must not report
                # a state that was never committed.
                return False
        _reviews[stem] = review
        return True

    # ── Review app routes ─────────────────────────────────────────────────────

    @flask_app.route("/api/docs")
    @login_required
    def review_api_docs():
        from flask import jsonify as _jsonify
        stems = []
        for doc_type in schema.DOC_TYPES:
            folder = EXTRACT_RESULTS / PIPELINE / doc_type
            if not folder.is_dir():
                continue
            stems.extend(sorted(jf.stem for jf in folder.glob("*.json")))

        # Bulk-fetch all reviews in one query (one DB round-trip vs. N)
        reviews_map = _get_reviews_bulk(stems)

        docs = []
        for stem in stems:
            review    = reviews_map.get(stem) or {}
            overrides = review.get("field_overrides") or {}
            # Same authoritative computation the review panel and the verify
            # endpoint use, so the board can never report a document ready on
            # weaker criteria than verification actually enforces (item #16).
            state = core.compute_doc_state(stem, overrides)
            if state is None:
                log.warning("Skipping unreadable document: %s", stem)
                continue
            fields = state["fields"]
            entity_name = ""
            for fname in schema.ENTITY_NAME_FIELDS:
                entity_name = overrides.get(fname) or fields.get(fname) or ""
                if entity_name:
                    break
            docs.append({
                "stem":          stem,
                "doc_type":      state["doc_type"],
                "label":         _REVIEW_DOC_TYPE_LABELS.get(state["doc_type"], state["doc_type"]),
                "group_id":      core.group_id_for(stem),
                "found":         state["found"],
                "grounded":      state["grounded"],
                "total":         state["total"],
                "verified":      bool(review.get("verified", False)),
                "customer_name": entity_name,
            })
        return _jsonify(docs)

    def _persist_document(stem: str, state: dict) -> bool:
        """Upsert the document row so reviews.stem's FK can never dangle (item #4).

        reviews.stem REFERENCES documents(stem). The document row used to be
        written only when /api/doc/<stem> was opened, so batch-verifying a
        never-opened document violated the FK — the review write failed while the
        server still moved the file. Verification now upserts first.

        Returns False if Supabase is enabled and the upsert failed. The caller
        must not proceed: with a stale FK row already present the review write
        could otherwise succeed against out-of-date document metadata.
        """
        ok = sb.upsert_document(
            stem                = stem,
            doc_type            = state["doc_type"],
            group_id            = core.group_id_for(stem),
            extracted_fields    = state["fields"],
            extraction_metadata = state["extraction_metadata"],
            confidence_summary  = state["confidence_summary"],
        )
        if sb.is_enabled() and not ok:
            log.error("Document upsert FAILED for %s", stem)
            return False
        return True

    @flask_app.route("/api/doc/<stem>")
    @login_required
    def review_api_doc(stem: str):
        from flask import abort as _abort, jsonify as _jsonify
        stem = core.sanitize_stem(stem)
        review = _get_review(stem)
        state = core.compute_doc_state(stem, review.get("field_overrides") or {})
        if state is None:
            _abort(404)

        _persist_document(stem, state)
        # Lazy-upload extract JSON to Supabase Storage (non-blocking; logs on failure)
        sb.lazy_upload(sb.BUCKET_EXTRACT,
                       f"{PIPELINE}/{state['doc_type']}/{stem}.json",
                       state["extract_path"], "application/json")

        file_ext = next(
            (e for e in _FILE_EXTS
             if (DOCS_DIR / f"{stem}{e}").exists() or (DOCS_OUTBOX_DIR / f"{stem}{e}").exists()),
            None,
        )
        return _jsonify({
            "stem":               stem,
            "doc_type":           state["doc_type"],
            "label":              _REVIEW_DOC_TYPE_LABELS.get(state["doc_type"], state["doc_type"]),
            "fields":             state["fields"],
            "field_labels":       _REVIEW_FIELD_LABELS.get(state["doc_type"], {}),
            "referenced":         state["referenced"],
            "ref_boxes":          state["ref_boxes"],
            "field_fallback":     state["field_fallback"],
            "field_source":       state["field_source"],
            "confidence_summary": state["confidence_summary"],
            "field_high_conf":    state["field_high_conf"],
            "threshold":          state["threshold"],
            "chunk_index":        state["chunk_index"],   # powers viewer search
            "review":             review,
            "file_ext":           file_ext,
        })

    def _validate_overrides(raw, field_keys: list) -> tuple[dict, str | None]:
        """Whitelist override keys against schema.py and check value types.

        The client previously supplied arbitrary keys and values straight into
        persistence. Anything not declared in FIELD_LABELS for this doc type is
        now rejected outright.
        """
        if raw is None:
            return {}, None
        if not isinstance(raw, dict):
            return {}, "field_overrides must be an object"
        allowed = set(field_keys)
        clean: dict = {}
        for k, v in raw.items():
            if k not in allowed:
                return {}, f"Unknown field: {k}"
            if v is None or v == "" or v == []:
                continue                       # empty override == no override
            if isinstance(v, str):
                clean[k] = v
            elif isinstance(v, list):
                # Array-typed field: a list of flat objects (e.g. line items).
                if not all(isinstance(item, dict) for item in v):
                    return {}, f"Field {k}: array items must be objects"
                clean[k] = v
            else:
                return {}, f"Field {k}: expected string or array, got {type(v).__name__}"
        return clean, None

    @flask_app.route("/api/review/<stem>", methods=["POST"])
    @login_required
    def review_save(stem: str):
        from flask import request as _req, jsonify as _jsonify, abort as _abort
        stem = core.sanitize_stem(stem)

        try:
            body = _req.get_json(silent=True)
        except Exception:
            body = None
        if body is None:
            return _jsonify({"error": "Invalid JSON"}), 400

        old_review = _get_review(stem)   # fetch before overwriting (for audit diff)

        state = core.compute_doc_state(stem, old_review.get("field_overrides") or {})
        if state is None:
            _abort(404)
        field_keys = state["field_keys"]

        # Idempotent re-verify: a retry, a second tab, or a double-click must not
        # rewrite verified_at/verified_by or append another 'verify' audit row.
        # The first sign-off is the one of record.
        if bool(body.get("verified", False)) and old_review.get("verified"):
            log.info("Re-verify ignored for %s (already verified by %s)",
                     stem, old_review.get("verified_by"))
            return _jsonify({
                "ok":          True,
                "verified":    True,
                "verified_at": old_review.get("verified_at", ""),
                "verified_by": old_review.get("verified_by", ""),
                "moved":       False,
                "idempotent":  True,
            })

        overrides, err = _validate_overrides(body.get("field_overrides"), field_keys)
        if err:
            return _jsonify({"error": err}), 400

        # Recompute readiness against the SUBMITTED overrides, not the stored ones.
        state = core.compute_doc_state(stem, overrides)
        if state is None:
            _abort(404)

        verified = bool(body.get("verified", False))
        human_approved: list = []

        if verified:
            # ── Server-authoritative verification (item #1) ────────────────────
            # A field may be signed off when ADE grounded it at/above the
            # threshold, when the reviewer overrode it, or when the reviewer
            # explicitly approved it. Approvals come from the client, but the
            # server decides which fields still NEED one and rejects the request
            # if any is missing. A forged payload can no longer verify a document
            # whose fields were never looked at.
            raw_approvals = body.get("approvals") or {}
            if not isinstance(raw_approvals, dict):
                return _jsonify({"error": "approvals must be an object"}), 400
            unknown = set(raw_approvals) - set(field_keys)
            if unknown:
                return _jsonify({"error": f"Unknown field(s) in approvals: {sorted(unknown)}"}), 400
            approved = {k for k, v in raw_approvals.items() if v is True}

            missing = [
                k for k in field_keys
                if not state["auto_ok"].get(k) and k not in approved
            ]
            # Fields a person had to vouch for — ADE could not auto-approve them.
            # These become 'approve' rows in the audit log.
            human_approved = sorted(
                k for k in field_keys
                if k in approved and not state["auto_ok"].get(k)
            )
            if missing:
                labels = _REVIEW_FIELD_LABELS.get(state["doc_type"], {})
                return _jsonify({
                    "error":   "Cannot verify: some fields are neither high-confidence nor approved.",
                    "missing": [{"key": k, "label": labels.get(k, k)} for k in missing],
                }), 409

        new_review = {
            "verified":        verified,
            # Reviewer identity and timestamp come from the server, never the
            # client. The audit trail is now trustworthy.
            "verified_at":     datetime.now(timezone.utc).isoformat() if verified
                               else str(old_review.get("verified_at", "")),
            "verified_by":     session["username"] if verified
                               else str(old_review.get("verified_by", "")),
            "field_overrides": overrides,
            "human_approved":  human_approved,
        }

        # Ensure the FK target row exists — and is current — before the review write.
        if not _persist_document(stem, state):
            return _jsonify({
                "error": "Could not save the document record. Nothing was changed.",
            }), 503

        if not _save_review(stem, new_review, old_review, actor=session["username"]):
            log.error("Review persistence FAILED for %s — document left in inbox", stem)
            return _jsonify({
                "error": "Could not save the review. The document was not verified; nothing was moved.",
            }), 503

        # Only after a confirmed write do we move the source document.
        moved = None
        if verified:
            try:
                moved = _move_doc(stem, DOCS_DIR, DOCS_OUTBOX_DIR)
            except OSError:
                # The review IS committed; the file move is a recoverable
                # follow-up, so report success but say the move did not happen.
                log.exception("Verified %s but could not move it to the outbox", stem)
                return _jsonify({"ok": True, "moved": False,
                                 "warning": "Review saved, but the file could not be moved."}), 200

        log.info("Review saved: stem=%s verified=%s by=%s", stem, verified, session["username"])
        return _jsonify({
            "ok":          True,
            "verified":    verified,
            "verified_at": new_review["verified_at"],
            "verified_by": new_review["verified_by"],
            "moved":       bool(moved),
        })

    def _move_doc(stem: str, src_dir: Path, dst_dir: Path):
        """Move a document between inbox and outbox. Idempotent."""
        dst_dir.mkdir(parents=True, exist_ok=True)
        for ext in _FILE_EXTS:
            src = src_dir / f"{stem}{ext}"
            if src.exists():
                dst = dst_dir / src.name
                shutil.move(str(src), str(dst))
                return dst
        return None

    @flask_app.route("/api/reset", methods=["POST"])
    @admin_required
    def review_reset():
        """Clear review decisions AND return verified documents to the inbox.

        The UI promises a reset to "start again from the beginning", but this
        used to leave every verified file stranded in the outbox, so a reset
        document could never be reviewed a second time (item #12).
        """
        from flask import jsonify as _jsonify
        stems = []
        for doc_type in schema.DOC_TYPES:
            folder = EXTRACT_RESULTS / PIPELINE / doc_type
            if folder.is_dir():
                stems.extend(jf.stem for jf in folder.glob("*.json"))

        restored = 0
        for stem in stems:
            try:
                if _move_doc(stem, DOCS_OUTBOX_DIR, DOCS_DIR):
                    restored += 1
            except OSError:
                log.exception("Reset: could not restore %s to the inbox", stem)

        _reviews.clear()
        sb.clear_all_reviews()   # no-op if Supabase not configured
        log.info("Reset by %s — %d document(s) restored to the inbox",
                 session.get("username"), restored)
        return _jsonify({"ok": True, "restored": restored})


    @flask_app.route("/pdf/<stem>")
    @login_required
    def review_serve_pdf(stem: str):
        from flask import send_file as _sf, abort as _abort
        stem = re.sub(r"[^a-zA-Z0-9_\-]", "", stem)
        _EXT_MIME = [
            (".pdf",  "application/pdf"),
            (".png",  "image/png"),
            (".jpg",  "image/jpeg"),
            (".jpeg", "image/jpeg"),
            (".gif",  "image/gif"),
            (".webp", "image/webp"),
            (".txt",  "text/plain; charset=utf-8"),
            (".docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
            (".pptx", "application/vnd.openxmlformats-officedocument.presentationml.presentation"),
        ]
        for ext, mime in _EXT_MIME:
            for search_dir in (DOCS_DIR, DOCS_OUTBOX_DIR):
                p = search_dir / f"{stem}{ext}"
                if p.exists():
                    sb.lazy_upload(sb.BUCKET_PDFS, f"{stem}{ext}", p, mime)
                    return _sf(p, mimetype=mime)
        _abort(404)

    # ── Settings routes ───────────────────────────────────────────────────────

    @flask_app.route("/api/settings", methods=["GET"])
    @login_required
    def api_settings_get():
        return jsonify(_load_settings())

    @flask_app.route("/api/settings", methods=["POST"])
    @admin_required
    def api_settings_post():
        try:
            body = request.get_json() or {}
        except Exception:
            return jsonify({"error": "Invalid JSON"}), 400
        doc_type = body.get("doc_type")
        threshold = body.get("threshold")
        if doc_type not in schema.DOC_TYPES:
            return jsonify({"error": f"Unknown doc_type: {doc_type}"}), 400
        try:
            threshold = float(threshold)
        except (TypeError, ValueError):
            return jsonify({"error": "threshold must be a number"}), 400
        if not (0.0 <= threshold <= 1.0):
            return jsonify({"error": "threshold must be between 0.0 and 1.0"}), 400
        with core.settings_transaction() as settings:
            settings.setdefault("thresholds", {})[doc_type] = threshold
        return jsonify({"ok": True, "threshold": threshold})

    # ── Users routes ──────────────────────────────────────────────────────────

    @flask_app.route("/api/users", methods=["GET"])
    @admin_required
    def api_users_list():
        users = _load_users()
        result = [
            {"uid": uid, "username": u["username"], "role": u["role"]}
            for uid, u in users.items()
        ]
        return jsonify(result)

    @flask_app.route("/api/users", methods=["POST"])
    @admin_required
    def api_users_create():
        body = request.get_json(silent=True)
        if body is None:
            return jsonify({"error": "Invalid JSON"}), 400
        username = (body.get("username") or "").strip()
        password = body.get("password") or ""
        role     = body.get("role") or ""
        if not username:
            return jsonify({"error": "username required"}), 400
        pw_err = validate_password(password)
        if pw_err:
            return jsonify({"error": pw_err}), 400
        if role not in ("admin", "user"):
            return jsonify({"error": "role must be 'admin' or 'user'"}), 400
        uid = str(uuid.uuid4())
        # Load+mutate+save under one lock, so two concurrent creates cannot both
        # read the old dict and have the second clobber the first.
        with core.users_transaction() as users:
            if any(u["username"] == username for u in users.values()):
                return jsonify({"error": "Username already exists"}), 409
            users[uid] = {
                "username":        username,
                "password_hash":   generate_password_hash(password),
                "role":            role,
                "session_version": 1,
            }
        log.info("User created: %s (role=%s) by %s", username, role, session.get("username"))
        return jsonify({"ok": True, "uid": uid}), 201

    @flask_app.route("/api/users/<uid>", methods=["DELETE"])
    @admin_required
    def api_users_delete(uid: str):
        if uid == session.get("user_id"):
            return jsonify({"error": "Cannot delete yourself"}), 400
        with core.users_transaction() as users:
            if uid not in users:
                return jsonify({"error": "User not found"}), 404
            # The last-admin check and the delete must be atomic, or two
            # concurrent deletes can each see two admins and remove both.
            admins = [u for u in users.values() if u["role"] == "admin"]
            if users[uid]["role"] == "admin" and len(admins) <= 1:
                return jsonify({"error": "Cannot delete the last admin"}), 400
            username = users[uid]["username"]
            del users[uid]
        # The deleted user's cookie no longer resolves to a record, so
        # _current_user() rejects it on their very next request.
        log.info("User deleted: %s by %s", username, session.get("username"))
        return jsonify({"ok": True})

    @flask_app.route("/api/users/<uid>/reset-password", methods=["POST"])
    @admin_required
    def api_users_reset_password(uid: str):
        body = request.get_json(silent=True)
        if body is None:
            return jsonify({"error": "Invalid JSON"}), 400
        password = body.get("password") or ""
        pw_err = validate_password(password)
        if pw_err:
            return jsonify({"error": pw_err}), 400
        with core.users_transaction() as users:
            if uid not in users:
                return jsonify({"error": "User not found"}), 404
            users[uid]["password_hash"] = generate_password_hash(password)
            # Bump session_version so any live session for this user is revoked.
            # Under the lock: a concurrent write must not drop this bump, or the
            # revoked cookie would keep working.
            users[uid]["session_version"] = int(users[uid].get("session_version", 1)) + 1
            target = users[uid]["username"]
        log.info("Password reset for %s by %s (sessions revoked)", target, session.get("username"))
        return jsonify({"ok": True})

    @flask_app.route("/api/change-password", methods=["POST"])
    @login_required
    def api_change_password():
        body = request.get_json(silent=True)
        if body is None:
            return jsonify({"error": "Invalid JSON"}), 400
        current_password = body.get("current_password") or ""
        new_password     = body.get("new_password") or ""
        # One policy, shared with account creation and admin reset (item #8).
        pw_err = validate_password(new_password)
        if pw_err:
            return jsonify({"error": pw_err}), 400
        uid = session.get("user_id")
        with core.users_transaction() as users:
            if uid not in users:
                return jsonify({"error": "User not found"}), 404
            if not check_password_hash(users[uid]["password_hash"], current_password):
                log.warning("Failed password change for %s (wrong current password)",
                            users[uid]["username"])
                return jsonify({"error": "Current password is incorrect"}), 400
            users[uid]["password_hash"] = generate_password_hash(new_password)
            users[uid]["session_version"] = int(users[uid].get("session_version", 1)) + 1
            new_version = users[uid]["session_version"]
            who = users[uid]["username"]
        # Keep the caller signed in on their new version; other sessions die.
        session["session_version"] = new_version
        log.info("Password changed for %s (other sessions revoked)", who)
        return jsonify({"ok": True})

    @flask_app.route("/api/password-policy")
    @login_required
    def api_password_policy():
        """So the UI can never disagree with the server about the minimum length."""
        return jsonify({"min_length": PASSWORD_MIN_LEN})

    return flask_app


def run_web():
    flask_app = make_flask_app()
    port = int(os.environ.get("PORT", 8080))
    print(f"\n" + "─" * 60)
    print(f"HITL Document Review  →  http://localhost:{port}")
    print("─" * 60 + "\n")
    flask_app.run(debug=False, host="0.0.0.0", port=port, threaded=True)


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 3 — ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

def main():
    args  = sys.argv[1:]
    flags = {a for a in args if a.startswith("--")}

    # ── Parse mode ────────────────────────────────────────────────────────────
    if "--parse" in flags:
        parse_all = "--all" in flags
        force     = "--force" in flags
        limit = 2
        for arg in args:
            try:
                if arg.startswith("--limit="):
                    limit = int(arg.split("=")[1])
                elif arg == "--limit":
                    idx = args.index(arg)
                    if idx + 1 < len(args):
                        limit = int(args[idx + 1])
            except ValueError:
                print(f"[error] --limit requires an integer value"); sys.exit(1)
        run_parse(parse_all=parse_all, limit=limit, force=force)
        return

    # ── Web mode (default) ────────────────────────────────────────────────────
    run_web()


if __name__ == "__main__":
    main()
