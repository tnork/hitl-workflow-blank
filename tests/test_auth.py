"""Authentication, session revalidation, throttling, password policy."""

import core
import web_app
from conftest import login, auth_headers


def test_health_is_public(client):
    assert client.get("/health").status_code == 200


def test_api_requires_login(client):
    r = client.get("/api/docs")
    assert r.status_code == 401


def test_no_default_admin_password(env):
    """admin/admin must not exist. The bootstrap uses $ADMIN_PASSWORD or a random one."""
    from werkzeug.security import check_password_hash
    users = core.load_users()
    admin = next(u for u in users.values() if u["username"] == "admin")
    assert not check_password_hash(admin["password_hash"], "admin")
    assert check_password_hash(admin["password_hash"], "correct-horse-battery")


def test_bootstrap_password_is_random_without_env(env, monkeypatch):
    from werkzeug.security import check_password_hash
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    core.USERS_FILE.unlink(missing_ok=True)
    users = core.load_users()
    admin = next(u for u in users.values() if u["username"] == "admin")
    assert not check_password_hash(admin["password_hash"], "admin")


def test_login_succeeds_and_rejects(client):
    r = client.post("/login", data={"username": "admin", "password": "correct-horse-battery"},
                    headers={"Origin": "http://localhost"})
    assert r.status_code == 302 and "/login" not in r.headers["Location"]

    r = client.post("/login", data={"username": "admin", "password": "wrong"},
                    headers={"Origin": "http://localhost"})
    assert "error=invalid" in r.headers["Location"]


def test_login_throttled_after_repeated_failures(client):
    for _ in range(web_app._LOGIN_MAX_ATTEMPTS):
        client.post("/login", data={"username": "admin", "password": "wrong"},
                    headers={"Origin": "http://localhost"})
    # Even the CORRECT password is now refused — the account is locked out.
    r = client.post("/login", data={"username": "admin", "password": "correct-horse-battery"},
                    headers={"Origin": "http://localhost"})
    assert "error=locked" in r.headers["Location"]


def test_password_reset_revokes_existing_sessions(client, env):
    """A live cookie must stop working once its user's password is reset."""
    token = login(client)
    assert client.get("/api/docs").status_code == 200

    users = core.load_users()
    uid = next(k for k, u in users.items() if u["username"] == "admin")
    users[uid]["session_version"] += 1
    core.save_users(users)

    # Same cookie, bumped version -> rejected.
    assert client.get("/api/docs").status_code == 401


def test_deleted_user_session_is_rejected(client, env):
    token = login(client)
    assert client.get("/api/docs").status_code == 200
    core.save_users({})          # user is gone
    assert client.get("/api/docs").status_code == 401


def test_password_policy_is_uniform(client, env):
    """Create, admin-reset, and self-change must all enforce the same minimum."""
    token = login(client)
    short = "x" * (core.PASSWORD_MIN_LEN - 1)
    ok    = "x" * core.PASSWORD_MIN_LEN

    r = client.post("/api/users", json={"username": "bob", "password": short, "role": "user"},
                    headers=auth_headers(token))
    assert r.status_code == 400

    r = client.post("/api/change-password",
                    json={"current_password": "correct-horse-battery", "new_password": short},
                    headers=auth_headers(token))
    assert r.status_code == 400

    r = client.post("/api/users", json={"username": "bob", "password": ok, "role": "user"},
                    headers=auth_headers(token))
    assert r.status_code == 201

    uid = r.get_json()["uid"]
    r = client.post(f"/api/users/{uid}/reset-password", json={"password": short},
                    headers=auth_headers(token))
    assert r.status_code == 400


def test_non_admin_cannot_reach_admin_routes(client, env):
    token = login(client)
    client.post("/api/users",
                json={"username": "bob", "password": "a" * core.PASSWORD_MIN_LEN, "role": "user"},
                headers=auth_headers(token))
    client.post("/logout", headers=auth_headers(token))

    token = login(client, "bob", "a" * core.PASSWORD_MIN_LEN)
    assert client.get("/api/users").status_code == 403
    assert client.post("/api/reset", headers=auth_headers(token)).status_code == 403
