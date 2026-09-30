# Copy Signals

Two crypto strategies, each running with pretend money until it proves itself. The **Auto-trader** (home page) runs
a backtested BTC/ETH trend strategy on a demo account. The **Long/short test** ranks the 25 most traded coins every day
with a model trained on 8 years of market data, buys the best-scored fifth and shorts the worst-scored fifth, with paper
money, futures fees and funding. With a read-only OKX key the app also mirrors your real OKX account. Many other ideas,
including copying traders, were tested and dropped (see [Tested and dropped](#tested-and-dropped)). Nothing here trades
real money.

OKX is the only exchange for real money: it's licensed in the EU (MiCA, via Malta; futures as "X-Perps" under MiFID).

## Run

```bash
python3 -m venv .venv && .venv/bin/pip install --require-hashes -r requirements.txt -r requirements-dev.txt
.venv/bin/uvicorn app.main:app --app-dir backend --port 8000
```

Open http://localhost:8000 and log in. OKX prices and your OKX account refresh every 5 minutes. The trend bot checks
once a day at 00:05 UTC and the long/short test runs at 00:15 UTC; the first long/short run happens right after the
first start (market history, then training the model: a few minutes). Tests: `.venv/bin/pytest`. Double-clicking
`Start Copy Signals.command` does the same as the uvicorn line.

## Auto-trader (trend bot)

`trendbot.py`, strategy in `backtest.TrendEnsemble`. BTC and ETH each get half of the account, held in proportion to
how many of their 50/100/150-day averages the price is above; the rest stays in USDT. It checks once a day, 5 minutes
after the 00:00 UTC close (OKX daily candles), and trades only when a holding is off target by 1% of the account or
more. Fills use the live OKX price with slippage and the user's taker fee. Each user has their own **demo** account:
start, pause, resume and reset on the Auto-trader page; every run is in the audit log and sends a phone alert.

**Why this strategy** (`python -m app.manage backtest`, from 2018-09, 0.25% per unit traded):

| | a year | worst drop |
|---|---|---|
| Trend bot BTC+ETH (with the funding rule) | +55% | −49% |
| Hold BTC | +35% | −77% |
| Hold ETH | +31% | −79% |
| Top-20 coin trend portfolio | +26% | −64% |
| Top-5 weekly gainers | −6% | −92% |

The multi-coin strategies look great on OKX data (+58% to +101% a year) only because OKX lists today's survivors.
On Binance history, which still includes delisted coins (LUNA, FTT…), they lose to holding BTC. The BTC trend rule
holds up for every lookback from 50 to 150 days and at double fees. Averaging three lookbacks avoids picking the best
one after the fact. Backtests aren't promises: the bot mainly helps by stepping aside in long crashes and loses in
choppy years.

**Why once a day** (BTC+ETH, Binance hourly/4-hour history since 2018-09, 0.25% per unit traded): the same rule
checked every 4 hours made +49% a year, and every hour +39%. Faster averages traded far more and lost: 8/17/25-day
averages on 4-hour bars made +26%, 2/4/6-day averages checked hourly −52% a year (481 trades a year). Most of the
difference is fees.

Price history lives in `data/history.db` (`history.py`): OKX daily candles for the bot, Binance daily candles (all
pairs, dead ones included) for research only.

## Long/short test

`longshort.py` (the paper account), `lsmodel.py` (the model), `lsdata.py` (the data). Research: `research/daily_patterns.py`
and `research/longshort_check.py`.

**What it does.** Every day after the 00:00 UTC close: new market data for every coin (Binance spot candles with
taker-buy volume and trade counts, Binance perpetual volume and funding, Bybit open interest, Deribit's BTC implied
volatility). 42 signals per coin (trends over 1–90 days, volatility, distance from averages, volume surges, buying
pressure, funding, futures activity, open-interest changes, plus market-wide ones) are ranked across the day's 100
most traded coins. Five gradient-boosting models, retrained every 30 days on everything since 2018, predict each coin's
move over the next 3 days compared with the average coin; their predictions are averaged. Among the 25 most traded
coins that have a perpetual, the best-scored fifth is bought and the worst-scored fifth is shorted, half the account
on each side. A coin stays while it's within 1.5 fifths of its end. Every trade pays 0.10% (0.05% OKX futures taker +
slippage) and positions pay or receive the day's funding. Paper money, one shared account ($10,000 at the start);
admins can start it over.

**Why.** The single-coin signals rank coins against each other consistently from 2018 through 2026 (calm coins beat
wild ones, recent losers beat recent winners, coins with a lot of open interest for their volume do better), while
nothing predicts where the whole market goes the next day. Buying only the best coins lost money in 2024–2026
because altcoins as a group fell 55% a year; buying the best and shorting the worst cancels that out.

**Backtest** (`python -m app.manage ls-backtest`, walk-forward: a model trained only on earlier data every 3 months,
trading exactly like the paper account at daily closes): see the Long/short page for the latest numbers. From mid-2022,
averaging five models gave about +83% a year with a −24% worst drop and a positive result in every year; a single
model gave between +54% and +107% depending only on its random draw, so the size of the edge is uncertain. It stays
positive with fees doubled and when trading a day late; tripled fees leave little. It needs futures (shorting) and
trades about 70% of the account a day, so it's paper-only until months of live results match.

## Hosting (24/7, reachable from your phone)

The app only listens on `127.0.0.1` and is reached through [Tailscale](https://tailscale.com) (free for personal use):
a private network between your own devices with an HTTPS address like `https://copy-signals.tail1234.ts.net`. Nothing
is exposed to the public internet. Host it in Europe: Binance blocks US servers, and the app reads its public data.

**Free: Oracle Cloud "Always Free"** (2 ARM cores and 12 GB are free forever; this app needs well under 1 GB):
1. Sign up at oracle.com/cloud/free. A card is needed to check your identity; Always Free resources aren't charged.
   Pick a **home region in Europe** (Frankfurt, Amsterdam, Zurich, Milan…): it can't be changed later.
2. Upgrade the account to **Pay As You Go** (still €0 within the free limits). Otherwise Oracle may reclaim a free
   machine that's idle for 7 days, and this app is mostly idle.
3. Create a compute instance: **Ubuntu 24.04**, shape **VM.Standard.A1.Flex** with 1 OCPU and 6 GB. If it says "out
   of capacity", try again later or pick the free **VM.Standard.E2.1.Micro**. Add your SSH public key. Don't open
   any ports.
4. On your Mac: `deploy/push.sh ubuntu@<server-ip> --with-data` (copies the app, your database, keys and price history).
5. On the server (`ssh ubuntu@<server-ip>`):
   ```bash
   curl -fsSL https://tailscale.com/install.sh | sh
   sudo tailscale up                 # log in with the same account as your phone and Mac
   sudo tailscale serve --bg 8000    # prints your https://….ts.net address
   bash copy-signals/deploy/setup.sh
   nano copy-signals/.env            # put the ….ts.net name in ALLOWED_HOSTS
   sudo systemctl restart copy-signals
   ```
6. Install Tailscale on your phone and Mac, open the `https://….ts.net` address and log in.
7. Stop the app on your Mac, so only one copy runs.

Updates: `deploy/push.sh ubuntu@<server-ip>`, then `ssh ubuntu@<server-ip> sudo systemctl restart copy-signals`.
Logs: `journalctl -u copy-signals -f` or `data/logs/app.log`. Back up `data/` now and then.

**Other options:** any small Ubuntu 24.04 server in the EU with the same steps (e.g. Hetzner, about €4 a month, no
capacity problems), or your own Mac left on and awake with Tailscale (free, but it stops when the Mac sleeps or shuts
down).

## Accounts and admin

Everything needs a login. Admin tasks on the command line (run from the `backend` folder):

```bash
cd backend
../.venv/bin/python -m app.manage create-admin <username>      # asks for the password
../.venv/bin/python -m app.manage set-password <username>
../.venv/bin/python -m app.manage list-users
../.venv/bin/python -m app.manage backtest                      # update price history, backtest every strategy
../.venv/bin/python -m app.manage ls-update                     # long/short data: import data/market.db, fetch new days
../.venv/bin/python -m app.manage ls-train                      # retrain the long/short model now
../.venv/bin/python -m app.manage ls-backtest [2022-07-01]      # walk-forward backtest shown on the Long/short page
```

In the app, admins get an **Admin** page: users (add, make admin, disable, reset password, unlock), active
sessions (end any of them), the audit log with filters, data-source health, the long/short test's state, and the
suggested starting balance for new trend bots.

**Security** (OWASP Top 10, 2025):
- **Passwords:** hashed with Argon2id. At least 12 characters. 5 wrong tries lock the account for 15 minutes, and
  logins are rate-limited per IP.
- **Two-step login:** optional (TOTP, any authenticator app), recommended for admins.
- **Sessions:** a random token in an HttpOnly, SameSite=Strict cookie, stored only as a hash. It ends after
  30 minutes idle or 12 hours. Changing your password logs out your other sessions.
- **Requests:** every state-changing request needs the session's CSRF token and a same-origin Origin header.
  Strict Content-Security-Policy (no inline scripts), HSTS when `HTTPS=1`, Host header allow-list, and API rate limit.
- **Data access:** every user sees only their own trend bot, OKX account, settings and keys (the long/short paper test
  is shared and only admins can reset it); admin routes check the role on the server.
- **Secrets:** OKX keys and 2FA secrets are encrypted (Fernet) with `data/secret.key` (file mode 600, git-ignored,
  back it up).
- **Errors and logs:** errors never leak details (a reference number is logged instead) and input isn't echoed.
  `data/logs/app.log` rotates at 5 MB. The audit log records logins, failures, lockouts, setting changes, key changes
  and bot and long/short trades, with secrets redacted, and warns on bursts of failed logins.
- **Supply chain:** dependencies are pinned with hashes (`requirements*.txt`, made with pip-compile) and checked
  with `pip-audit`.

## Your OKX account (read-only)

### Connect OKX

On OKX (European accounts: my.okx.com): Profile → API → Create API key. Choose a passphrase and tick **only
"Read"**. Then paste it in **Settings → OKX connection**. The key is tested first, stored encrypted with your account,
and refused if it can withdraw. European keys use OKX's `eea.okx.com` API; pick "Global account" for one opened
outside Europe.

The Portfolio page then shows, refreshed every 5 minutes (or when you click "Sync with OKX"): your trading-account
total, cash, Funding and Earn balances, each coin with its average cost (worked out from your trades) and result,
whether a stop-loss order (TP/SL or trailing) protects it, open orders and recent trades.

### Fees

Enter your OKX spot fees in **Settings → OKX spot fees** (for example maker 0.10%, taker 0.20%). The trend bot pays the
taker fee on every trade; until you set them, 0.20% is used. The long/short test uses futures costs (0.05% taker plus
0.05% slippage).

### Phone alerts

In **Settings → Phone notifications**, paste an `https://ntfy.sh/<secret-topic>` address and subscribe to that topic
in the ntfy app. You get the trend bot's trades and (admins) the long/short test's daily picks.

## Data (all public)

| Source | Used for |
|---|---|
| OKX spot | Prices of your OKX coins; the trend bot's daily candles; live price stream for the bot |
| Binance spot and futures | Daily candles of every coin (delisted ones kept), taker-buy volume, funding rates: the long/short model; the trend bot's funding rule |
| Bybit | Daily open interest per coin (long/short model) |
| Deribit | BTC implied volatility (long/short model) |

Price history lives in `data/history.db`; the long/short model is saved as `data/ls_model.pkl` and its backtest as
`data/ls_backtest.json`.

## Configuration (`.env` or environment)

| Var | Default | |
|---|---|---|
| `ALLOWED_HOSTS` | `localhost,127.0.0.1` | Host names the app answers to |
| `HTTPS` | `0` | `1` behind HTTPS: secure cookies and HSTS |
| `DB_PATH`, `SECRET_KEY_PATH`, `LOG_DIR`, `HISTORY_PATH` | `data/…` | |
| `RESEARCH_MARKET_DB` | `data/market.db` | research/collect_market.py's download, imported once to seed the long/short history |
| `FEE_RATE` / `MAKER_FEE_RATE` / `SLIPPAGE` | `0.002` / `0.001` / `0.0005` | Per side; each user's own fees replace these |
| `NOTIFY_WEBHOOK_URL` | | Admin alerts |

The suggested starting balance for new trend bots is set on the Admin page. OKX keys are set per user in Settings, not
in `.env`. Training the long/short model needs about 1 GB of memory for a minute each month: on Oracle's free tier use
the A1.Flex machine, not the 1 GB E2.Micro.

## Toward real money

Neither strategy trades real money. The order of steps:
1. **Now:** both run with pretend money; compare their live results with their backtests.
2. **Trend bot, after months that behave like the backtest:** a trade-only OKX spot key (never withdrawals, locked to
   the server's IP), a money limit and a stop-everything switch, and your go-ahead.
3. **Long/short, after 2–3 months of paper results close to the backtest:** OKX futures access (X-Perps in Europe
   needs an appropriateness test), the same safeguards, and your go-ahead. It trades daily, so it has to be automated.

## Research

- `research/sentiment_funding.py`: the Fear & Greed index didn't improve the trend bot. Futures funding did, in both
  2019–2022 and 2023–now: when a coin's 3-day average funding is at or below zero, the bot keeps at least a third of
  that coin (+51% → +55% a year, worst drop −49% either way).
- `research/collect_market.py` → `daily_patterns.py` → `longshort_check.py`: the long/short test above.
- `research/futures_backtest.py`: the trend bot on futures. Cheaper fees are eaten by funding (about 10% a year on a
  long), shorting in downtrends halved the return, 2x/3x leverage had −79%/−93% worst drops. Spot without leverage stays.
- Copy trading: `copy_backtest.py`, `copy_recheck.py`, `copy_profitable.py`, `copy_patterns.py`,
  `trader_positioning.py`, `dip_backtest.py` (see below).

## Tested and dropped

Removed because they lost money after fees, lost to holding BTC, or only looked good on the data they were found on.
Their code is in the git history.

- **Swing copies** (copy a followed trader's long once they've held it 12 hours, sell when they sell): the first test
  showed +1.33% a trade, but the same coins bought at random times for the same hours did as well (Hyperliquid: skill
  −0.2%, range −1.0% to +0.7%; July −2.8%). BTC rose 44% in those 92 days, so nearly any buy made money. Last month's
  profitable traders did no better than last month's losers the next month, copying the moment they buy changed
  nothing, and the crowd of recently profitable traders (Hyperliquid, OKX lead traders) didn't predict the next days
  either. OKX lead traders showed a small edge, possibly because traders who quit aren't listed.
- **Buying dips** (a top-50 coin down 10–30% in a week): +5–10% a trade on 2018–2022, −0.7% to −2.5% on 2023–now.
- **Picks** (coins several followed traders hold): no backtest supported them.
- **Rising now** (smaller coins starting to rise on unusual volume, and pump rides): 3,683 trades at −0.64% each after
  costs on 6 months of 5-minute candles, every month negative; random entries did −0.48%.
- **52 pump, breakout and dump rules** on small coins: none worked on both halves of the data.
- **Top-20 coin trend portfolio** and **top-5 weekly gainers**: see the table under Auto-trader; both lost to holding
  BTC once delisted coins are included.

## Caveats

- Backtests show what rules would have done, not what they will do. The trend bot wins by losing less in crashes and
  lags BTC in booms. The long/short model's edge was clear in every year tested, but its size varied a lot between
  otherwise identical models, and daily trading makes it sensitive to real costs.
- Research tool, not financial advice.
