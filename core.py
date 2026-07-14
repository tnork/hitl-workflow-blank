"""
core.py — Shared configuration, paths, persistence helpers, and document state.

Single source of truth for anything web_app.py and extract_docs.py both need.
Previously PIPELINE and the parse-section helpers were duplicated across both
files and had to be kept in sync by hand; they now live here.

The authoritative document-readiness computation (`compute_doc_state`) also
lives here so that the task board, the review panel, and the verification
endpoint can never disagree about whether a document is ready to verify.
"""

import json
import logging
import os
import re
import tempfile
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path

from werkzeug.security import generate_password_hash

import schema

log = logging.getLogger("hitl")

# ─── Pipeline ─────────────────────────────────────────────────────────────────
# Change PIPELINE to switch pipelines (e.g. "jobs"). Both the parser and the web
# app read it from here, so there is only one place to change it.
PIPELINE = os.environ.get("PIPELINE", "docs")

BASE_DIR        = Path(__file__).parent
DOCS_DIR        = BASE_DIR / f"{PIPELINE}_inbox"
DOCS_OUTBOX_DIR = BASE_DIR / f"{PIPELINE}_outbox"
PARSE_RESULTS   = BASE_DIR / "parse_results"
EXTRACT_RESULTS = BASE_DIR / "extract_results"
SETTINGS_FILE   = BASE_DIR / "settings.json"
USERS_FILE      = BASE_DIR / "users.json"
SECRET_KEY_FILE = BASE_DIR / ".secret_key"

SEP = "─" * 80

# Supported document file extensions (priority order for serving)
FILE_EXTS = [".pdf", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".txt", ".docx", ".pptx"]

DEFAULT_THRESHOLD = 0.95

# ─── Password policy (single source of truth — item #8) ───────────────────────
PASSWORD_MIN_LEN = 12
PASSWORD_POLICY_MSG = f"Password must be at least {PASSWORD_MIN_LEN} characters."


def validate_password(pw: str) -> str | None:
    """Return an error message if pw violates policy, else None."""
    if not isinstance(pw, str) or len(pw) < PASSWORD_MIN_LEN:
        return PASSWORD_POLICY_MSG
    return None


# ─── Startup validation ───────────────────────────────────────────────────────

def validate_config() -> None:
    """Fail fast on a malformed schema.py rather than 500ing at request time."""
    if not schema.DOC_TYPES:
        raise RuntimeError("schema.DOC_TYPES is empty — define at least one document type.")
    for dt in schema.DOC_TYPES:
        if dt not in schema.FIELD_LABELS:
            raise RuntimeError(f"schema.FIELD_LABELS is missing an entry for doc type {dt!r}.")
        if dt not in schema.EXTRACTION_SCHEMAS:
            raise RuntimeError(f"schema.EXTRACTION_SCHEMAS is missing an entry for doc type {dt!r}.")
        props = (schema.EXTRACTION_SCHEMAS[dt] or {}).get("properties") or {}
        unknown = set(schema.FIELD_LABELS[dt]) - set(props)
        if unknown:
            raise RuntimeError(
                f"schema.FIELD_LABELS[{dt!r}] has keys absent from the extraction schema: "
                f"{sorted(unknown)}"
            )


# ─── Atomic JSON persistence (item #13) ───────────────────────────────────────
# Guards the read-modify-write cycle on users.json / settings.json. A process
# lock serializes concurrent threads; os.replace makes the write atomic so an
# interrupted write can never leave truncated JSON behind.
#
# Ceiling: this is safe for a single multi-threaded process only. Multiple
# processes (e.g. gunicorn --workers 2) still race. Upgrade path: move users and
# settings into Postgres alongside reviews.

_file_lock = threading.RLock()


def atomic_write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _read_json_or_quarantine(path: Path, default):
    """Read JSON. Move a genuinely-corrupt file aside and return default.

    ONLY a decode failure counts as corruption. A transient OSError (EINTR, a
    full disk, a permissions blip) must propagate — quarantining on an I/O error
    would move a perfectly good file out of the way and destroy live data.
    """
    if not path.exists():
        return default
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        log.exception("Could not read %s", path)
        raise                                  # transient: do NOT quarantine
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        quarantine = path.with_suffix(path.suffix + f".corrupt.{uuid.uuid4().hex[:8]}")
        log.error("Corrupt JSON at %s (%s). Quarantining to %s", path, e, quarantine)
        try:
            os.replace(path, quarantine)
        except OSError:
            log.exception("Could not quarantine %s", path)
        return default


# ─── Settings ─────────────────────────────────────────────────────────────────

def load_settings() -> dict:
    with _file_lock:
        data = _read_json_or_quarantine(SETTINGS_FILE, None)
        if not isinstance(data, dict):
            data = {}
        thresholds = data.get("thresholds")
        if not isinstance(thresholds, dict):
            thresholds = {}
        for dt in schema.DOC_TYPES:
            thresholds.setdefault(dt, DEFAULT_THRESHOLD)
        data["thresholds"] = thresholds
        return data


def save_settings(data: dict) -> None:
    with _file_lock:
        atomic_write_json(SETTINGS_FILE, data)


def get_threshold(doc_type: str) -> float:
    return float(load_settings().get("thresholds", {}).get(doc_type, DEFAULT_THRESHOLD))


# ─── Users ────────────────────────────────────────────────────────────────────
# User records: {uid: {username, password_hash, role, session_version}}
# session_version is bumped on any credential/role change so existing cookies
# for that user stop validating (item #5).

def _bootstrap_admin() -> dict:
    """Create the initial admin. Never invents a known-weak default (item #6)."""
    pw = os.environ.get("ADMIN_PASSWORD", "")
    generated = False
    if not pw:
        import secrets
        pw = secrets.token_urlsafe(18)
        generated = True
    err = validate_password(pw)
    if err:
        raise RuntimeError(f"ADMIN_PASSWORD rejected: {err}")

    uid = str(uuid.uuid4())
    users = {uid: {
        "username":        "admin",
        "password_hash":   generate_password_hash(pw),
        "role":            "admin",
        "session_version": 1,
    }}
    atomic_write_json(USERS_FILE, users)
    if generated:
        # Printed exactly once, at creation. Not recoverable afterwards — the
        # hash is one-way. Admin must reset the password if this is lost.
        print("\n" + "=" * 68)
        print("[AUTH] Created initial admin account.")
        print(f"[AUTH]   username: admin")
        print(f"[AUTH]   password: {pw}")
        print("[AUTH] This password is shown ONCE. Store it now.")
        print("[AUTH] Set ADMIN_PASSWORD in the environment to choose your own.")
        print("=" * 68 + "\n")
    else:
        print("[AUTH] Created initial admin account from $ADMIN_PASSWORD.")
    return users


def load_users() -> dict:
    with _file_lock:
        if not USERS_FILE.exists():
            return _bootstrap_admin()
        users = _read_json_or_quarantine(USERS_FILE, None)
        if not isinstance(users, dict) or not users:
            # A quarantined/empty user file would otherwise lock everyone out
            # silently. Rebuild an admin rather than serving zero accounts.
            log.error("users.json unreadable or empty — recreating admin account.")
            return _bootstrap_admin()
        for u in users.values():
            u.setdefault("session_version", 1)   # migrate pre-existing files
        return users


def save_users(users: dict) -> None:
    with _file_lock:
        atomic_write_json(USERS_FILE, users)


@contextmanager
def users_transaction():
    """Load → mutate → save with the lock held for the WHOLE cycle.

    `load_users()` and `save_users()` each take the lock, but a route that calls
    them in sequence releases it in between. Two concurrent requests could then
    both read the old dict and the second save would clobber the first — silently
    restoring a deleted user, or dropping a password change and its
    session_version bump (which would leave a revoked cookie working).

        with core.users_transaction() as users:
            users[uid]["role"] = "admin"        # saved on clean exit
    """
    with _file_lock:
        users = load_users()
        yield users
        atomic_write_json(USERS_FILE, users)


@contextmanager
def settings_transaction():
    """Same read-modify-write guarantee for settings.json."""
    with _file_lock:
        settings = load_settings()
        yield settings
        atomic_write_json(SETTINGS_FILE, settings)


# ─── Parse-result readers (were duplicated in web_app.py + extract_docs.py) ────

def get_section_json(txt: str, section: str):
    idx = txt.find(section)
    if idx == -1:
        return [] if section == "CHUNKS" else {}
    after = txt[idx + len(section):]
    in_json = False
    lines = []
    for line in after.splitlines()[1:]:
        stripped = line.strip()
        is_sep = bool(stripped) and all(c in "─━" for c in stripped)
        if is_sep:
            if not in_json:
                in_json = True
            else:
                break
        elif in_json:
            lines.append(line)
    raw = "\n".join(lines).strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        log.warning("Malformed JSON in %s section of parse result", section)
        return [] if section == "CHUNKS" else {}


def doc_type_for_stem(stem: str) -> str | None:
    return next((dt for dt in schema.DOC_TYPES if stem.endswith(f"_{dt}")), None)


def extract_path_for(stem: str, doc_type: str) -> Path:
    return EXTRACT_RESULTS / PIPELINE / doc_type / f"{stem}.json"


def load_parse(stem: str) -> dict:
    txt_path = PARSE_RESULTS / PIPELINE / f"{stem}.txt"
    if txt_path.exists():
        txt = txt_path.read_text(encoding="utf-8")
        return {
            "chunks":    get_section_json(txt, "CHUNKS"),
            "grounding": get_section_json(txt, "GROUNDING"),
        }
    # Fallback: grounding embedded in the extract JSON (deployments without parse_results/)
    doc_type = doc_type_for_stem(stem)
    if doc_type:
        p = extract_path_for(stem, doc_type)
        if p.exists():
            data = _read_json_or_quarantine(p, {}) or {}
            return {"chunks": data.get("chunks") or [], "grounding": data.get("grounding") or {}}
    return {"chunks": [], "grounding": {}}


def group_id_for(stem: str) -> str:
    parts = stem.split("_")
    n = schema.GROUP_PREFIX_PARTS
    return "_".join(parts[:n]) if len(parts) >= n else stem


def sanitize_stem(stem: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_\-]", "", stem)


# ─── Authoritative document state (items #1 and #16) ──────────────────────────
# One computation, used by the task board, the review panel, and the verify
# endpoint. Previously the board counted "has any grounding reference" while the
# review panel required "confidence >= threshold", so the two disagreed and the
# board could auto-verify on weaker criteria than the panel enforced.

def _normalize_num(s: str) -> str:
    """Strip $, commas, trailing zeros: '89,525.00' -> '89525'."""
    s = s.replace("$", "").replace(",", "").strip()
    try:
        return str(int(float(s)))
    except (ValueError, OverflowError):
        return s


def _chunk_matches(needle: str, haystack: str):
    """Return match_type string or None. Tries exact, numeric, token."""
    h = haystack.lower()
    n = needle.lower()
    if n in h:
        return "exact"
    norm_n = _normalize_num(n)
    if len(norm_n) >= 3 and norm_n in _normalize_num(h):
        return "numeric"
    tokens = [t for t in re.split(r"\W+", n) if len(t) >= 3]
    if len(tokens) >= 2 and all(t in h for t in tokens):
        return "token"
    return None


def compute_doc_state(stem: str, overrides: dict | None = None) -> dict | None:
    """Resolve a document's fields, grounding boxes, and per-field confidence.

    Returns None if the stem has no known doc type or no extract JSON.
    `overrides` (reviewer corrections) count as satisfying a field.
    """
    overrides = overrides or {}
    doc_type = doc_type_for_stem(stem)
    if doc_type is None:
        return None
    extract_path = extract_path_for(stem, doc_type)
    if not extract_path.exists():
        return None

    extract = _read_json_or_quarantine(extract_path, None)
    if not isinstance(extract, dict):
        return None

    parse      = load_parse(stem)
    fields     = extract.get("extracted_fields") or {}
    if not isinstance(fields, dict):
        fields = {}
    ex_meta    = extract.get("extraction_metadata") or {}
    if not isinstance(ex_meta, dict):
        ex_meta = {}
    grounding  = parse.get("grounding") or {}
    chunks_raw = parse.get("chunks") or []

    # referenced: field_key → list of chunk/cell IDs
    referenced: dict = {}
    for field_key, meta in ex_meta.items():
        if isinstance(meta, dict):
            referenced[field_key] = meta.get("references") or []
        elif isinstance(meta, list):
            # Array field: meta is a list of per-item dicts. Expand into per-item
            # composite keys AND a top-level key holding all refs.
            all_refs: list = []
            for i, item_meta in enumerate(meta):
                if not isinstance(item_meta, dict):
                    continue
                for sub_key, sub_meta in item_meta.items():
                    if isinstance(sub_meta, dict):
                        sub_refs = sub_meta.get("references") or []
                        referenced[f"{field_key}__{i}__{sub_key}"] = sub_refs
                        all_refs.extend(sub_refs)
            seen_ids: set = set()
            referenced[field_key] = [r for r in all_refs if not (r in seen_ids or seen_ids.add(r))]

    # ref_boxes from top-level grounding (covers table-cell IDs too)
    ref_boxes: dict = {}
    chunk_text: dict = {}
    cell_grounding: dict = {}
    for ch in chunks_raw:
        cid = ch.get("id")
        if cid:
            chunk_text[cid] = re.sub(r"<[^>]+>", "", ch.get("markdown", "") or "").strip()
        for cell in (ch.get("cells") or []):
            cell_id = cell.get("id")
            if cell_id and cell_id not in grounding:
                cg = cell.get("grounding") or {}
                if cg.get("box"):
                    if "page" not in cg:
                        cg = dict(cg)
                        cg["page"] = (ch.get("grounding") or {}).get("page", 0)
                    cell_grounding[cell_id] = cg

    for refs in referenced.values():
        for rid in refs:
            g = grounding.get(rid) or cell_grounding.get(rid)
            if g:
                ref_boxes[rid] = {
                    "page":        g.get("page", 0),
                    "box":         g.get("box") or {},
                    "confidence":  g.get("confidence"),
                    "type":        g.get("type", ""),
                    "source_text": chunk_text.get(rid, ""),
                }

    # Fallback: fields with no refs (or refs resolving to no boxes) → text search
    field_fallback: dict = {}
    for field_key, refs in referenced.items():
        if refs and any(rid in ref_boxes for rid in refs):
            continue
        val = fields.get(field_key)
        if not val:
            continue
        needle = str(val).strip()
        if len(needle) < 3:
            continue
        for ch in chunks_raw:
            cid   = ch.get("id")
            clean = chunk_text.get(cid, "")
            if not clean:
                continue
            match_type = _chunk_matches(needle, clean)
            if match_type:
                g      = ch.get("grounding") or {}
                gentry = grounding.get(cid) or {}
                field_fallback[field_key] = {
                    "ref_id":      cid,
                    "page":        g.get("page", 0),
                    "box":         g.get("box") or {},
                    "confidence":  gentry.get("confidence"),
                    "type":        "text_search",
                    "source_text": clean[:300],
                    "match_type":  match_type,
                }
                break

    # field_source: best available source text per field
    field_source: dict = {}
    for field_key, refs in referenced.items():
        for rid in refs:
            txt = ref_boxes.get(rid, {}).get("source_text", "")
            if txt:
                field_source[field_key] = txt
                break
        if field_key not in field_source and refs:
            val = fields.get(field_key)
            if val is not None:
                field_source[field_key] = str(val)
        if field_key not in field_source and field_key in field_fallback:
            field_source[field_key] = field_fallback[field_key].get("source_text", "")

    # ── Full-text search index ────────────────────────────────────────────────
    # Every chunk that has a grounding box, with its text. ref_boxes only covers
    # chunks a field happens to reference, which is a fraction of the page — a
    # document-wide search needs all of them.
    #
    # Cost: this ships the document's full chunk text in the /api/doc payload,
    # roughly the size of the parsed markdown. Fine for the page-count these
    # reviews involve; for very large documents the upgrade path is a server-side
    # search endpoint that returns only matching boxes.
    chunk_index = []
    for ch in chunks_raw:
        cid = ch.get("id")
        text = chunk_text.get(cid, "")
        if not cid or not text:
            continue
        # Prefer whichever source actually carries a box. A top-level grounding
        # record that exists but has no box must NOT suppress the chunk's own
        # grounding — that silently dropped otherwise-searchable chunks.
        top    = grounding.get(cid) or {}
        inline = ch.get("grounding") or {}
        box    = top.get("box") or inline.get("box")
        if not box:
            continue
        # Likewise an explicit page: null must fall through, not be taken as 0.
        page = top.get("page")
        if page is None:
            page = inline.get("page")
        chunk_index.append({
            "id":   cid,
            "page": page if page is not None else 0,
            "box":  box,
            "text": text,
        })

    # Confidence summary from parse grounding
    conf_scores = [
        (grounding.get(ch.get("id")) or {}).get("confidence")
        for ch in chunks_raw
        if (grounding.get(ch.get("id")) or {}).get("confidence") is not None
    ]
    conf_summary = extract.get("confidence_summary") or {
        "avg_parse_confidence":       round(sum(conf_scores) / len(conf_scores), 2) if conf_scores else None,
        "total_chunks_scored":        len(conf_scores),
        "low_confidence_chunk_count": sum(1 for c in conf_scores if c < 0.85),
    }

    # Per-field high-confidence flag, against the configured threshold
    threshold = get_threshold(doc_type)
    field_high_conf: dict = {}
    for field_key, refs in referenced.items():
        boxes = [ref_boxes[rid] for rid in refs if rid in ref_boxes]
        field_high_conf[field_key] = bool(boxes) and \
            max((b.get("confidence") or 0) for b in boxes) >= threshold

    # ── Readiness, computed over the schema's declared fields only ──
    field_keys = list(schema.FIELD_LABELS.get(doc_type, {}).keys())
    found = sum(
        1 for k in field_keys
        if overrides.get(k) or fields.get(k) is not None
    )
    # A field is auto-approvable when the reviewer overrode it, or when ADE
    # grounded it at or above the threshold AND actually extracted a value.
    # Grounding references can exist for a field ADE ultimately returned as null;
    # auto-approving that would sign off an empty field nobody ever looked at.
    auto_ok = {
        k: bool(overrides.get(k)) or (
            bool(field_high_conf.get(k)) and fields.get(k) not in (None, "", [])
        )
        for k in field_keys
    }

    return {
        "doc_type":           doc_type,
        "extract_path":       extract_path,
        "fields":             fields,
        "extraction_metadata": ex_meta,
        "field_keys":         field_keys,
        "referenced":         referenced,
        "ref_boxes":          ref_boxes,
        "field_fallback":     field_fallback,
        "field_source":       field_source,
        "confidence_summary": conf_summary,
        "field_high_conf":    field_high_conf,
        "threshold":          threshold,
        "chunk_index":        chunk_index,
        "auto_ok":            auto_ok,
        "found":              found,
        "total":              len(field_keys),
        "grounded":           sum(1 for k in field_keys if auto_ok[k]),
    }
