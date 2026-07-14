-- HITL Workflow — Supabase Schema
-- Run via: supabase db push  OR paste into Supabase SQL Editor
-- ──────────────────────────────────────────────────────────────────────────────

-- ── documents ─────────────────────────────────────────────────────────────────
-- One row per extracted document (stem). Populated on first /api/doc access.
-- Extracted fields and metadata are stored as JSONB for schema flexibility.
CREATE TABLE IF NOT EXISTS public.documents (
    stem                 TEXT PRIMARY KEY,
    doc_type             TEXT NOT NULL,
    group_id             TEXT,
    extracted_fields     JSONB NOT NULL DEFAULT '{}'::jsonb,
    extraction_metadata  JSONB NOT NULL DEFAULT '{}'::jsonb,
    confidence_summary   JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ── reviews ───────────────────────────────────────────────────────────────────
-- Current review state per document. Upserted on every POST /api/review/<stem>.
-- verified_by is the reviewer's display name (no auth system yet).
CREATE TABLE IF NOT EXISTS public.reviews (
    stem             TEXT PRIMARY KEY REFERENCES public.documents(stem) ON DELETE CASCADE,
    verified         BOOLEAN NOT NULL DEFAULT FALSE,
    verified_at      TIMESTAMPTZ,
    verified_by      TEXT,
    field_overrides  JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ── field_audit_log ───────────────────────────────────────────────────────────
-- Immutable append-only log of every field change and verification event.
-- action values:
--   'override'  — a field value was added or changed by a reviewer
--   'clear'     — a field override was removed
--   'verify'    — document was marked verified (field_key = '__verified__')
CREATE TABLE IF NOT EXISTS public.field_audit_log (
    id          BIGSERIAL PRIMARY KEY,
    stem        TEXT NOT NULL REFERENCES public.documents(stem) ON DELETE CASCADE,
    field_key   TEXT NOT NULL,
    old_value   TEXT,
    new_value   TEXT,
    reviewer    TEXT,
    action      TEXT NOT NULL DEFAULT 'override',
    changed_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_field_audit_log_stem       ON public.field_audit_log(stem);
CREATE INDEX IF NOT EXISTS idx_field_audit_log_changed_at ON public.field_audit_log(changed_at DESC);
CREATE INDEX IF NOT EXISTS idx_reviews_verified           ON public.reviews(verified);
