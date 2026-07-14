"""
supabase_client.py — Supabase persistence layer for the HITL review workflow.

Requires environment variables (set in .env or Supabase Space secrets):
    SUPABASE_URL               — https://<project-ref>.supabase.co
    SUPABASE_SERVICE_ROLE_KEY  — service role key (server-side only; bypasses RLS)

If either var is missing this module is a no-op and web_app.py falls back to its
in-memory _reviews dict — so local dev without Supabase works with no changes.

Storage buckets (created automatically on first use):
    docs            — raw PDF files  (lazy-uploaded on first /pdf/<stem> serve)
    parse-results   — ADE raw .txt   (lazy-uploaded on first parse result access)
    extract-results — extracted JSON  (lazy-uploaded on first /api/doc access)

Tables (created by supabase/migrations/001_initial.sql):
    documents       — one row per document stem; extracted fields + metadata
    reviews         — current review state; upserted on every POST /api/review
    field_audit_log — immutable log of every field override and verify event
"""

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

# ── Lazy client init ───────────────────────────────────────────────────────────

_client  = None
_enabled = None   # None = not yet resolved; True/False after first _get_client() call

def _get_client():
    global _client, _enabled
    if _enabled is not None:
        return _client   # already resolved — return cached result (or None)
    url = os.environ.get("SUPABASE_URL", "").strip()
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "").strip()
    if not url or not key:
        log.info("Supabase: SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY not set — using in-memory fallback.")
        _enabled = False
        return None
    try:
        from supabase import create_client
        _client  = create_client(url, key)
        _enabled = True
        log.info("Supabase: connected to %s", url)
        return _client
    except Exception as exc:
        log.warning("Supabase init failed (%s) — using in-memory fallback.", exc)
        _enabled = False
        return None


def is_enabled() -> bool:
    """True if Supabase is configured and the client initialized successfully."""
    return bool(_get_client())


# ── Storage ────────────────────────────────────────────────────────────────────

BUCKET_PDFS    = "docs"
BUCKET_PARSE   = "parse-results"
BUCKET_EXTRACT = "extract-results"

_buckets_ensured: set = set()
_uploaded: set        = set()   # paths already uploaded this process lifetime


def _ensure_bucket(client, bucket: str) -> None:
    """Create the bucket if it doesn't exist yet (idempotent)."""
    if bucket in _buckets_ensured:
        return
    try:
        client.storage.get_bucket(bucket)
    except Exception:
        try:
            client.storage.create_bucket(bucket, options={"public": False})
            log.info("Supabase: created storage bucket '%s'", bucket)
        except Exception as exc:
            log.warning("Supabase: could not ensure bucket '%s': %s", bucket, exc)
    _buckets_ensured.add(bucket)


def lazy_upload(bucket: str, storage_path: str, local_path: Path,
                content_type: str = "application/octet-stream") -> bool:
    """
    Upload a local file to Supabase Storage if not already uploaded this session.
    Uses upsert=true so re-uploading the same path is safe but avoided via the
    in-memory _uploaded set to prevent redundant network I/O on repeated requests.
    Returns True on success or if already uploaded; False if Supabase is disabled
    or the local file doesn't exist.
    """
    cache_key = f"{bucket}/{storage_path}"
    if cache_key in _uploaded:
        return True
    client = _get_client()
    if not client:
        return False
    if not local_path.exists():
        return False
    try:
        _ensure_bucket(client, bucket)
        client.storage.from_(bucket).upload(
            storage_path,
            local_path.read_bytes(),
            file_options={"content-type": content_type, "upsert": "true"},
        )
        _uploaded.add(cache_key)
        log.debug("Supabase storage: uploaded %s/%s", bucket, storage_path)
        return True
    except Exception as exc:
        log.warning("Supabase storage upload failed (%s/%s): %s", bucket, storage_path, exc)
        return False


# ── Documents ──────────────────────────────────────────────────────────────────

def upsert_document(stem: str, doc_type: str, group_id: str,
                    extracted_fields: dict, extraction_metadata: dict,
                    confidence_summary: dict) -> bool:
    """
    Upsert a document row. Called on first /api/doc/<stem> access.
    Idempotent — safe to call on every request (Postgres ON CONFLICT DO UPDATE).
    """
    client = _get_client()
    if not client:
        return False
    try:
        client.table("documents").upsert({
            "stem":                stem,
            "doc_type":            doc_type,
            "group_id":            group_id,
            "extracted_fields":    extracted_fields,
            "extraction_metadata": extraction_metadata,
            "confidence_summary":  confidence_summary,
        }).execute()
        return True
    except Exception as exc:
        log.warning("Supabase upsert_document failed (%s): %s", stem, exc)
        return False


# ── Reviews ────────────────────────────────────────────────────────────────────

def _row_to_review(row: dict) -> dict:
    return {
        "verified":        row.get("verified", False),
        "verified_at":     row.get("verified_at") or "",
        "verified_by":     row.get("verified_by") or "",
        "field_overrides": row.get("field_overrides") or {},
    }


def get_review(stem: str) -> dict:
    """Return the current review state for stem, or {} if none stored yet."""
    client = _get_client()
    if not client:
        return {}
    try:
        res  = client.table("reviews").select("*").eq("stem", stem).execute()
        rows = res.data or []
        return _row_to_review(rows[0]) if rows else {}
    except Exception as exc:
        log.warning("Supabase get_review failed (%s): %s", stem, exc)
        return {}


def get_reviews_bulk(stems: list) -> dict:
    """
    Bulk-fetch reviews for a list of stems in a single query.
    Returns {stem: review_dict}. Missing stems are absent from the result.
    """
    client = _get_client()
    if not client or not stems:
        return {}
    try:
        res  = client.table("reviews").select("*").in_("stem", stems).execute()
        return {row["stem"]: _row_to_review(row) for row in (res.data or [])}
    except Exception as exc:
        log.warning("Supabase get_reviews_bulk failed: %s", exc)
        return {}


def _audit_value(val) -> str | None:
    """Serialize an override value for the audit log's TEXT columns.

    Overrides are either scalars (stored as-is) or arrays/objects for array-typed
    fields. Previously the raw dict/list was handed to a TEXT column, which the
    driver coerced inconsistently. Serialize non-strings as JSON so the column
    always holds one well-defined representation.
    """
    if val is None:
        return None
    if isinstance(val, str):
        return val
    return json.dumps(val, sort_keys=True, default=str)


def save_review(stem: str, verified: bool, verified_at: str, verified_by: str,
                field_overrides: dict, old_overrides: dict, actor: str = "",
                approved_fields: list | None = None) -> bool:
    """
    Upsert the review row and append an audit entry for every changed field, in
    a SINGLE transaction (see supabase/migrations/002_transactional_review.sql).

    Returns True only if the write is durably committed. The caller MUST NOT
    treat a False return as success — the document has not been persisted.

    old_overrides — field_overrides from the previous review state, used to
                    compute the diff for the audit log. Pass {} on first save.
    actor         — the logged-in user making THIS change. Recorded as the audit
                    reviewer for every row. Distinct from verified_by, which is
                    only set on sign-off: attributing audit rows to verified_by
                    left every non-verifying field edit with no actor at all.

    Audit log actions:
        'override'   — field was added or changed
        'clear'      — field override was removed
        'verify'     — document marked verified (field_key = '__verified__')
    """
    client = _get_client()
    if not client:
        return False

    # Build the audit diff — one row per field that changed
    audit_rows = []
    for key in set(old_overrides) | set(field_overrides):
        old_val = old_overrides.get(key)
        new_val = field_overrides.get(key)
        if old_val != new_val:
            audit_rows.append({
                "field_key": key,
                "old_value": _audit_value(old_val),
                "new_value": _audit_value(new_val),
                "action":    "override" if new_val else "clear",
            })

    # Record every field a human had to explicitly sign off (i.e. one that ADE
    # did not ground confidently enough to auto-approve). Without this the audit
    # log shows the document was verified but not WHICH fields a person actually
    # vouched for — the whole point of a human-in-the-loop trail.
    for key in sorted(approved_fields or []):
        audit_rows.append({
            "field_key": key,
            "old_value": None,
            "new_value": "approved",
            "action":    "approve",
        })

    if verified:
        audit_rows.append({
            "field_key": "__verified__",
            "old_value": None,
            "new_value": "true",
            "action":    "verify",
        })

    try:
        client.rpc("save_review_with_audit", {
            "p_stem":            stem,
            "p_verified":        bool(verified),
            "p_verified_at":     verified_at or None,
            "p_verified_by":     verified_by or None,
            "p_actor":           actor or verified_by or None,
            "p_field_overrides": field_overrides or {},
            "p_audit":           audit_rows,
        }).execute()
        return True
    except Exception as exc:
        # Fail closed: the caller returns 5xx and leaves the document in the inbox.
        log.error("Supabase save_review failed (%s): %s", stem, exc)
        return False


def clear_all_reviews() -> bool:
    """
    Delete all review rows (demo reset).
    Audit log is preserved — the reset itself is not logged, but field_audit_log
    entries remain for historical reference.
    """
    client = _get_client()
    if not client:
        return False
    try:
        # neq with a value that never matches any real stem deletes all rows.
        # supabase-py requires at least one filter on DELETE.
        client.table("reviews").delete().neq("stem", "").execute()
        return True
    except Exception as exc:
        log.warning("Supabase clear_all_reviews failed: %s", exc)
        return False
