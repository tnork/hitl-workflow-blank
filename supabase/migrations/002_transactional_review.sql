-- HITL Workflow — transactional review + audit write, and table lockdown
-- ──────────────────────────────────────────────────────────────────────────────
-- 1. save_review_with_audit(): the app used to upsert `reviews` and then insert
--    `field_audit_log` rows as two separate statements. If the audit insert
--    failed, the review was already committed and the change had no audit trail.
--    A plpgsql function body runs inside a single transaction, so a failure in
--    either statement rolls back both.
--
-- 2. p_actor: the audit `reviewer` column used to be filled from `verified_by`,
--    which is only set on sign-off — so a field edit saved WITHOUT verifying
--    recorded no actor. p_actor is the logged-in user making the change and is
--    always present.
--
-- 3. RLS: 001_initial.sql created the tables but never enabled row-level
--    security or revoked the default grants, so anyone holding the anon or
--    authenticated key could read and write reviews directly, bypassing the app
--    (and the RPC's own EXECUTE revoke). Lock the tables down: only the
--    service-role key the server uses may touch them.
--
-- Run after 001_initial.sql:  supabase db push  OR  paste into the SQL Editor.

-- ── 1 + 2: transactional review + audit ───────────────────────────────────────

DROP FUNCTION IF EXISTS public.save_review_with_audit(
    TEXT, BOOLEAN, TIMESTAMPTZ, TEXT, JSONB, JSONB
);

CREATE OR REPLACE FUNCTION public.save_review_with_audit(
    p_stem            TEXT,
    p_verified        BOOLEAN,
    p_verified_at     TIMESTAMPTZ,
    p_verified_by     TEXT,
    p_actor           TEXT,
    p_field_overrides JSONB,
    p_audit           JSONB
) RETURNS VOID
LANGUAGE plpgsql
SECURITY INVOKER
AS $$
BEGIN
    INSERT INTO public.reviews (stem, verified, verified_at, verified_by, field_overrides, updated_at)
    VALUES (
        p_stem,
        COALESCE(p_verified, FALSE),
        p_verified_at,
        NULLIF(p_verified_by, ''),
        COALESCE(p_field_overrides, '{}'::jsonb),
        NOW()
    )
    ON CONFLICT (stem) DO UPDATE SET
        verified        = EXCLUDED.verified,
        verified_at     = EXCLUDED.verified_at,
        verified_by     = EXCLUDED.verified_by,
        field_overrides = EXCLUDED.field_overrides,
        updated_at      = NOW();

    IF p_audit IS NOT NULL AND jsonb_array_length(p_audit) > 0 THEN
        INSERT INTO public.field_audit_log
            (stem, field_key, old_value, new_value, reviewer, action, changed_at)
        SELECT
            p_stem,
            e ->> 'field_key',
            e ->> 'old_value',
            e ->> 'new_value',
            NULLIF(p_actor, ''),          -- always the user who made THIS change
            COALESCE(e ->> 'action', 'override'),
            NOW()
        FROM jsonb_array_elements(p_audit) AS e;
    END IF;
END;
$$;

-- Postgres grants EXECUTE to PUBLIC by default. Revoke it so the anon and
-- authenticated roles cannot write reviews; the server calls this with the
-- service-role key.
REVOKE EXECUTE ON FUNCTION public.save_review_with_audit(
    TEXT, BOOLEAN, TIMESTAMPTZ, TEXT, TEXT, JSONB, JSONB
) FROM PUBLIC;

GRANT EXECUTE ON FUNCTION public.save_review_with_audit(
    TEXT, BOOLEAN, TIMESTAMPTZ, TEXT, TEXT, JSONB, JSONB
) TO service_role;

-- ── 3: lock the tables down ───────────────────────────────────────────────────
-- Enable RLS and add no policies: with RLS on and zero policies, every role is
-- denied. service_role bypasses RLS entirely, which is exactly what the server
-- needs and what anon/authenticated must not have.

ALTER TABLE public.documents       ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.reviews         ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.field_audit_log ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.documents       FORCE ROW LEVEL SECURITY;
ALTER TABLE public.reviews         FORCE ROW LEVEL SECURITY;
ALTER TABLE public.field_audit_log FORCE ROW LEVEL SECURITY;

-- Belt and braces: strip the direct table privileges too, so a future policy
-- added by mistake cannot silently open these up to the public API roles.
REVOKE ALL ON public.documents       FROM anon, authenticated;
REVOKE ALL ON public.reviews         FROM anon, authenticated;
REVOKE ALL ON public.field_audit_log FROM anon, authenticated;

GRANT ALL ON public.documents       TO service_role;
GRANT ALL ON public.reviews         TO service_role;
GRANT ALL ON public.field_audit_log TO service_role;
