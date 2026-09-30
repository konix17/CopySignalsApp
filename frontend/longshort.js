// Long/short test page: the shared paper account, today's ranking, the backtest of the same code. Uses common.js
// helpers and bot.js's lineChart.

const lsState = { data: null };
const STATE_TEXT = {
  starting: "Starting…", "importing history": "Importing market history…", "updating market data": "Updating market data…",
  "training the model": "Training the model (a minute or two)…", "scoring coins": "Scoring coins…",
  "waiting for data": "Waiting for the day’s data", waiting: "Running", error: "Last run failed",
};

function lsHero(d) {
  const a = d.account;
  const busy = d.state !== "waiting";
  const pill = d.state === "error" ? `<span class="pill pause">Error</span>` : a && !busy ? `<span class="pill run">Running</span>`
    : `<span class="pill">${esc(STATE_TEXT[d.state] || d.state)}</span>`;
  if (!a) {
    return `<div class="card hero">
      <div class="hero-top">${pill}<span class="pill demo">Paper</span></div>
      <p class="muted">The first run opens a paper account with ${money(10000, 0)} and buys the first long and short positions.
        ${d.error ? esc(d.error) : "It takes a few minutes the first time (market history, then the model)."}</p>
    </div>`;
  }
  const fees = d.trades.reduce((s, t) => s + t.fee_usd, 0);
  const funding = d.days.reduce((s, x) => s + x.funding_usd, 0);
  const nl = a.positions.filter((p) => p.side === "long").length, ns = a.positions.length - nl;
  return `<div class="card hero">
    <div class="hero-top">${pill}<span class="pill demo">Paper</span>
      <span class="muted small">started ${esc(ago(a.started_at))} with ${money(a.start_balance, 0)}</span>
      ${d.is_admin ? `<span class="hero-actions"><button type="button" class="btn danger ghost small" id="ls-reset">Start over</button></span>` : ""}
    </div>
    <div class="value">${money(a.equity, 2)}</div>
    <div class="value-sub"><span class="chg ${a.pnl >= 0 ? "up" : "down"}">${pct(a.pnl_pct, 2)}</span>${signed(a.pnl, money(a.pnl, 2))}
      <span class="muted">since start${a.btc_return != null ? ` · BTC ${pct(a.btc_return, 2)} over the same time` : ""}</span></div>
    <div class="stats">
      ${stat("Bought (long)", money(a.long_value), `${nl} coins`)}
      ${stat("Shorted", money(a.short_value), `${ns} coins`)}
      ${stat("Fees paid", money(fees, 2))}
      ${stat("Funding", signed(-funding, money(-funding, 2)), funding > 0 ? "paid" : "received")}
      ${stat("Next run", d.state === "waiting" ? `in ${esc(until(d.next_run))}` : esc(STATE_TEXT[d.state] || d.state))}
    </div>
    ${d.error ? `<p class="muted small">${esc(d.error)}</p>` : ""}
    <div class="chart-head"><div class="chart-legend"><span class="key bot">Paper account</span><span class="key btc">Holding BTC instead</span></div></div>
    <div class="chart-wrap" id="ls-chart"></div>
  </div>`;
}

function lsPositions(a) {
  if (!a || !a.positions.length) return "";
  const row = (p) => `<tr><td>${coinIcon(p.coin)} ${esc(p.coin)}</td>
    <td><span class="badge ${p.side === "long" ? "buy" : "short"}">${p.side === "long" ? "Long" : "Short"}</span></td>
    <td class="num">${money(p.value)}</td><td class="num">${price(p.entry_price)}</td><td class="num">${price(p.price)}</td>
    <td class="num">${signed(p.pnl, money(p.pnl, 2))} <span class="muted small">${pct(p.pnl_pct, 1)}</span></td>
    <td>${esc(date(p.opened_day))}</td></tr>`;
  return `<div class="card"><p class="card-title">Positions</p><div class="table-wrap"><table>
    <thead><tr><th>Coin</th><th>Side</th><th class="num">Size</th><th class="num">Entry</th><th class="num">Now</th>
      <th class="num">Result</th><th>Since</th></tr></thead>
    <tbody>${a.positions.map(row).join("")}</tbody></table></div>
    <p class="muted small">A short makes money when the coin falls. Prices are Binance spot, refreshed every 30 seconds while
      you watch.</p></div>`;
}

function lsLog(d) {
  if (!d.days.length) return "";
  return `<div class="card"><p class="card-title">Daily runs</p><div class="table-wrap"><table>
    <thead><tr><th>Day (close)</th><th>Bought</th><th>Shorted</th><th class="num">Fees</th><th class="num">Funding</th>
      <th class="num">Account after</th></tr></thead>
    <tbody>${d.days.map((x) => `<tr><td>${esc(date(x.day))}</td><td>${x.longs.map(esc).join(", ")}</td>
      <td>${x.shorts.map(esc).join(", ")}</td><td class="num">${money(x.fees_usd, 2)}</td>
      <td class="num">${signed(-x.funding_usd, money(-x.funding_usd, 2))}</td><td class="num">${money(x.equity)}</td></tr>`).join("")}</tbody>
  </table></div></div>`;
}

function lsTrades(d) {
  if (!d.trades.length) return "";
  return `<details class="card"><summary>Trades</summary><div class="table-wrap"><table>
    <thead><tr><th>When</th><th>Trade</th><th class="num">Amount</th><th class="num">Price</th><th class="num">Value</th><th class="num">Fee</th></tr></thead>
    <tbody>${d.trades.map((t) => `<tr><td>${esc(dateTime(t.ts))}</td>
      <td><span class="badge ${t.side === "buy" ? "buy" : "early"}">${t.side === "buy" ? "Buy" : "Sell"}</span> ${esc(t.coin)}</td>
      <td class="num">${num(t.qty)}</td><td class="num">${price(t.price)}</td><td class="num">${money(t.value_usd)}</td>
      <td class="num">${money(t.fee_usd, 2)}</td></tr>`).join("")}</tbody></table></div></details>`;
}

function lsRanking(d) {
  if (!d.ranking.length) {
    return `<div class="card"><p class="card-title">Today’s ranking</p><p class="muted">Appears after the first run.</p></div>`;
  }
  const longs = new Set(d.longs), shorts = new Set(d.shorts);
  const scores = d.ranking.map((r) => r[1]);
  const top = Math.max(...scores.map(Math.abs)) || 1;
  return `<div class="card">
    <div class="card-head"><p class="card-title">Today’s ranking</p><span class="spacer"></span>
      <span class="muted small">from the ${esc(date(d.ranking_day))} close</span></div>
    <div class="rank-list">${d.ranking.map(([coin, s]) => `<div class="rank-row">
      ${coinIcon(coin)}<span class="nm">${esc(coin)}</span>
      <span class="rank-bar ${s >= 0 ? "gain" : "loss"}"><span data-width="${(Math.abs(s) / top * 100).toFixed(1)}"></span></span>
      <span class="sc">${pct(s, 2)}</span>
      ${longs.has(coin) ? `<span class="badge buy">Long</span>` : shorts.has(coin) ? `<span class="badge short">Short</span>` : `<span class="badge none">–</span>`}
    </div>`).join("")}</div>
    <p class="muted small">The model’s expected move over the next 3 days compared with the average of these coins.
      The 25 most traded coins that can be shorted, best first.</p>
  </div>`;
}

function lsBacktest(bt) {
  if (!bt) {
    return `<div class="card"><p class="card-title">Backtest</p><p class="muted small">Not run yet on this machine:
      <code>python -m app.manage ls-backtest</code> (from the backend folder).</p></div>`;
  }
  const years = Object.entries(bt.by_year);
  return `<div class="card">
    <div class="card-head"><p class="card-title">Backtest of this exact code</p><span class="spacer"></span>
      <span class="muted small">${esc(date(bt.from))} ${new Date(bt.from * 1000).getFullYear()} – ${esc(date(bt.to))}</span></div>
    <div class="kpis">
      <div class="kpi"><small>Per year</small><b class="${bt.cagr >= 0 ? "up" : "down"}">${pct(bt.cagr, 0)}</b><span class="vs">BTC ${pct(bt.btc_cagr, 0)}</span></div>
      <div class="kpi"><small>Worst drop</small><b class="down">${pct(bt.max_drawdown, 0)}</b><span class="vs">BTC ${pct(bt.btc_max_drawdown, 0)}</span></div>
    </div>
    <div class="chart-head"><div class="chart-legend"><span class="key bot">Long/short</span><span class="key btc">Hold BTC</span></div>
      <span class="muted small">growth of $1,000, log scale</span></div>
    <div class="chart-wrap" id="ls-bt-chart"></div>
    <div class="years">${years.map(([y, v]) => `<div class="yr"><small>${esc(y)}</small><b>${signed(v, pct(v, 0))}</b></div>`).join("")}</div>
    <p class="note small">Walk-forward: a model trained only on earlier data every 3 months, trading exactly like the paper
      account (same picks, fees and funding) at daily closes. It trades about ${pct(bt.turnover_per_day, 0).replace("+", "")} of
      the account a day. A backtest is not a promise; the paper account is the real check.</p>
  </div>`;
}

function lsRules(d) {
  const r = d.rules, m = d.model;
  return `<div class="card"><p class="card-title">Rules</p>
    <div class="side-row"><span>Coins</span><b>${r.top_coins} most traded with a perpetual</b></div>
    <div class="side-row"><span>Long / short</span><b>best ${pct(r.fraction, 0).replace("+", "")} / worst ${pct(r.fraction, 0).replace("+", "")}</b></div>
    <div class="side-row"><span>Each side</span><b>half the account, equal parts</b></div>
    <div class="side-row"><span>Keeps a coin while it’s in the best (worst)</span><b>${fixed(r.keep, 1)} fifths</b></div>
    <div class="side-row"><span>Cost per trade</span><b>${fixed(r.cost * 100, 2)}% + funding</b></div>
    <div class="side-row"><span>Runs</span><b>daily, ${Math.round(r.run_after_s / 60)} min after 00:00 UTC</b></div>
    <div class="side-row"><span>Model</span><b>${m ? `trained ${esc(ago(m.trained_at))}` : "not trained yet"}</b></div>
    <p class="muted small">${r.features} signals per coin; retrained every ${r.retrain_days} days on all history since 2018
      (${m ? `${num(m.rows)} coin-days` : "…"}). Data: Binance, Bybit and Deribit, after each day’s close.</p>
  </div>`;
}

function lsRealMoney(d) {
  const days = d.account ? Math.floor((Date.now() / 1000 - d.account.started_at) / 86400) : 0;
  return `<div class="card">
    <div class="card-head"><p class="card-title">Real money</p><span class="spacer"></span><span class="pill">Off</span></div>
    <p class="muted small">This needs futures to short. Before it could trade your money:</p>
    <ul class="lock-list">
      <li class="done">Walk-forward backtest with fees and funding</li>
      <li class="${days >= 60 ? "done" : ""}">2–3 months of paper results close to the backtest (${Math.min(days, 60)} of 60 days)</li>
      <li>OKX futures access (X-Perps in Europe: an appropriateness test) and a trade-only key</li>
      <li>Your go-ahead, with a money limit and a stop-everything switch</li>
    </ul>
  </div>`;
}

function drawLsCharts(d) {
  const el = $("ls-chart");
  if (el && d.account) {
    const start = d.account.start_balance;
    const btc0 = d.history.find((h) => h[2])?.[2];
    lineChart(el, {
      series: [{ key: "bot", label: "Paper account", points: d.history.map((h) => [h[0], h[1]]) },
        { key: "btc", label: "Holding BTC", points: d.history.map((h) => [h[0], btc0 && h[2] ? start * (h[2] / btc0) : null]) }],
      fmtY: (v) => money(v, v < 1000 ? 2 : 0), baseline: start, label: "Paper account value over time compared with holding BTC",
    });
  }
  const bel = $("ls-bt-chart");
  if (bel && d.backtest) {
    const pts = d.backtest.points;
    lineChart(bel, {
      series: [{ key: "bot", label: "Long/short", points: pts.map((p) => [p[0], 1000 * p[1]]) },
        { key: "btc", label: "Hold BTC", points: pts.map((p) => [p[0], 1000 * p[2]]) }],
      log: true, H: 220, fmtY: (v) => money(v, 0), label: "Backtest: growth of $1,000 with the long/short test and holding BTC",
    });
  }
}

function renderLs(d) {
  lsState.data = d;
  const el = $("longshort");
  if (busyInside(el)) return;
  el.innerHTML = `<div class="bot-grid">
    <div class="bot-stack">${lsHero(d)}${lsPositions(d.account)}${lsLog(d)}${lsTrades(d)}</div>
    <div class="bot-stack">${lsRanking(d)}${lsBacktest(d.backtest)}${lsRules(d)}${lsRealMoney(d)}</div>
  </div>`;
  applyDynamicStyles(el);
  drawLsCharts(d);
  $("ls-reset")?.addEventListener("click", async (e) => {
    if (!confirm("Start the long/short paper test over? Its account, trades and chart are deleted (the final result is kept in the audit log). The next run opens a fresh account right away.")) return;
    try {
      renderLs(await withBusy(e.target, "Resetting…", () => post("/api/longshort/reset")));
      notice("Long/short test reset. It restarts within a minute.");
    } catch (err) { notice(err.message, "error"); }
  });
}

async function loadLs() {
  renderLs(await api("/api/longshort"));
}

PAGE_LOADERS.longshort = () => loadLs().catch((e) => { $("longshort").innerHTML = `<div class="empty">${esc(e.message)}</div>`; });
setInterval(() => { if (pageOpen("longshort")) loadLs().catch(() => {}); }, 30000);
