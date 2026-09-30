# Copy Signals

A crypto spot trade-picker and trend-following auto-trader for OKX. The **Auto-trader** (home page) runs a backtested
BTC/ETH trend strategy on a demo account. It is also a trade-picker: In the background it follows consistently profitable public traders, checks
the price trend and how crowded the trade is, and shows a short list of coins to buy on OKX. For each one you get
an amount, a stop-loss, a take-profit and a hold time. It follows every pick as a paper trade to build an honest
track record. With a read-only OKX key it also mirrors your real portfolio and checks it.

OKX is the only exchange: it's licensed in the EU (MiCA, via Malta), and among EU-licensed exchanges it has the most
coins that trade enough to buy and sell cleanly. Binance stopped new spot trading for EU users in July 2026.

## Run

```bash
python3 -m venv .venv && .venv/bin/pip install --require-hashes -r requirements-dev.txt
.venv/bin/uvicorn app.main:app --app-dir backend --port 8000
```

Open http://localhost:8000 and log in. The first data pull takes 1–3 minutes; after that trader data refreshes every
10 minutes (admin setting) for leaderboards, crowding and trends. Followed traders' positions, OKX prices, picks and
accounts update every minute, and the rising-now scanner runs every 15 seconds. Open positions, demo trades and paper trades are
re-priced every 2 seconds from OKX's live price stream (websocket), or every 10 seconds over REST while the stream is down.
Tests: `.venv/bin/pytest`. Double-clicking `Start Copy Signals.command` does the same as the uvicorn line.

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
../.venv/bin/python -m app.manage replay                        # the paper trades under other exit rules
../.venv/bin/python -m app.manage backtest                      # update price history, backtest every strategy
```

In the app, admins get an **Admin** page: users (add, make admin, disable, reset password, unlock), active
sessions (end any of them), the audit log with filters, data-source health, and app settings (refresh interval,
default bankroll, starting demo balance, high-risk budget), and the **Strategy lab**.

**Strategy lab** (`replay.py`): replays every paper trade on 5-minute OKX prices under other exit rules, with the
same costs, so rules can be compared on real data before the live ones change: what happened, stop/target/hold time
only, and the plan plus a "top traders sold" exit that only counts sells made after the buy. A coin is held once at a
time, so a rule that keeps a trade open also skips the re-buys made meanwhile. Run it from the tab or the command above.

**Security** (OWASP Top 10, 2025):
- **Passwords:** hashed with Argon2id. At least 12 characters. 5 wrong tries lock the account for 15 minutes, and
  logins are rate-limited per IP.
- **Two-step login:** optional (TOTP, any authenticator app), recommended for admins.
- **Sessions:** a random token in an HttpOnly, SameSite=Strict cookie, stored only as a hash. It ends after
  30 minutes idle or 12 hours. Changing your password logs out your other sessions.
- **Requests:** every state-changing request needs the session's CSRF token and a same-origin Origin header.
  Strict Content-Security-Policy (no inline scripts), HSTS when `HTTPS=1`, Host header allow-list, and API rate limit.
- **Data access:** every user sees only their own positions, demo account, settings and keys; admin routes check
  the role on the server.
- **Secrets:** OKX keys and 2FA secrets are encrypted (Fernet) with `data/secret.key` (file mode 600, git-ignored,
  back it up).
- **Errors and logs:** errors never leak details (a reference number is logged instead) and input isn't echoed.
  `data/logs/app.log` rotates at 5 MB. The audit log records logins, failures, lockouts, setting changes, key changes
  and demo trades, with secrets redacted, and warns on bursts of failed logins.
- **Supply chain:** dependencies are pinned with hashes (`requirements*.txt`, made with pip-compile) and checked
  with `pip-audit`.

## Demo and real

The **Demo** switch in the top bar chooses what you see. On: the demo account, and pick buttons make demo buys.
Off: your real OKX account.

**Automatic demo trading** (Demo portfolio page) buys every new pick of the ticked types (Strong and Good by default;
Early and Pump can be ticked, but lost money in the backtest below) with demo money. Limits: at most 10 open trades and 60% of the demo account invested, both adjustable. Each
trade is sized like the pick, scaled to the demo account. Trades follow the pick's plan and close by themselves.
Results are shown by pick type, and an hourly chart compares the account with holding BTC. Only the demo account is
ever traded automatically (this and the trend bot).

### Connect OKX (read-only)

On OKX (European accounts: my.okx.com): Profile → API → Create API key. Choose a passphrase and tick **only
"Read"**. Then paste it in **Settings → OKX connection**. The key is tested first, stored encrypted with your account,
and refused if it can withdraw. European keys use OKX's `eea.okx.com` API; pick "Global account" for one opened
outside Europe.

The app then reads, every refresh (or when you click "Sync with OKX"):
- **Balances:** your bankroll becomes your OKX trading-account total; Funding and Earn balances are shown separately.
- **Trades:** each holding's average cost is worked out from them.
- **Open orders:** it checks each holding is protected by a stop-loss (TP/SL or trailing stop) order.

Coins you buy show up in "My portfolio" automatically, with the stop, target and hold plan of the pick that was live
when you bought. When you sell on OKX, the position closes at your real sell price with your real fees.

### Fees

Enter your OKX spot fees in **Settings → Your OKX fees** (for example maker 0.10%, taker 0.20%). Every pick's
after-fee win/loss, demo trade and result uses the taker fee, since market orders and triggered stops pay it. Until
you set them, 0.20% taker is used.

### Phone alerts

In **Settings → Phone notifications**, paste an `https://ntfy.sh/<secret-topic>` address and subscribe to that topic
in the ntfy app. You get sell alerts and finished demo trades.

## Data (all public)

| Source | Used for |
|---|---|
| Hyperliquid, GMX | Profitable traders: leaderboards, open positions, PnL history (drawdowns) |
| OKX spot | Which coins you can buy, price, 24h volume, spread, daily/hourly/minute prices (trend, volatility, scanner) |
| OKX price stream | Live prices for everything open (public websocket, `wseea.okx.com` for European accounts) |
| OKX, Binance daily history | Backtests and the trend bot (`data/history.db`); Binance only for research |
| Binance & OKX futures | Top-trader long/short positioning and funding (crowding): public statistics, no account |

## How picks are chosen

Based on research on copy trading, trend following and crowding (sources in the app's design notes):

**Traders worth following** (`scoring.py`): profitable in at least 2 of week / month / all time. Market makers and
bots are excluded (huge volume relative to account size, tiny return), and so is anyone who lost 50% or more
of their typical balance at some point last month. They're ranked by return, profit and consistency, minus
their drawdown.

**Three checks per coin** (`picks.py`):
1. **Smart traders are buying** (required): 3 or more followed traders long, 65% or more of the weight on the
   long side, not net selling in the last 24h, and the price not far past their average entry (late entries are
   penalized).
2. **Price is in an uptrend:** above its 50-day average and up over 30 days.
3. **Not overcrowded:** under 75% of Binance's top futures traders long, funding under 0.05%/8h, no rush of new longs.

3 of 3 checks = **Strong**, 2 of 3 = **Good**. Only OKX spot USDT pairs with $20M or more daily volume and a
spread of 0.2% or less are considered. At most 5 picks are shown, or 2 when BTC is below its 50-day average; in
that case sizes are also halved.

**Levels and size:**
- **Stop:** 1.5 × daily volatility × hold_days^¼, kept between 3% and 20%.
- **Target:** 2 × the stop distance.
- **Costs:** fees, half the spread and slippage, each way.
- **Minimum reward:** picks whose net reward/risk is under 1.5 are dropped.
- **Size:** a stop costs 1.25% of the bankroll for a Strong pick and 0.75% for a Good one, capped at 10% per
  coin and 40% across all open positions and picks.

**Sell advice for your positions** (`portfolio.py`):
- **SELL:** stop hit, traders left or flipped, heavy selling since you bought (sells from before the buy don't
  count), or the price fell below its 50-day average.
- **TAKE PROFIT:** target hit.
- **CHECK:** some selling, the price below its 20-day average, exchange traders cutting, hold time up, or no stop
  order on OKX.

**Track record** (`tracker.py`): each pick is one paper trade. It exits at the stop, the target, a sell signal or the
end of the hold time, net of costs, and is compared with BTC over the same days. Results are split by pick type.

## Configuration (`.env` or environment)

| Var | Default | |
|---|---|---|
| `ALLOWED_HOSTS` | `localhost,127.0.0.1` | Host names the app answers to |
| `HTTPS` | `0` | `1` behind HTTPS: secure cookies and HSTS |
| `DB_PATH`, `SECRET_KEY_PATH`, `LOG_DIR` | `data/…` | |
| `MAX_PICKS` / `MAX_PICKS_DOWNTREND` | `5` / `2` | |
| `RISK_STRONG` / `RISK_GOOD` | `0.0125` / `0.0075` | Bankroll lost if a stop is hit |
| `MAX_POSITION_PCT` / `MAX_TOTAL_PCT` | `0.10` / `0.40` | Per coin / all positions |
| `MIN_VOLUME_USD` / `MAX_SPREAD` | `20000000` / `0.002` | Tradability |
| `FEE_RATE` / `MAKER_FEE_RATE` / `SLIPPAGE` | `0.002` / `0.001` / `0.0005` | Per side; each user's own fees replace these |
| `MIN_NET_REWARD_RISK` | `1.5` | |
| `SOURCES` / `EXCHANGES` | `hyperliquid,gmx` / `binance,okx` | Trader data / futures positioning data |
| `NOTIFY_WEBHOOK_URL` | | Admin alerts (security warnings) |

Refresh interval, default bankroll, starting demo balance and high-risk budget are set on the Admin page. OKX keys
are set per user in Settings, not in `.env`.

## Toward automatic trading

The order of steps:
1. **Now:** manual trading with the order ticket.
2. **Once the track record holds up after fees:** add an OKX executor in `execution.py`. It would place a market
   buy with the pick's stop and target attached as a TP/SL order. Use a separate key with
   spot trading enabled, never withdrawals, locked to your server's IP. You'd confirm each order at first, and
   there should be a kill switch and a maximum total exposure.

## Swing copies (`swing.py`)

A long that a followed trader has held for at least 12 hours (and not cut in half), in a coin that's liquid on OKX
spot, becomes a **Swing copy**: shown on the Picks page, followed as a paper trade, and bought by automatic demo
trading when "Copy" is ticked (on by default). One copy per coin, following the best-scored trader holding it. It
sells when that trader closes or halves the position (checked every minute), with a 25% safety stop and a 30-day cap,
and ignores the crowd and trend exits that normal picks use. Size: 5% of the bankroll (half in a downtrend).

**Why** (`research/copy_backtest.py`): 6,500 long trades by 260 random Hyperliquid traders (second half of the last
90 days, traders never chosen with hindsight), copied on spot at 0.5% round-trip cost:

| Copying | Per trade after costs |
|---|---|
| Every long, instantly / 1 / 5 / 10 minutes late | +0.28% / +0.29% / +0.29% / +0.28% |
| Positions they closed within an hour | −0.59% |
| Only positions still open after 4 / 12 / 24 hours | +1.07% / +1.33% / +1.90% |

Delay doesn't matter; fees on short trades do. Positions traders keep are the ones they're right about (before costs
they beat holding BTC over the same hours by 0.84–1.37%). It's a mostly rising market, so copies are being proven in demo.

## Research: other data sources

`research/sentiment_funding.py`: the Fear & Greed index didn't improve the trend bot. Futures funding did, in both
2019–2022 and 2023–now: when a coin's 3-day average funding is at or below zero (traders are paying to bet against it),
the bot keeps at least a third of that coin. That lifts the backtest from +51% to +55% a year (worst drop −49% either
way). `research/pump_dump_explore.py`: 52 pump, breakout and dump rules on 6 months of small coins; none worked on both
the first four months and the last two.

## Research: does Rising now work?

`research/rising_backtest.py` replays the Rising now rules on 6 months of Binance 5-minute candles for every coin that
was ranked 21–150 by volume at the time (237 coins, dead ones included; data in `data/research.db`, about 1.4 GB,
safe to delete and re-download). Result for 2026-03 to 2026-09 at 0.65% round-trip costs:

| | Trades | Per trade after costs |
|---|---|---|
| All flags | 3,683 | −0.64% (every month negative) |
| Early movers / pump rides | 786 / 2,897 | −0.67% / −0.63% |
| Random entries, same exits | 7,960 | −0.48% |

Before costs the flags average about 0%: no edge at these horizons. So automatic demo trading buys only Strong and
Good picks by default. Rising coins are still shown and followed as paper trades (the live scanner reacts faster than
5-minute bars). The test also found a bug, now fixed: pump rides below their 50-day average were sold at the next check.

## Caveats

- Copy-trading followers usually do worse than the traders they copy, mostly through late entries. The late-entry
  penalty and the trend and crowding checks target that, but only the track record will show whether they work.
- Buy/sell flow, observed hold times and trader drawdowns fill in over the first hours and days.
- Before you charge subscribers, get legal advice: specific paid buy/sell recommendations can be regulated
  investment advice, and each data source's terms need checking for commercial use.
- This is a research tool, not financial advice.
