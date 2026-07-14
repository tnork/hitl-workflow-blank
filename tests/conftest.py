"""
Shared pytest fixtures.

Every test runs against a throwaway directory tree — no test may read or write
the real users.json, settings.json, inbox, or extract_results.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import core          # noqa: E402
import supabase_client as sb   # noqa: E402
import web_app       # noqa: E402


STEM = "ups1_shipping_label"

# Two grounding chunks: one high-confidence, one below any sane threshold.
GROUNDING = {
    "chunk-hi": {"box": {"top": 0.1, "left": 0.1, "right": 0.4, "bottom": 0.2},
                 "page": 0, "confidence": 0.99, "type": "text"},
    "chunk-lo": {"box": {"top": 0.5, "left": 0.1, "right": 0.4, "bottom": 0.6},
                 "page": 0, "confidence": 0.20, "type": "text"},
}
CHUNKS = [
    {"id": "chunk-hi", "markdown": "TRACKING #: 1Z 999 AA1 01 2345 6784"},
    {"id": "chunk-lo", "markdown": "some smudged text"},
]

# Cover EVERY field the schema declares — the server requires each one to be
# either auto-approvable or explicitly approved, so a partial fixture would make
# verification impossible for reasons unrelated to what a test is asserting.
#
# All fields are grounded at 0.99 EXCEPT ship_to_name (0.20), which therefore
# always needs a human approval. That single asymmetry is what the verification
# tests lean on.
import schema as _schema

_FIELD_KEYS = list(_schema.FIELD_LABELS["shipping_label"].keys())
LOW_CONF_FIELD = "ship_to_name"
assert LOW_CONF_FIELD in _FIELD_KEYS

FIELDS = {k: f"value-{k}" for k in _FIELD_KEYS}
FIELDS["tracking_number"] = "1Z 999 AA1 01 2345 6784"
FIELDS[LOW_CONF_FIELD] = "JANE DOE"

EX_META = {
    k: {"references": ["chunk-lo" if k == LOW_CONF_FIELD else "chunk-hi"], "value": FIELDS[k]}
    for k in _FIELD_KEYS
}
TOTAL_FIELDS = len(_FIELD_KEYS)


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Point every path in the app at a temp tree and reset in-memory state."""
    inbox   = tmp_path / "docs_inbox"
    outbox  = tmp_path / "docs_outbox"
    parse   = tmp_path / "parse_results"
    extract = tmp_path / "extract_results"
    for d in (inbox, outbox, parse / "docs", extract / "docs" / "shipping_label"):
        d.mkdir(parents=True, exist_ok=True)

    (extract / "docs" / "shipping_label" / f"{STEM}.json").write_text(json.dumps({
        "source_file": f"{STEM}.txt",
        "document_type": "shipping_label",
        "extracted_fields": FIELDS,
        "extraction_metadata": EX_META,
        "chunks": CHUNKS,
        "grounding": GROUNDING,
    }))
    (inbox / f"{STEM}.pdf").write_bytes(b"%PDF-1.4 fake\n")

    # core resolves these at call time, so patching the module attribute is enough.
    monkeypatch.setattr(core, "USERS_FILE", tmp_path / "users.json")
    monkeypatch.setattr(core, "SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setattr(core, "PARSE_RESULTS", parse)
    monkeypatch.setattr(core, "EXTRACT_RESULTS", extract)

    # web_app bound these names at import, so they must be patched separately.
    monkeypatch.setattr(web_app, "DOCS_DIR", inbox)
    monkeypatch.setattr(web_app, "DOCS_OUTBOX_DIR", outbox)
    monkeypatch.setattr(web_app, "EXTRACT_RESULTS", extract)

    monkeypatch.setenv("ADMIN_PASSWORD", "correct-horse-battery")
    monkeypatch.setattr(sb, "is_enabled", lambda: False)
    monkeypatch.setattr(sb, "upsert_document", lambda **kw: True)
    monkeypatch.setattr(sb, "lazy_upload", lambda *a, **kw: True)
    monkeypatch.setattr(sb, "clear_all_reviews", lambda: True)

    web_app._reviews.clear()
    web_app._login_failures.clear()

    return {
        "tmp": tmp_path, "inbox": inbox, "outbox": outbox,
        "extract": extract, "stem": STEM,
    }


@pytest.fixture
def app(env):
    a = web_app.make_flask_app()
    a.config.update(TESTING=True, SERVER_NAME="localhost")
    return a


@pytest.fixture
def client(app):
    return app.test_client()


def login(client, username="admin", password="correct-horse-battery"):
    """Log in and return the CSRF token for subsequent unsafe requests."""
    r = client.post("/login",
                    data={"username": username, "password": password},
                    headers={"Origin": "http://localhost"})
    assert r.status_code == 302, f"login failed: {r.status_code}"
    with client.session_transaction() as sess:
        # The token is minted lazily by the context processor; force it now.
        import secrets
        tok = sess.get("csrf_token")
        if not tok:
            tok = secrets.token_urlsafe(32)
            sess["csrf_token"] = tok
    return tok


def auth_headers(token):
    return {"Origin": "http://localhost", "X-CSRF-Token": token}
