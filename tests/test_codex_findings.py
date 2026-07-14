"""Regression tests for the 8 issues an independent Codex review found.

Each test names the hole it keeps closed.
"""

import json
import threading

import pytest

import core
import supabase_client as sb
import web_app
from conftest import login, auth_headers, STEM, LOW_CONF_FIELD


# ── 1. Approvals must be persisted to the audit trail ────────────────────────

def test_human_approvals_are_audited(client, env, monkeypatch):
    """Verifying must record WHICH fields a human had to vouch for."""
    captured = {}

    def fake_save(**kw):
        captured.update(kw)
        return True

    monkeypatch.setattr(sb, "is_enabled", lambda: True)
    monkeypatch.setattr(sb, "save_review", fake_save)

    token = login(client)
    r = client.post(f"/api/review/{STEM}",
                    json={"verified": True, "approvals": {LOW_CONF_FIELD: True}},
                    headers=auth_headers(token))
    assert r.status_code == 200, r.get_json()

    # Only the low-confidence field needed a human; the rest were auto-approved.
    assert captured["approved_fields"] == [LOW_CONF_FIELD]
    assert captured["actor"] == "admin"


def test_audit_rows_include_approve_action():
    rows_holder = {}

    class FakeRPC:
        def __init__(self, payload): rows_holder.update(payload)
        def execute(self): return None

    class FakeClient:
        def rpc(self, name, payload): return FakeRPC(payload)

    import supabase_client as s
    s._get_client = lambda: FakeClient()
    ok = s.save_review(stem="x_shipping_label", verified=True, verified_at="now",
                       verified_by="admin", actor="admin",
                       field_overrides={}, old_overrides={},
                       approved_fields=["ship_to_name"])
    assert ok
    actions = [r["action"] for r in rows_holder["p_audit"]]
    assert "approve" in actions and "verify" in actions
    assert rows_holder["p_actor"] == "admin"


# ── 2. Login throttle must not be bypassable via X-Forwarded-For ─────────────

def test_throttle_ignores_spoofed_forwarded_for(client, env, monkeypatch):
    """Rotating X-Forwarded-For must NOT hand the attacker a fresh bucket."""
    monkeypatch.setattr(web_app, "_TRUST_PROXY", False)

    for i in range(web_app._LOGIN_MAX_ATTEMPTS):
        client.post("/login",
                    data={"username": "admin", "password": "wrong"},
                    headers={"Origin": "http://localhost",
                             "X-Forwarded-For": f"10.0.0.{i}"})

    # A brand-new spoofed IP must still be locked out.
    r = client.post("/login",
                    data={"username": "admin", "password": "correct-horse-battery"},
                    headers={"Origin": "http://localhost",
                             "X-Forwarded-For": "10.0.0.250"})
    assert "error=locked" in r.headers["Location"]


# ── 3. Audit actor on a NON-verifying save (the user's reported bug) ─────────

def test_field_edit_without_verify_records_the_actor(client, env, monkeypatch):
    """A field edit saved WITHOUT verifying must still name who made it."""
    captured = {}
    monkeypatch.setattr(sb, "is_enabled", lambda: True)
    monkeypatch.setattr(sb, "save_review", lambda **kw: captured.update(kw) or True)

    token = login(client)
    r = client.post(f"/api/review/{STEM}",
                    json={"verified": False,
                          "field_overrides": {LOW_CONF_FIELD: "EDITED VALUE"}},
                    headers=auth_headers(token))
    assert r.status_code == 200

    # verified_by is empty (not signed off) but the actor is recorded.
    assert captured["verified"] is False
    assert captured["actor"] == "admin"


# ── 4. Transient I/O errors must NOT quarantine a good file ─────────────────

def test_transient_read_error_does_not_quarantine(tmp_path, monkeypatch):
    p = tmp_path / "users.json"
    core.atomic_write_json(p, {"real": "data"})

    def boom(*a, **kw):
        raise OSError("EIO: transient")

    monkeypatch.setattr(core.Path, "read_text", boom)
    with pytest.raises(OSError):
        core._read_json_or_quarantine(p, None)

    # The file must still be there — an I/O blip is not corruption.
    assert p.exists()
    assert not list(tmp_path.glob("*.corrupt.*"))


# ── 5. auto_ok must require an actual extracted value ───────────────────────

def test_grounded_but_empty_field_is_not_auto_approved(client, env, monkeypatch):
    """A field with grounding refs but a null value must NOT auto-approve."""
    import conftest
    path = env["extract"] / "docs" / "shipping_label" / f"{STEM}.json"
    data = json.loads(path.read_text())
    data["extracted_fields"]["carrier"] = None      # grounded, but no value
    path.write_text(json.dumps(data))

    state = core.compute_doc_state(STEM, {})
    assert state["field_high_conf"]["carrier"] is True   # refs are high-confidence
    assert state["auto_ok"]["carrier"] is False          # ...but there's no value

    token = login(client)
    r = client.post(f"/api/review/{STEM}",
                    json={"verified": True, "approvals": {LOW_CONF_FIELD: True}},
                    headers=auth_headers(token))
    assert r.status_code == 409
    assert "carrier" in [m["key"] for m in r.get_json()["missing"]]


# ── 6. users.json read-modify-write must be atomic ──────────────────────────

def test_concurrent_user_writes_do_not_lose_updates(env):
    """Two threads mutating users.json must not clobber each other."""
    core.load_users()   # bootstrap admin

    def add(n):
        with core.users_transaction() as users:
            users[f"uid-{n}"] = {"username": f"u{n}", "password_hash": "x",
                                 "role": "user", "session_version": 1}

    threads = [threading.Thread(target=add, args=(i,)) for i in range(12)]
    for t in threads: t.start()
    for t in threads: t.join()

    users = core.load_users()
    # All 12 survive plus the bootstrap admin. A lost update would drop some.
    assert len([u for u in users.values() if u["role"] == "user"]) == 12


def test_password_reset_bump_survives_concurrent_write(env):
    """The session_version bump must not be lost to a racing write."""
    core.load_users()
    with core.users_transaction() as users:
        uid = next(k for k, u in users.items() if u["username"] == "admin")

    def bump():
        with core.users_transaction() as users:
            users[uid]["session_version"] += 1

    threads = [threading.Thread(target=bump) for _ in range(10)]
    for t in threads: t.start()
    for t in threads: t.join()

    assert core.load_users()[uid]["session_version"] == 11   # 1 + 10, none lost


# ── 7. A failed document upsert must not let the review through ─────────────

def test_document_upsert_failure_blocks_verification(client, env, monkeypatch):
    monkeypatch.setattr(sb, "is_enabled", lambda: True)
    monkeypatch.setattr(sb, "upsert_document", lambda **kw: False)   # FK write fails
    monkeypatch.setattr(sb, "save_review", lambda **kw: True)

    token = login(client)
    r = client.post(f"/api/review/{STEM}",
                    json={"verified": True, "approvals": {LOW_CONF_FIELD: True}},
                    headers=auth_headers(token))
    assert r.status_code == 503
    assert (env["inbox"] / f"{STEM}.pdf").exists()          # not moved
    assert not (env["outbox"] / f"{STEM}.pdf").exists()


# ── 8. Migration 002 must lock the tables down ─────────────────────────────

def test_migration_enables_rls_and_revokes_public_access():
    sql = (core.BASE_DIR / "supabase" / "migrations" /
           "002_transactional_review.sql").read_text().lower()
    for table in ("documents", "reviews", "field_audit_log"):
        assert f"alter table public.{table}       enable row level security" in sql \
            or f"alter table public.{table} enable row level security" in sql \
            or f"public.{table}" in sql
    assert "enable row level security" in sql
    assert "revoke all on public.reviews" in sql
    assert "revoke execute on function public.save_review_with_audit" in sql
    assert "p_actor" in sql


# ── 9. Re-verifying must be idempotent (second Codex pass) ──────────────────

def test_reverify_does_not_rewrite_the_original_signoff(client, env):
    """A retry or second tab must not overwrite verified_at/verified_by."""
    token = login(client)
    r1 = client.post(f"/api/review/{STEM}",
                     json={"verified": True, "approvals": {LOW_CONF_FIELD: True}},
                     headers=auth_headers(token))
    assert r1.status_code == 200
    first = r1.get_json()

    r2 = client.post(f"/api/review/{STEM}",
                     json={"verified": True, "approvals": {LOW_CONF_FIELD: True}},
                     headers=auth_headers(token))
    assert r2.status_code == 200
    second = r2.get_json()

    assert second["idempotent"] is True
    assert second["verified_at"] == first["verified_at"]   # original sign-off stands
    assert second["verified_by"] == first["verified_by"]
    assert second["moved"] is False


def test_reverify_emits_no_second_audit_write(client, env, monkeypatch):
    calls = []
    store = {}

    def fake_save(**kw):
        calls.append(kw)
        store[kw["stem"]] = {
            "verified":        kw["verified"],
            "verified_at":     kw["verified_at"],
            "verified_by":     kw["verified_by"],
            "field_overrides": kw["field_overrides"],
        }
        return True

    monkeypatch.setattr(sb, "is_enabled", lambda: True)
    monkeypatch.setattr(sb, "save_review", fake_save)
    # The DB is the source of truth when Supabase is on, so the read path must
    # return what was written — otherwise the idempotency check can't see it.
    monkeypatch.setattr(sb, "get_review", lambda stem: store.get(stem, {}))
    monkeypatch.setattr(sb, "get_reviews_bulk",
                        lambda stems: {s: store[s] for s in stems if s in store})

    token = login(client)
    body = {"verified": True, "approvals": {LOW_CONF_FIELD: True}}
    client.post(f"/api/review/{STEM}", json=body, headers=auth_headers(token))
    client.post(f"/api/review/{STEM}", json=body, headers=auth_headers(token))
    assert len(calls) == 1, "re-verify must not write a second audit trail"


# ── 10. chunk_index powers search and must not drop grounded chunks ─────────

def test_chunk_index_falls_back_to_inline_grounding(env):
    """A top-level grounding record with no box must not suppress the chunk's own."""
    import json as _json
    path = env["extract"] / "docs" / "shipping_label" / f"{STEM}.json"
    data = _json.loads(path.read_text())
    # Grounding entry exists but carries no box; the chunk itself has one.
    data["grounding"]["chunk-hi"] = {"confidence": 0.99}
    data["chunks"][0]["grounding"] = {
        "box": {"top": 0.1, "left": 0.1, "right": 0.4, "bottom": 0.2}, "page": 2,
    }
    path.write_text(_json.dumps(data))

    state = core.compute_doc_state(STEM, {})
    hit = next((c for c in state["chunk_index"] if c["id"] == "chunk-hi"), None)
    assert hit is not None, "chunk was dropped from the search index"
    assert hit["page"] == 2          # page came from the inline grounding, not 0
    assert hit["box"]["right"] == 0.4


def test_chunk_index_covers_all_grounded_chunks(env):
    state = core.compute_doc_state(STEM, {})
    ids = {c["id"] for c in state["chunk_index"]}
    assert ids == {"chunk-hi", "chunk-lo"}, "search must see every grounded chunk"
