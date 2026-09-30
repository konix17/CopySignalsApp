"""Command-line admin tasks. Run from the backend folder (cd backend):

    ../.venv/bin/python -m app.manage create-admin <username> [--temporary]
        (asks for the password, or pipe it on stdin; --temporary allows a weak password that must be changed at
         first login)
    ../.venv/bin/python -m app.manage set-password <username>
    ../.venv/bin/python -m app.manage import-okx-env <username>    (moves OKX_* keys from .env into the account)
    ../.venv/bin/python -m app.manage list-users
    ../.venv/bin/python -m app.manage backtest      (updates price history, then backtests every strategy; backtest.py)
"""

import asyncio
import getpass
import sys
import time

from . import backtest, db, history, trendbot, users
from .config import ROOT, settings
from .logs import audit
from .security import SecretBox


def _password() -> str:
    if not sys.stdin.isatty():
        return sys.stdin.readline().rstrip("\n")
    first = getpass.getpass("Password: ")
    if first != getpass.getpass("Repeat password: "):
        sys.exit("Passwords don't match.")
    return first


def _env_values(path) -> dict:
    out = {}
    if path.exists():
        for line in path.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def main(argv: list[str]) -> None:
    if len(argv) < 1:
        sys.exit(__doc__)
    conn = db.connect(settings.db_path)
    users.init_secrets(SecretBox(settings.secret_key_path))
    cmd, args = argv[0], argv[1:]

    if cmd == "create-admin" and len(args) in (1, 2) and (len(args) == 1 or args[1] == "--temporary"):
        try:
            user = users.create(conn, args[0], _password(), role="admin", temporary=len(args) == 2)
        except users.UserError as e:
            sys.exit(str(e))
        adopted = users.adopt_orphans(conn, user.id)
        audit(conn, "admin.user_created", username="(command line)", detail={"new_user": user.username, "role": "admin"})
        print(f"Admin '{user.username}' created. Existing data moved to this account: {adopted}")
    elif cmd == "set-password" and len(args) == 1:
        row = users.by_name(conn, args[0])
        if not row:
            sys.exit("No such user.")
        try:
            users.set_password(conn, row["id"], _password())
        except users.UserError as e:
            sys.exit(str(e))
        users.end_user_sessions(conn, row["id"])
        audit(conn, "admin.user_changed", username="(command line)", level="warning",
              detail={"target": row["username"], "password_reset": True})
        print("Password changed; all of that user's sessions were ended.")
    elif cmd == "import-okx-env" and len(args) == 1:
        row = users.by_name(conn, args[0])
        if not row:
            sys.exit("No such user.")
        env = _env_values(ROOT / ".env")
        names = {"OKX_API_KEY": "okx_api_key", "OKX_API_SECRET": "okx_api_secret", "OKX_API_PASSPHRASE": "okx_api_passphrase"}
        if not all(env.get(k) for k in names):
            sys.exit("OKX_API_KEY, OKX_API_SECRET and OKX_API_PASSPHRASE aren't all set in .env.")
        for env_name, secret_name in names.items():
            users.set_secret(conn, row["id"], secret_name, env[env_name])
        users.set_setting(conn, row["id"], "okx_region", env.get("OKX_REGION", "eea") or "eea")
        audit(conn, "okx.key_saved", user_id=row["id"], username=row["username"],
              detail={"from": ".env", "key_hint": env["OKX_API_KEY"][-4:]})
        print(f"OKX key stored encrypted for '{row['username']}'. You can now remove the OKX_* lines from .env.")
    elif cmd == "list-users":
        for r in conn.execute("SELECT id, username, role, disabled, last_login_at FROM users ORDER BY id"):
            last = time.strftime("%Y-%m-%d %H:%M", time.localtime(r["last_login_at"])) if r["last_login_at"] else "never"
            print(f"{r['id']:>3}  {r['username']:<20} {r['role']:<6} {'disabled' if r['disabled'] else 'active':<9} last login {last}")
    elif cmd == "backtest" and not args:
        hist = history.connect(settings.history_path)
        print("Updating price history (Binance for research, dead coins included; OKX for the bot)…")
        asyncio.run(history.update_binance(hist))
        asyncio.run(history.update(hist, pairs=list(trendbot.PAIRS), region=settings.okx_region, log=lambda *_: None))
        research = backtest.Panel(history.load(hist, history.BINANCE_BAR))
        start = trendbot.BACKTEST_FROM
        print(f"\nAll strategies, Binance prices, from {time.strftime('%Y-%m-%d', time.gmtime(start))}, "
              f"{backtest.DEFAULT_COST * 100:.2f}% per unit traded:")
        print(backtest.format_table([backtest.run(research, s, start).metrics() for s in backtest.STRATEGIES]))
        asyncio.run(history.update_funding(hist, list(trendbot.PAIRS)))
        okx = backtest.Panel(history.load(hist, "1Dutc", list(trendbot.PAIRS)))
        summary = trendbot.backtest_summary(okx, funding=history.load_funding(hist))
        print("\nThe trend bot on OKX prices:")
        print(backtest.format_table([summary["strategy"], *summary["hold"].values()]))
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
