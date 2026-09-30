"""Download OKX copy-trading lead traders and their closed futures positions (public API, about 3 months kept).
Research only: a second pool of traders, separate from Hyperliquid, to test copying on (research/copy_recheck.py).

    .venv/bin/python research/okx_fetch_leads.py

OKX only publishes position history for futures (SWAP) lead traders; spot lead traders show just summary stats.
"""
import sqlite3
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
BASE = "https://www.okx.com/api/v5/copytrading/"
GAP_S = 0.35  # public copy-trading endpoints allow about 5 requests per 2 seconds

db = sqlite3.connect(ROOT / "data" / "research.db", timeout=120)
db.executescript("""
CREATE TABLE IF NOT EXISTS okx_leads (code TEXT PRIMARY KEY, inst_type TEXT, nick TEXT, pnl REAL, pnl_ratio REAL,
  win_ratio REAL, aum REAL, lead_days INTEGER, copiers INTEGER, fetched_at INTEGER);
CREATE TABLE IF NOT EXISTS okx_lead_positions (code TEXT, id TEXT, inst_id TEXT, side TEXT, lever REAL, open_px REAL,
  close_px REAL, open_time INTEGER, close_time INTEGER, margin REAL, pnl REAL, PRIMARY KEY (code, id));
""")


def get(client, path, **params):
    for _ in range(5):
        time.sleep(GAP_S)
        r = client.get(BASE + path, params=params)
        if r.status_code == 429:
            time.sleep(5)
            continue
        r.raise_for_status()
        body = r.json()
        if body.get("code") == "50011":  # rate limited
            time.sleep(5)
            continue
        return body.get("data", [])
    return []


with httpx.Client(timeout=30, headers={"User-Agent": "copy-signals-research"}) as client:
    leads = []
    for inst in ("SWAP", "SPOT"):
        page = 1
        while True:
            data = get(client, "public-lead-traders", instType=inst, page=str(page), limit="20")
            if not data or not data[0]["ranks"]:
                break
            leads += [(inst, t) for t in data[0]["ranks"]]
            if page >= int(data[0]["totalPage"]):
                break
            page += 1
    db.executemany("INSERT OR REPLACE INTO okx_leads VALUES (?,?,?,?,?,?,?,?,?,NULL)", [
        (t["uniqueCode"], inst, t["nickName"], float(t["pnl"]), float(t["pnlRatio"]), float(t["winRatio"] or 0),
         float(t["aum"] or 0), int(t["leadDays"]), int(t["copyTraderNum"])) for inst, t in leads])
    db.commit()
    swap = [t["uniqueCode"] for inst, t in leads if inst == "SWAP"]
    done = {r[0] for r in db.execute("SELECT code FROM okx_leads WHERE fetched_at IS NOT NULL")}
    print(f"{len(leads)} lead traders ({len(swap)} futures); fetching position history", flush=True)
    for i, code in enumerate(swap):
        if code in done:
            continue
        after = None
        for _ in range(60):  # 100 per page
            params = {"instType": "SWAP", "uniqueCode": code, "limit": "100"}
            if after:
                params["after"] = after
            rows = get(client, "public-subpositions-history", **params)
            if not rows:
                break
            db.executemany("INSERT OR IGNORE INTO okx_lead_positions VALUES (?,?,?,?,?,?,?,?,?,?,?)", [
                (code, x["subPosId"], x["instId"], x["posSide"], float(x["lever"] or 0), float(x["openAvgPx"] or 0),
                 float(x["closeAvgPx"] or 0), int(x["openTime"]), int(x["closeTime"]), float(x["margin"] or 0),
                 float(x["pnl"] or 0)) for x in rows if x.get("openTime") and x.get("closeTime")])
            after = rows[-1]["subPosId"]
            if len(rows) < 100:
                break
        db.execute("UPDATE okx_leads SET fetched_at = ? WHERE code = ?", (int(time.time()), code))
        db.commit()
        if i % 20 == 0:
            n = db.execute("SELECT COUNT(*) FROM okx_lead_positions").fetchone()[0]
            print(f"  {i + 1}/{len(swap)} traders, {n:,} positions", flush=True)
    # Positions still open (history only has closed ones): open_time known, close_time NULL.
    db.execute("DELETE FROM okx_lead_positions WHERE close_time IS NULL")
    for code in swap:
        rows = get(client, "public-current-subpositions", instType="SWAP", uniqueCode=code, limit="100")
        db.executemany("INSERT OR IGNORE INTO okx_lead_positions VALUES (?,?,?,?,?,?,?,?,?,?,?)", [
            (code, x["subPosId"], x["instId"], x["posSide"], float(x["lever"] or 0), float(x["openAvgPx"] or 0), None,
             int(x["openTime"]), None, float(x["margin"] or 0), None) for x in rows if x.get("openTime")])
    db.commit()
    print("done", db.execute("SELECT COUNT(DISTINCT code), COUNT(*), SUM(close_time IS NULL) FROM okx_lead_positions").fetchone())
