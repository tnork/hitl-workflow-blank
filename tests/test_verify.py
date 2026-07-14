"""Server-authoritative verification, persistence failure, file movement, CSRF."""

import core
import supabase_client as sb
import web_app
from conftest import login, auth_headers, STEM, TOTAL_FIELDS, LOW_CONF_FIELD


def test_csrf_required_on_unsafe_methods(client, env):
    token = login(client)
    # Right session, no token -> refused.
    r = client.post(f"/api/review/{STEM}", json={"verified": False},
                    headers={"Origin": "http://localhost"})
    assert r.status_code == 403

    # Cross-origin, valid token -> still refused.
    r = client.post(f"/api/review/{STEM}", json={"verified": False},
                    headers={"Origin": "https://evil.example", "X-CSRF-Token": token})
    assert r.status_code == 403


def test_cannot_verify_without_approving_low_confidence_field(client, env):
    """The core forgery case: claim verified=true, approve nothing."""
    token = login(client)
    r = client.post(f"/api/review/{STEM}",
                    json={"verified": True, "field_overrides": {}, "approvals": {}},
                    headers=auth_headers(token))
    assert r.status_code == 409
    missing = [m["key"] for m in r.get_json()["missing"]]
    # tracking_number is grounded at 0.99 (auto-ok); ship_to_name is at 0.20.
    assert missing == ["ship_to_name"]

    # And the document must NOT have moved.
    assert (env["inbox"] / f"{STEM}.pdf").exists()
    assert not (env["outbox"] / f"{STEM}.pdf").exists()


def test_verify_succeeds_when_low_confidence_field_is_approved(client, env):
    token = login(client)
    r = client.post(f"/api/review/{STEM}",
                    json={"verified": True, "field_overrides": {},
                          "approvals": {"ship_to_name": True}},
                    headers=auth_headers(token))
    assert r.status_code == 200, r.get_json()
    body = r.get_json()
    assert body["ok"] and body["verified"] and body["moved"]

    # Moved to the outbox only after a confirmed write.
    assert not (env["inbox"] / f"{STEM}.pdf").exists()
    assert (env["outbox"] / f"{STEM}.pdf").exists()


def test_reviewer_and_timestamp_come_from_the_server(client, env):
    """A client-supplied reviewer/timestamp must be ignored, not trusted."""
    token = login(client)
    r = client.post(f"/api/review/{STEM}",
                    json={"verified": True, "field_overrides": {},
                          "approvals": {"ship_to_name": True},
                          "reviewer": "someone-else",
                          "verified_at": "1999-01-01T00:00:00Z"},
                    headers=auth_headers(token))
    assert r.status_code == 200
    body = r.get_json()
    assert body["verified_by"] == "admin"
    assert not body["verified_at"].startswith("1999")


def test_unknown_override_key_is_rejected(client, env):
    token = login(client)
    r = client.post(f"/api/review/{STEM}",
                    json={"verified": False, "field_overrides": {"evil_key": "x"}},
                    headers=auth_headers(token))
    assert r.status_code == 400
    assert "evil_key" in r.get_json()["error"]


def test_unknown_approval_key_is_rejected(client, env):
    token = login(client)
    r = client.post(f"/api/review/{STEM}",
                    json={"verified": True, "approvals": {"not_a_field": True}},
                    headers=auth_headers(token))
    assert r.status_code == 400


def test_persistence_failure_returns_error_and_does_not_move_file(client, env, monkeypatch):
    """A failed DB write must NOT report success and must NOT move the document."""
    monkeypatch.setattr(sb, "is_enabled", lambda: True)
    monkeypatch.setattr(sb, "save_review", lambda **kw: False)   # simulate DB failure

    token = login(client)
    r = client.post(f"/api/review/{STEM}",
                    json={"verified": True, "field_overrides": {},
                          "approvals": {"ship_to_name": True}},
                    headers=auth_headers(token))
    assert r.status_code == 503
    assert (env["inbox"] / f"{STEM}.pdf").exists()
    assert not (env["outbox"] / f"{STEM}.pdf").exists()
    # Nothing may be cached as verified either.
    assert not web_app._reviews.get(STEM, {}).get("verified")


def test_override_is_persisted_and_counts_as_approval(client, env):
    token = login(client)
    r = client.post(f"/api/review/{STEM}",
                    json={"verified": True,
                          "field_overrides": {"ship_to_name": "JOHN SMITH"},
                          "approvals": {}},          # override alone satisfies the field
                    headers=auth_headers(token))
    assert r.status_code == 200, r.get_json()
    assert web_app._reviews[STEM]["field_overrides"]["ship_to_name"] == "JOHN SMITH"


def test_reset_restores_documents_to_the_inbox(client, env):
    token = login(client)
    client.post(f"/api/review/{STEM}",
                json={"verified": True, "field_overrides": {},
                      "approvals": {"ship_to_name": True}},
                headers=auth_headers(token))
    assert (env["outbox"] / f"{STEM}.pdf").exists()

    r = client.post("/api/reset", headers=auth_headers(token))
    assert r.status_code == 200
    assert r.get_json()["restored"] == 1
    assert (env["inbox"] / f"{STEM}.pdf").exists()
    assert not (env["outbox"] / f"{STEM}.pdf").exists()
    assert web_app._reviews == {}


def test_board_and_panel_agree_on_readiness(client, env):
    """/api/docs must not call a doc ready on weaker criteria than /api/doc."""
    token = login(client)
    board = {d["stem"]: d for d in client.get("/api/docs").get_json()}[STEM]
    panel = client.get(f"/api/doc/{STEM}").get_json()

    # Only the high-confidence field is auto-approvable in both surfaces.
    assert board["total"] == TOTAL_FIELDS
    assert board["grounded"] == TOTAL_FIELDS - 1     # only ship_to_name is low-confidence
    assert panel["field_high_conf"]["tracking_number"] is True
    assert panel["field_high_conf"]["ship_to_name"] is False


def test_threshold_change_moves_a_field_out_of_auto_approval(client, env):
    token = login(client)
    # Raise the bar above the good chunk's 0.99 -> nothing is auto-approvable.
    r = client.post("/api/settings", json={"doc_type": "shipping_label", "threshold": 0.995},
                    headers=auth_headers(token))
    assert r.status_code == 200

    panel = client.get(f"/api/doc/{STEM}").get_json()
    assert panel["field_high_conf"]["tracking_number"] is False

    r = client.post(f"/api/review/{STEM}",
                    json={"verified": True, "approvals": {LOW_CONF_FIELD: True}},
                    headers=auth_headers(token))
    assert r.status_code == 409
    missing = [m["key"] for m in r.get_json()["missing"]]
    # Nothing is auto-approvable any more, so every field except the one we
    # explicitly approved now demands a human.
    assert "tracking_number" in missing
    assert LOW_CONF_FIELD not in missing
    assert len(missing) == TOTAL_FIELDS - 1
