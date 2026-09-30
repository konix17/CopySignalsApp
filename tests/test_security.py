"""Web app security and account features, tested through the real HTTP layer with a throwaway database."""

import json
import time

import pytest
from fastapi.testclient import TestClient

from app import autotrade, db, portfolio, security, users
from app.config import Settings
from app.logs import audit, redact
from app.main import create_app
from app.models import Check, Pick

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
    for path in ("/api/copies", "/api/portfolio", "/api/demo", "/api/settings", "/api/admin/users", "/api/status"):
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
    pid = portfolio.open_position(conn, user_id=admin_id, market_key="perp:BTC", symbol="BTC", entry_price=100,
                                  size_usd=50, pick=None, now=0)
    portfolio.raise_alert(conn, conn.execute("SELECT * FROM my_positions WHERE id = ?", (pid,)).fetchone(),
                          "x", "SELL", "test", 0)
    alice = client_for(app, "alice", USER_PW)
    assert alice.post(f"/api/positions/{pid}/close", json={}).status_code == 404
    assert alice.delete(f"/api/positions/{pid}").status_code == 404
    aid = conn.execute("SELECT id FROM alerts").fetchone()[0]
    assert alice.post(f"/api/alerts/{aid}/seen").status_code == 404
    assert alice.get("/api/portfolio").json()["positions"] == []
    assert conn.execute("SELECT status FROM my_positions WHERE id = ?", (pid,)).fetchone()[0] == "open"


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
    assert c.put("/api/settings/prefs", json={"demo_mode": True}).status_code == 403
    assert c.put("/api/settings/prefs", json={"demo_mode": True}, headers={"X-CSRF-Token": "forged"}).status_code == 403
    assert c.put("/api/settings/prefs", json={"demo_mode": True},
                 headers={"X-CSRF-Token": token, "Origin": "https://evil.example"}).status_code == 403
    r = c.put("/api/settings/prefs", json={"demo_mode": True}, headers={"X-CSRF-Token": token})
    assert r.status_code == 200 and r.json()["demo_mode"] is True


def test_own_https_name_counts_as_same_origin_behind_a_proxy(tmp_path):
    cfg = Settings(db_path=tmp_path / "t.db", secret_key_path=tmp_path / "secret.key", log_dir=tmp_path / "logs",
                   history_path=tmp_path / "history.db", allowed_hosts=("localhost", "bot.tail1234.ts.net"))
    app = create_app(cfg, background=False)
    users.create(app.state.ctx.conn, "admin1", ADMIN_PW, role="admin")
    c = client_for(app, "admin1", ADMIN_PW)
    body = {"demo_mode": True}
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
    monkeypatch.setattr(routes_app.tracker, "performance", boom)
    r = c.get("/api/performance")
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
    c.put("/api/settings/prefs", json={"bankroll": 500})
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
    assert admin.put("/api/admin/settings", json={"values": {"refresh_minutes": 1}}).status_code == 400
    assert admin.put("/api/admin/settings", json={"values": {"default_demo_balance": 5000}}).status_code == 200


def test_admin_status_counts_copies(app_and_conn):
    app, _ = app_and_conn
    body = client_for(app, "admin1", ADMIN_PW).get("/api/admin/status").json()
    assert body["stream"]["connected"] is False
    assert body["counts"]["tracked_copies_open"] == 0 and body["counts"]["tracked_copies_closed"] == 0
    assert client_for(app, "admin1", ADMIN_PW).get("/api/admin/lab").status_code in (404, 405)  # the lab was removed


def test_copies_page(app_and_conn):
    app, _ = app_and_conn
    body = client_for(app, "alice", USER_PW).get("/api/copies").json()
    assert body["copies"] == [] and "risk_on" in body["regime"] and body["bankroll"]["amount"] > 0
    assert client_for(app, "alice", USER_PW).get("/api/movers").status_code in (404, 405)  # rising now was removed


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


# --- demo mode and automatic demo trading -----------------------------------------------------------------

def _pick(symbol="SOL", size=100.0, price=100.0, score=1.0):
    """A swing copy."""
    return Pick(market_key=f"perp:{symbol}", symbol=symbol, pair=f"{symbol}-USDT", strength="Copy", score=score,
                checks=[Check("x", True, "y")], price=price, stop_price=price * 0.75, target_price=price * 2,
                stop_pct=-0.25, target_pct=1.0, cost_pct=0.007, hold_days=30, hold_basis="estimated", size_usd=size,
                net_win_usd=0, net_loss_usd=0, n_traders=1, buyers_24h=0, sellers_24h=0, features={"copy_log_id": 7})


def test_demo_mode_switch(app_and_conn):
    app, _ = app_and_conn
    c = client_for(app, "alice", USER_PW)
    assert c.get("/api/status").json()["demo_mode"] is False
    c.put("/api/settings/prefs", json={"demo_mode": True})
    assert c.get("/api/status").json()["demo_mode"] is True


def test_autotrade_only_for_users_who_turned_it_on_and_within_limits(app_and_conn):
    app, conn = app_and_conn
    alice = users.by_name(conn, "alice")["id"]
    # Saved before pick types were removed: the old "types" list is ignored.
    users.set_setting(conn, alice, "autotrade", {"enabled": True, "types": ["Strong", "Pump"], "max_open": 2,
                                                  "max_invested_pct": 0.5})
    assert autotrade.config(conn, alice) == {"enabled": True, "max_open": 2, "max_invested_pct": 0.5}
    picks = [_pick("XRP", score=0.5), _pick("SOL", score=3), _pick("ETH", score=2)]
    bought = autotrade.run(conn, picks, reference_bankroll=1000, markets={}, now=100)
    assert [b["symbol"] for b in bought] == ["SOL", "ETH"]  # best traders first, max 2 open, admin didn't opt in
    rows = conn.execute("SELECT user_id, source, auto, size_usd, strength, style, features FROM my_positions").fetchall()
    assert {r["user_id"] for r in rows} == {alice} and all(r["source"] == "demo" and r["auto"] == 1 for r in rows)
    assert rows[0]["size_usd"] == pytest.approx(1000)  # 10% of the $10k demo account, like 100/1000 of the reference
    assert rows[0]["strength"] == "Copy" and rows[0]["style"] == "copy"
    assert json.loads(rows[0]["features"]) == {"copy_log_id": 7}
    assert autotrade.run(conn, picks, 1000, {}, now=200) == []  # already at max open / already held
    assert conn.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'demo.auto_buy'").fetchone()[0] == 2


def test_autotrade_respects_invested_limit(app_and_conn):
    _, conn = app_and_conn
    alice = users.by_name(conn, "alice")["id"]
    users.set_setting(conn, alice, "autotrade", {"enabled": True, "max_open": 10, "max_invested_pct": 0.15})
    bought = autotrade.run(conn, [_pick("SOL"), _pick("ETH")], 1000, {}, now=100)
    total = sum(b["size_usd"] for b in bought)
    assert total <= 0.15 * 10_000 + 0.01 and len(bought) == 2 and bought[1]["size_usd"] == pytest.approx(500)


def test_demo_page_has_progress_and_results(app_and_conn):
    app, conn = app_and_conn
    c = client_for(app, "alice", USER_PW)
    alice = users.by_name(conn, "alice")["id"]
    portfolio.snapshot_demo(conn, alice, 10_100, 50_000, now=1000)
    body = c.get("/api/demo").json()
    assert body["account"]["start_balance"] == 10_000 and body["history"][0]["value"] == 10_100
    assert body["autotrade"]["enabled"] is False and body["results_by_type"] == {}
    c.post("/api/demo/reset", json={"start_balance": 2000})
    assert c.get("/api/demo").json()["account"]["value"] == 2000
    assert conn.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'demo.reset'").fetchone()[0] == 1


def test_selling_a_demo_trade_uses_the_live_price(app_and_conn):
    app, conn = app_and_conn
    c = client_for(app, "alice", USER_PW)
    alice = users.by_name(conn, "alice")["id"]
    pid = portfolio.open_position(conn, user_id=alice, market_key="SOL", symbol="SOL", entry_price=100.0,
                                  size_usd=500, pick=None, now=100, source="demo", cost_pct=0.005)
    with conn:
        conn.execute("UPDATE my_positions SET last_price = 110 WHERE id = ?", (pid,))
    assert c.post(f"/api/positions/{pid}/close", json={"exit_price": 1000}).status_code == 200
    row = conn.execute("SELECT status, exit_price, net_return FROM my_positions WHERE id = ?", (pid,)).fetchone()
    assert row["status"] == "closed" and row["exit_price"] == 110  # the typed price is ignored for demo trades
    assert row["net_return"] == pytest.approx(0.095)  # +10% minus costs
    users.create(conn, "bob", USER_PW, role="user")
    bob = client_for(app, "bob", USER_PW)
    assert bob.post(f"/api/positions/{pid}/close", json={}).status_code == 404


# --- fees ------------------------------------------------------------------------------------------------------

def test_user_fees_are_used_for_costs(app_and_conn):
    app, conn = app_and_conn
    from app.pipeline import bankroll_info
    c = client_for(app, "alice", USER_PW)
    alice = users.by_name(conn, "alice")["id"]
    cfg = app.state.ctx.settings
    assert bankroll_info(conn, cfg, 0, alice)["fee_source"] == "default"
    assert c.put("/api/settings/prefs", json={"fee_taker": 0.2}).status_code == 422  # 20% is not a fee
    assert c.put("/api/settings/prefs", json={"fee_taker": 0.002, "fee_maker": 0.001}).status_code == 200
    b = bankroll_info(conn, cfg, 0, alice)
    assert b["fee_rate"] == 0.002 and b["maker_fee_rate"] == 0.001 and b["fee_source"] == "yours"
    assert c.get("/api/settings").json()["fees"]["taker"] == 0.002


def test_autotrade_costs_use_each_users_fee(app_and_conn):
    _, conn = app_and_conn
    alice = users.by_name(conn, "alice")["id"]
    users.set_setting(conn, alice, "autotrade", {"enabled": True, "max_open": 5, "max_invested_pct": 1})
    p = _pick("SOL")  # costed for a 0.10% reference fee: 0.007 round trip
    autotrade.run(conn, [p], 1000, {}, now=1, reference_fee=0.001, fee_for=lambda uid: 0.002)
    cost = conn.execute("SELECT cost_pct FROM my_positions").fetchone()[0]
    assert cost == pytest.approx(0.007 + 2 * 0.001)


def test_temporary_password_must_be_changed_first(app_and_conn):
    app, conn = app_and_conn
    users.create(conn, "firstadmin", "short1!", role="admin", temporary=True)
    with pytest.raises(users.UserError):
        users.create(conn, "someone", "short1!")  # without temporary the rules apply
    c = client_for(app, "firstadmin", "short1!")
    assert c.get("/api/status").json()["user"]["must_change_password"] is True
    assert c.get("/api/copies").status_code == 403 and c.get("/api/admin/users").status_code == 403
    assert c.post("/api/auth/password", json={"current": "short1!", "new": "a much better password 1"}).status_code == 200
    assert c.get("/api/admin/users").status_code == 200
