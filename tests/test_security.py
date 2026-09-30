"""Web app security and account features, tested through the real HTTP layer with a throwaway database."""

import json
import time

import pytest
from fastapi.testclient import TestClient

from app import db, longshort, security, trendbot, users
from app.config import Settings
from app.logs import audit, redact
from app.main import create_app

ADMIN_PW = "correct horse battery 42"
USER_PW = "another long password 7"


@pytest.fixture
def app_and_conn(tmp_path):
    cfg = Settings(db_path=tmp_path / "t.db", secret_key_path=tmp_path / "secret.key", log_dir=tmp_path / "logs",
                   history_path=tmp_path / "history.db")
    app = create_app(cfg, background=False)
    conn = app.state.ctx.conn
    users.create(conn, "admin1", ADMIN_PW, role="admin")
    users.create(conn, "alice", USER_PW, role="user")
    return app, conn


def client_for(app, username=None, password=None) -> TestClient:
    c = TestClient(app, raise_server_exceptions=False)
    if username:
        r = c.post("/api/auth/login", json={"username": username, "password": password})
        assert r.status_code == 200, r.text
        c.headers["X-CSRF-Token"] = c.cookies.get("cs_csrf")
    return c


# --- A01 access control --------------------------------------------------------------

def test_everything_requires_login(app_and_conn):
    app, _ = app_and_conn
    c = client_for(app)
    for path in ("/api/longshort", "/api/account", "/api/bot", "/api/settings", "/api/admin/users", "/api/status"):
        assert c.get(path).status_code == 401, path
    r = c.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login.html"
    assert c.get("/login.html").status_code == 200


def test_admin_pages_are_admin_only(app_and_conn):
    app, _ = app_and_conn
    alice = client_for(app, "alice", USER_PW)
    for path in ("/api/admin/users", "/api/admin/audit", "/api/admin/status", "/api/admin/sessions", "/api/admin/settings"):
        assert alice.get(path).status_code == 403, path
    admin = client_for(app, "admin1", ADMIN_PW)
    assert admin.get("/api/admin/users").status_code == 200


def test_users_cannot_touch_each_others_data(app_and_conn):
    app, conn = app_and_conn
    admin_id = users.by_name(conn, "admin1")["id"]
    trendbot.start(conn, admin_id, 5000, now=0, btc_price=100)
    users.set_secret(conn, admin_id, "okx_api_key", "admins-key-1234")
    with conn:
        conn.execute("INSERT INTO user_holdings VALUES (?, 'SOL', 'okx', 1, 0, 100, 100, 90, 1, 0, NULL, NULL, NULL, NULL, 0)",
                     (admin_id,))
    alice = client_for(app, "alice", USER_PW)
    assert alice.get("/api/bot").json()["account"] is None  # the admin's bot isn't hers
    assert alice.get("/api/account").json() == {"configured": False}
    assert "1234" not in alice.get("/api/settings").text
    assert alice.post("/api/bot/reset").json()["account"] is None
    assert trendbot.account(conn, admin_id) is not None  # resetting her (empty) bot left the admin's alone


# --- A07 authentication --------------------------------------------------------------------

def test_wrong_password_and_unknown_user_look_the_same(app_and_conn):
    app, _ = app_and_conn
    c = client_for(app)
    a = c.post("/api/auth/login", json={"username": "alice", "password": "nope nope nope"})
    b = c.post("/api/auth/login", json={"username": "nobody", "password": "nope nope nope"})
    assert a.status_code == b.status_code == 401 and a.json() == b.json()


def test_lockout_after_five_failures(app_and_conn):
    app, conn = app_and_conn
    c = client_for(app)
    for _ in range(4):
        assert c.post("/api/auth/login", json={"username": "alice", "password": "wrong password!"}).status_code == 401
    assert c.post("/api/auth/login", json={"username": "alice", "password": "wrong password!"}).status_code == 429
    # Even the right password is refused while locked.
    assert c.post("/api/auth/login", json={"username": "alice", "password": USER_PW}).status_code == 429
    with conn:
        conn.execute("UPDATE users SET locked_until = ? WHERE username = 'alice'", (int(time.time()) - 1,))
    assert c.post("/api/auth/login", json={"username": "alice", "password": USER_PW}).status_code == 200
    levels = {r[0] for r in conn.execute("SELECT level FROM audit_log WHERE action = 'auth.login_failed'")}
    assert "warning" in levels  # the lockout itself is flagged


def test_login_rate_limit_per_ip(app_and_conn):
    app, _ = app_and_conn
    c = client_for(app)
    codes = [c.post("/api/auth/login", json={"username": f"x{i}", "password": "whatever pw"}).status_code for i in range(12)]
    assert codes[:10] == [401] * 10 and 429 in codes[10:]


def test_session_cookie_flags_and_idle_timeout(app_and_conn):
    app, conn = app_and_conn
    c = TestClient(app)
    r = c.post("/api/auth/login", json={"username": "alice", "password": USER_PW})
    cookies = r.headers.get_list("set-cookie")
    session_cookie = next(x for x in cookies if x.startswith("cs_session="))
    assert "HttpOnly" in session_cookie and "SameSite=strict" in session_cookie
    csrf_cookie = next(x for x in cookies if x.startswith("cs_csrf="))
    assert "HttpOnly" not in csrf_cookie
    assert c.get("/api/auth/me").status_code == 200
    with conn:
        conn.execute("UPDATE sessions SET last_seen = last_seen - ?", (users.SESSION_IDLE_S + 5,))
    assert c.get("/api/auth/me").status_code == 401  # idle too long


def test_logout_ends_the_session_on_the_server(app_and_conn):
    app, conn = app_and_conn
    c = client_for(app, "alice", USER_PW)
    token = c.cookies.get("cs_session")
    assert c.post("/api/auth/logout").status_code == 200
    assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0
    stale = TestClient(app)
    stale.cookies.set("cs_session", token)
    assert stale.get("/api/auth/me").status_code == 401


def test_password_change_rules_and_other_sessions_end(app_and_conn):
    app, conn = app_and_conn
    first, second = client_for(app, "alice", USER_PW), client_for(app, "alice", USER_PW)
    assert first.post("/api/auth/password", json={"current": "wrong wrong", "new": "brand new password 1"}).status_code == 400
    assert first.post("/api/auth/password", json={"current": USER_PW, "new": "short"}).status_code == 400
    r = first.post("/api/auth/password", json={"current": USER_PW, "new": "brand new password 1"})
    assert r.status_code == 200 and r.json()["other_sessions_ended"] == 1
    assert first.get("/api/auth/me").status_code == 200 and second.get("/api/auth/me").status_code == 401
    assert client_for(app).post("/api/auth/login", json={"username": "alice", "password": USER_PW}).status_code == 401


def test_two_step_login(app_and_conn):
    app, _ = app_and_conn
    c = client_for(app, "alice", USER_PW)
    secret = c.post("/api/auth/2fa/setup").json()["secret"]
    assert c.post("/api/auth/2fa/enable", json={"code": "000000"}).status_code == 400
    assert c.post("/api/auth/2fa/enable", json={"code": security.totp_now(secret)}).status_code == 200
    fresh = client_for(app)
    r = fresh.post("/api/auth/login", json={"username": "alice", "password": USER_PW})
    assert r.status_code == 401 and r.json()["code_required"]
    r = fresh.post("/api/auth/login", json={"username": "alice", "password": USER_PW, "code": security.totp_now(secret)})
    assert r.status_code == 200


# --- A08 / A01: forged requests -----------------------------------------------------------------

def test_changes_need_the_csrf_token(app_and_conn):
    app, _ = app_and_conn
    c = client_for(app, "alice", USER_PW)
    token = c.headers.pop("X-CSRF-Token")
    body = {"fee_taker": 0.0015}
    assert c.put("/api/settings/prefs", json=body).status_code == 403
    assert c.put("/api/settings/prefs", json=body, headers={"X-CSRF-Token": "forged"}).status_code == 403
    assert c.put("/api/settings/prefs", json=body,
                 headers={"X-CSRF-Token": token, "Origin": "https://evil.example"}).status_code == 403
    r = c.put("/api/settings/prefs", json=body, headers={"X-CSRF-Token": token})
    assert r.status_code == 200 and r.json()["fees"]["taker"] == 0.0015


def test_own_https_name_counts_as_same_origin_behind_a_proxy(tmp_path):
    cfg = Settings(db_path=tmp_path / "t.db", secret_key_path=tmp_path / "secret.key", log_dir=tmp_path / "logs",
                   history_path=tmp_path / "history.db", allowed_hosts=("localhost", "bot.tail1234.ts.net"))
    app = create_app(cfg, background=False)
    users.create(app.state.ctx.conn, "admin1", ADMIN_PW, role="admin")
    c = client_for(app, "admin1", ADMIN_PW)
    body = {"fee_taker": 0.0015}
    # The proxy forwards to localhost:8000; the browser's address is the Tailscale name.
    assert c.put("/api/settings/prefs", json=body, headers={"Origin": "https://bot.tail1234.ts.net",
                                                            "Host": "localhost:8000"}).status_code == 200
    for bad in ("http://bot.tail1234.ts.net", "https://evil.example", "https://bot.tail1234.ts.net:4443"):
        assert c.put("/api/settings/prefs", json=body, headers={"Origin": bad, "Host": "localhost:8000"}).status_code == 403, bad


def test_unknown_host_is_rejected(app_and_conn):
    app, _ = app_and_conn
    assert TestClient(app).get("/login.html", headers={"host": "evil.example"}).status_code == 400


# --- A02 misconfiguration / A10 errors ---------------------------------------------------------------

def test_security_headers_and_no_api_docs(app_and_conn):
    app, _ = app_and_conn
    c = client_for(app)
    r = c.get("/login.html")
    h = r.headers
    assert "default-src 'self'" in h["content-security-policy"] and "frame-ancestors 'none'" in h["content-security-policy"]
    assert h["x-content-type-options"] == "nosniff" and h["x-frame-options"] == "DENY"
    assert h["referrer-policy"] == "no-referrer"
    assert c.get("/api/status").headers["cache-control"] == "no-store"
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert c.get(path).status_code == 404


def test_errors_dont_leak_details(app_and_conn, monkeypatch):
    app, _ = app_and_conn
    c = client_for(app, "alice", USER_PW)
    from app import routes_app

    def boom(*a, **k):
        raise RuntimeError("secret internal detail")
    monkeypatch.setattr(routes_app.trendbot, "value", boom)
    r = c.get("/api/bot")
    assert r.status_code == 500 and "secret internal detail" not in r.text and "reference" in r.json()["detail"]


def test_validation_errors_dont_echo_input(app_and_conn):
    app, _ = app_and_conn
    c = client_for(app)
    secret_value = "x" * 600
    r = c.post("/api/auth/login", json={"username": "alice", "password": secret_value})
    assert r.status_code == 422 and secret_value not in r.text


# --- A04 cryptography ---------------------------------------------------------------------------------

def test_passwords_are_argon2_hashes(app_and_conn):
    _, conn = app_and_conn
    stored = conn.execute("SELECT password_hash FROM users WHERE username = 'alice'").fetchone()[0]
    assert stored.startswith("$argon2id$") and USER_PW not in stored


def test_okx_key_is_encrypted_at_rest(app_and_conn, tmp_path):
    _, conn = app_and_conn
    uid = users.by_name(conn, "alice")["id"]
    users.set_secret(conn, uid, "okx_api_secret", "SUPERSECRETVALUE123")
    raw = conn.execute("SELECT value_enc FROM user_secrets WHERE user_id = ?", (uid,)).fetchone()[0]
    assert "SUPERSECRETVALUE123" not in raw
    assert users.get_secret(conn, uid, "okx_api_secret") == "SUPERSECRETVALUE123"
    other_box = security.SecretBox(tmp_path / "other.key")
    assert other_box.decrypt(raw) is None  # useless without the key file


def test_settings_never_return_the_key(app_and_conn):
    app, conn = app_and_conn
    uid = users.by_name(conn, "alice")["id"]
    for name, value in (("okx_api_key", "abcd-efgh-1234"), ("okx_api_secret", "S3CR3T"), ("okx_api_passphrase", "PASS")):
        users.set_secret(conn, uid, name, value)
    body = client_for(app, "alice", USER_PW).get("/api/settings").text
    assert "S3CR3T" not in body and "PASS\"" not in body and "abcd-efgh" not in body and "1234" in body


# --- A09 logging ---------------------------------------------------------------------------------------

def test_secrets_are_redacted_from_logs(app_and_conn):
    _, conn = app_and_conn
    assert "hunter2" not in redact('login password="hunter2" ok')
    assert "abc123" not in redact("api_key=abc123&x=1")
    audit(conn, "test", detail={"password": "hunter2", "secret": "zzz", "symbol": "BTC"})
    stored = conn.execute("SELECT detail FROM audit_log WHERE action = 'test'").fetchone()[0]
    assert "hunter2" not in stored and "zzz" not in stored and "BTC" in stored


def test_audit_trail_records_security_events(app_and_conn):
    app, conn = app_and_conn
    client_for(app).post("/api/auth/login", json={"username": "alice", "password": "bad password!"})
    c = client_for(app, "alice", USER_PW)
    c.put("/api/settings/prefs", json={"fee_taker": 0.0015})
    c.post("/api/auth/logout")
    actions = [r[0] for r in conn.execute("SELECT action FROM audit_log ORDER BY id")]
    assert actions[:4] == ["auth.login_failed", "auth.login", "settings.changed", "auth.logout"]


def test_many_failed_logins_raise_an_admin_alert(app_and_conn):
    app, conn = app_and_conn
    for i in range(10):
        audit(conn, "auth.login_failed", username=f"u{i}")
    client_for(app).post("/api/auth/login", json={"username": "alice", "password": "bad password!"})
    assert conn.execute("SELECT COUNT(*) FROM audit_log WHERE level = 'alert'").fetchone()[0] == 1


# --- admin rules ---------------------------------------------------------------------------------------

def test_admin_cannot_remove_the_last_admin_or_itself(app_and_conn):
    app, conn = app_and_conn
    admin = client_for(app, "admin1", ADMIN_PW)
    me = users.by_name(conn, "admin1")["id"]
    assert admin.patch(f"/api/admin/users/{me}", json={"role": "user"}).status_code == 400
    assert admin.patch(f"/api/admin/users/{me}", json={"disabled": True}).status_code == 400


def test_disabling_a_user_logs_them_out(app_and_conn):
    app, conn = app_and_conn
    alice = client_for(app, "alice", USER_PW)
    admin = client_for(app, "admin1", ADMIN_PW)
    aid = users.by_name(conn, "alice")["id"]
    assert admin.patch(f"/api/admin/users/{aid}", json={"disabled": True}).status_code == 200
    assert alice.get("/api/auth/me").status_code == 401
    assert client_for(app).post("/api/auth/login", json={"username": "alice", "password": USER_PW}).status_code == 401


def test_admin_creates_users_with_password_rules(app_and_conn):
    app, _ = app_and_conn
    admin = client_for(app, "admin1", ADMIN_PW)
    assert admin.post("/api/admin/users", json={"username": "bob", "password": "short"}).status_code == 400
    assert admin.post("/api/admin/users", json={"username": "bob", "password": "a proper password 9"}).status_code == 200
    assert client_for(app, "bob", "a proper password 9").get("/api/auth/me").json()["user"]["role"] == "user"


def test_admin_settings_are_validated(app_and_conn):
    app, _ = app_and_conn
    admin = client_for(app, "admin1", ADMIN_PW)
    assert admin.put("/api/admin/settings", json={"values": {"refresh_minutes": 5}}).status_code == 400  # removed
    assert admin.put("/api/admin/settings", json={"values": {"default_demo_balance": 1}}).status_code == 400
    assert admin.put("/api/admin/settings", json={"values": {"default_demo_balance": 5000}}).status_code == 200


def test_admin_status_counts(app_and_conn):
    app, _ = app_and_conn
    body = client_for(app, "admin1", ADMIN_PW).get("/api/admin/status").json()
    assert body["stream"]["connected"] is False and body["longshort"]["state"] == "starting"
    assert body["counts"]["longshort_positions"] == 0 and body["counts"]["trend_bots"] == 0


def test_swing_copies_are_gone(app_and_conn):
    app, _ = app_and_conn
    c = client_for(app, "alice", USER_PW)
    for path in ("/api/copies", "/api/demo", "/api/portfolio", "/api/performance", "/api/alerts"):
        assert c.get(path).status_code in (404, 405), path


def test_trend_bot_per_user_demo_account(app_and_conn):
    app, conn = app_and_conn
    alice, admin = client_for(app, "alice", USER_PW), client_for(app, "admin1", ADMIN_PW)
    assert alice.get("/api/bot").json()["account"] is None
    assert alice.post("/api/bot/start", json={"balance": 50}).status_code == 422
    r = alice.post("/api/bot/start", json={"balance": 5000})
    assert r.status_code == 200 and r.json()["account"]["start_balance"] == 5000
    assert app.state.ctx.pipeline.bot_wake.is_set()  # the first check runs right away
    assert alice.post("/api/bot/start", json={"balance": 5000}).status_code == 409
    assert admin.get("/api/bot").json()["account"] is None  # each user has their own
    assert alice.put("/api/bot/enabled", json={"enabled": False}).json()["account"]["enabled"] is False
    assert admin.put("/api/bot/enabled", json={"enabled": True}).status_code == 404
    assert alice.post("/api/bot/reset").json()["account"] is None
    actions = {r[0] for r in conn.execute("SELECT action FROM audit_log WHERE action LIKE 'bot.%'")}
    assert actions == {"bot.started", "bot.paused", "bot.reset"}


# --- long/short paper test --------------------------------------------------------------------------------

def test_longshort_page_and_reset_is_admin_only(app_and_conn, monkeypatch):
    app, conn = app_and_conn
    pipe = app.state.ctx.pipeline

    async def no_network(client, coins):
        return {"BTC": 60_000.0, "SOL": 110.0}
    monkeypatch.setattr(pipe, "ls_live_prices", no_network)
    longshort.start(conn, 10_000, now=0, btc_price=50_000)
    with conn:
        conn.execute("INSERT INTO ls_positions VALUES ('SOL', 10, 100, 0)")
        conn.execute("UPDATE ls_account SET cash = 9000")
    alice, admin = client_for(app, "alice", USER_PW), client_for(app, "admin1", ADMIN_PW)
    body = alice.get("/api/longshort").json()
    assert body["account"]["equity"] == pytest.approx(9000 + 10 * 110) and body["is_admin"] is False
    assert body["account"]["btc_return"] == pytest.approx(0.2) and body["rules"]["top_coins"] == 25
    assert alice.post("/api/longshort/reset").status_code == 403
    assert longshort.account(conn) is not None
    r = admin.post("/api/longshort/reset")
    assert r.status_code == 200 and r.json()["account"] is None and pipe.ls_wake.is_set()
    detail = json.loads(conn.execute("SELECT detail FROM audit_log WHERE action = 'ls.reset'").fetchone()[0])
    assert detail["previous"]["equity"] == pytest.approx(10_100)


# --- fees ------------------------------------------------------------------------------------------------------

def test_user_fees_are_used_for_costs(app_and_conn):
    app, conn = app_and_conn
    from app.pipeline import user_fee
    c = client_for(app, "alice", USER_PW)
    alice = users.by_name(conn, "alice")["id"]
    cfg = app.state.ctx.settings
    assert user_fee(conn, cfg, alice) == (cfg.fee_rate, "default")
    users.set_setting(conn, alice, "account_status", {"ok": True, "fee_rate": 0.0035})
    assert user_fee(conn, cfg, alice) == (0.0035, "okx")
    assert c.put("/api/settings/prefs", json={"fee_taker": 0.2}).status_code == 422  # 20% is not a fee
    assert c.put("/api/settings/prefs", json={"fee_taker": 0.002, "fee_maker": 0.001}).status_code == 200
    assert user_fee(conn, cfg, alice) == (0.002, "yours")
    assert c.get("/api/settings").json()["fees"]["taker"] == 0.002


def test_temporary_password_must_be_changed_first(app_and_conn):
    app, conn = app_and_conn
    users.create(conn, "firstadmin", "short1!", role="admin", temporary=True)
    with pytest.raises(users.UserError):
        users.create(conn, "someone", "short1!")  # without temporary the rules apply
    c = client_for(app, "firstadmin", "short1!")
    assert c.get("/api/status").json()["user"]["must_change_password"] is True
    assert c.get("/api/longshort").status_code == 403 and c.get("/api/admin/users").status_code == 403
    assert c.post("/api/auth/password", json={"current": "short1!", "new": "a much better password 1"}).status_code == 200
    assert c.get("/api/admin/users").status_code == 200
