"""Atomic persistence, corrupt-file quarantine, config validation, audit serialization."""

import json

import pytest

import core
import supabase_client as sb


def test_atomic_write_leaves_no_partial_file(tmp_path):
    p = tmp_path / "x.json"
    core.atomic_write_json(p, {"a": 1})
    assert json.loads(p.read_text()) == {"a": 1}
    # No temp files left behind.
    assert [f.name for f in tmp_path.iterdir()] == ["x.json"]


def test_atomic_write_does_not_clobber_on_failure(tmp_path, monkeypatch):
    p = tmp_path / "x.json"
    core.atomic_write_json(p, {"good": True})

    class Unserializable:
        pass

    with pytest.raises(TypeError):
        core.atomic_write_json(p, {"bad": Unserializable()})

    # Original survives intact; no stray .tmp files.
    assert json.loads(p.read_text()) == {"good": True}
    assert [f.name for f in tmp_path.iterdir()] == ["x.json"]


def test_corrupt_json_is_quarantined_not_silently_emptied(tmp_path, monkeypatch):
    p = tmp_path / "settings.json"
    p.write_text("{ this is not json")
    monkeypatch.setattr(core, "SETTINGS_FILE", p)

    settings = core.load_settings()
    assert "thresholds" in settings                  # usable defaults returned
    assert not p.exists()                            # bad file moved aside
    quarantined = list(tmp_path.glob("settings.json.corrupt.*"))
    assert len(quarantined) == 1                     # and preserved for diagnosis


def test_validate_config_rejects_field_not_in_extraction_schema(monkeypatch):
    import schema
    monkeypatch.setitem(schema.FIELD_LABELS["shipping_label"], "bogus_field", "Bogus")
    with pytest.raises(RuntimeError, match="bogus_field"):
        core.validate_config()


def test_validate_config_passes_on_real_schema():
    core.validate_config()      # must not raise


def test_password_policy_boundary():
    assert core.validate_password("x" * (core.PASSWORD_MIN_LEN - 1)) is not None
    assert core.validate_password("x" * core.PASSWORD_MIN_LEN) is None
    assert core.validate_password(None) is not None


def test_audit_value_serializes_structures_consistently():
    """Array overrides must not be handed raw to a TEXT column."""
    assert sb._audit_value(None) is None
    assert sb._audit_value("plain") == "plain"

    out = sb._audit_value([{"b": 2, "a": 1}])
    assert isinstance(out, str)
    assert json.loads(out) == [{"a": 1, "b": 2}]

    # Stable across key ordering, so an unchanged value never looks changed.
    assert sb._audit_value({"a": 1, "b": 2}) == sb._audit_value({"b": 2, "a": 1})


def test_doc_type_and_group_helpers():
    assert core.doc_type_for_stem("ups1_shipping_label") == "shipping_label"
    assert core.doc_type_for_stem("nope") is None
    assert core.sanitize_stem("../../etc/passwd") == "etcpasswd"
