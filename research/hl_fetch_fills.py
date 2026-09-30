"""Download 90 days of Hyperliquid fills for a random sample of active traders plus the ones the app follows.
Research only (see research/copy_backtest.py). Paced gently so the running app's own Hyperliquid calls keep working.

    .venv/bin/python research/hl_fetch_fills.py [--sample 400] [--days 90]
"""
import argparse
import random
import sqlite3
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
INFO = "https://api.hyperliquid.xyz/info"
LEADERBOARD = "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard"
GAP_S = 3.0  # userFillsByTime weighs 20+ of the 1200/min budget the app also uses

ap = argparse.ArgumentParser()
ap.add_argument("--sample", type=int, default=400)
ap.add_argument("--days", type=int, default=90)
args = ap.parse_args()

db = sqlite3.connect(ROOT / "data" / "research.db", timeout=120)
db.executescript("""
CREATE TABLE IF NOT EXISTS hl_fills (address TEXT, coin TEXT, px REAL, sz REAL, side TEXT, time INTEGER, dir TEXT,
  start_pos REAL, closed_pnl REAL, fee REAL, tid INTEGER, PRIMARY KEY (address, tid));
CREATE TABLE IF NOT EXISTS hl_traders (address TEXT PRIMARY KEY, grp TEXT, account_value REAL, fetched_at INTEGER);
""")
done = {r[0] for r in db.execute("SELECT address FROM hl_traders WHERE fetched_at IS NOT NULL")}

rows = httpx.get(LEADERBOARD, timeout=90).json()["leaderboardRows"]
active = [r for r in rows if float(dict(r["windowPerformances"])["month"]["vlm"]) > 100_000]
random.seed(20260930)
sample = random.sample(active, min(args.sample, len(active)))
app = sqlite3.connect(ROOT / "data" / "trading.db")
followed = {r[0] for r in app.execute("SELECT DISTINCT address FROM trader_stats WHERE source='hyperliquid' AND followed=1")}
value = {r["ethAddress"]: float(r["accountValue"]) for r in rows}
todo = [(r["ethAddress"], "sample") for r in sample] + [(a, "followed") for a in sorted(followed)]
print(f"{len(todo)} traders ({len(done)} already fetched)", flush=True)

start_ms = int((time.time() - args.days * 86400) * 1000)
with httpx.Client(timeout=30) as client:
    for i, (addr, grp) in enumerate(todo):
        if addr in done:
            continue
        since, pages = start_ms, 0
        while pages < 6:
            time.sleep(GAP_S)
            r = client.post(INFO, json={"type": "userFillsByTime", "user": addr, "startTime": since, "aggregateByTime": True})
            if r.status_code == 429:
                time.sleep(30)
                continue
            r.raise_for_status()
            fills = r.json()
            db.executemany("INSERT OR IGNORE INTO hl_fills VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                           [(addr, f["coin"], float(f["px"]), float(f["sz"]), f["side"], f["time"], f["dir"],
                             float(f["startPosition"]), float(f["closedPnl"]), float(f.get("fee") or 0), f["tid"])
                            for f in fills])
            pages += 1
            if len(fills) < 2000:
                break
            since = max(f["time"] for f in fills) + 1
        db.execute("INSERT OR REPLACE INTO hl_traders VALUES (?,?,?,?)", (addr, grp, value.get(addr), int(time.time())))
        db.commit()
        if i % 25 == 0:
            n = db.execute("SELECT COUNT(*) FROM hl_fills").fetchone()[0]
            print(f"  {i + 1}/{len(todo)} traders, {n:,} fills", flush=True)
print("done", db.execute("SELECT COUNT(DISTINCT address), COUNT(*) FROM hl_fills").fetchone())
