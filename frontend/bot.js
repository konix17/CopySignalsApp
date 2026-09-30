// Auto-trader page: the trend bot's demo account, today's signal, its backtest and trades. Uses common.js helpers.

const botState = { data: null, range: store.get("bot_range", "all") };
const COIN_NAME = { BTC: "Bitcoin", ETH: "Ethereum", USDT: "Cash (USDT)" };
const RANGES = { "1d": 86400, "1w": 7 * 86400, "1m": 30 * 86400, all: Infinity };

// ---------- Chart ----------
// Round axis values: 1-2-5 steps on a linear scale, 1-2-5 × 10^k on a log scale.
function niceTicks(lo, hi, log, want = 4) {
  if (log) {
    const out = [];
    for (let k = Math.floor(Math.log10(lo)); k <= Math.ceil(Math.log10(hi)); k++) {
      for (const m of [1, 2, 5]) { const v = m * 10 ** k; if (v >= lo && v <= hi) out.push(v); }
    }
    const step = Math.ceil(out.length / want);
    return out.filter((_, i) => i % step === 0);
  }
  const raw = (hi - lo) / want, mag = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 5, 10].map((m) => m * mag).find((x) => x >= raw);
  const out = [];
  for (let v = Math.ceil(lo / step) * step; v <= hi; v += step) out.push(v);
  return out;
}
const axisMoney = (v) => (Math.abs(v) >= 1e4 ? `$${fmt(v / 1000, { maximumFractionDigits: 1 })}k` : money(v, v < 100 ? 2 : 0));

// Line chart with hover read-out. `series`: [{ key, label, points: [[ts, value], ...] }]; the first gets a shaded area.
// Values are formatted with `fmtY`; `log` draws a log scale (for growth over years).
function lineChart(el, opts) {
  const { series, fmtY = (v) => money(v, 0), log = false, H = 240, baseline = null, label = "Chart" } = opts;
  el.chartOpts = opts;  // redrawn at the new width when the window is resized
  const W = Math.max(300, Math.round(el.clientWidth || 800)), PL = 4, PR = 66, PT = 12, PB = 26;
  const all = series.flatMap((s) => s.points.map((p) => p[1])).filter((v) => v != null && (!log || v > 0));
  if (!all.length || series[0].points.length < 2) {
    el.innerHTML = `<div class="empty">The chart fills in over time: one point an hour.</div>`;
    return;
  }
  const tf = log ? Math.log : (v) => v;
  let lo = Math.min(...all, baseline ?? Infinity), hi = Math.max(...all, baseline ?? -Infinity);
  if (hi === lo) { hi *= 1.01; lo *= 0.99; }
  const pad = (tf(hi) - tf(lo)) * 0.08;
  const y0 = tf(lo) - pad, y1 = tf(hi) + pad;
  const t0 = series[0].points[0][0], t1 = series[0].points.at(-1)[0];
  const x = (t) => PL + ((t - t0) / Math.max(1, t1 - t0)) * (W - PL - PR);
  const y = (v) => PT + (1 - (tf(v) - y0) / (y1 - y0)) * (H - PT - PB);
  const path = (pts) => pts.filter((p) => p[1] != null).map((p, i) => `${i ? "L" : "M"}${x(p[0]).toFixed(1)},${y(p[1]).toFixed(1)}`).join("");
  const inv = (u) => (log ? Math.exp(u) : u);
  const ticks = niceTicks(inv(y0), inv(y1), log);
  const first = series[0].points;
  const area = `${path(first)}L${x(first.at(-1)[0]).toFixed(1)},${H - PB}L${x(first[0][0]).toFixed(1)},${H - PB}Z`;
  const span = t1 - t0;
  const tlabel = (t) => span > 400 * 86400 ? new Date(t * 1000).getFullYear() : span > 2 * 86400 ? date(t)
    : new Date(t * 1000).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
  el.innerHTML = `
    <svg viewBox="0 0 ${W} ${H}" class="chart" role="img" aria-label="${esc(label)}">
      ${ticks.map((v) => `<line x1="${PL}" x2="${W - PR}" y1="${y(v).toFixed(1)}" y2="${y(v).toFixed(1)}" class="grid" />
        <text x="${W - PR + 8}" y="${(y(v) + 4).toFixed(1)}" class="axis">${esc(axisMoney(v))}</text>`).join("")}
      ${baseline != null ? `<line x1="${PL}" x2="${W - PR}" y1="${y(baseline).toFixed(1)}" y2="${y(baseline).toFixed(1)}" class="baseline" />` : ""}
      <path d="${area}" class="area" />
      ${series.slice(1).map((s) => `<path d="${path(s.points)}" class="line ${esc(s.key)}" />`).join("")}
      <path d="${path(first)}" class="line ${esc(series[0].key)}" />
      <text x="${PL}" y="${H - 6}" class="axis">${esc(tlabel(t0))}</text>
      <text x="${(PL + W - PR) / 2}" y="${H - 6}" class="axis mid">${esc(tlabel(t0 + span / 2))}</text>
      <text x="${W - PR}" y="${H - 6}" class="axis end">${esc(tlabel(t1))}</text>
      <line class="cross" y1="${PT}" y2="${H - PB}" x1="0" x2="0" visibility="hidden" />
      <circle class="dotp" r="0" cx="-10" cy="-10" />
      <rect x="${PL}" y="0" width="${W - PL - PR}" height="${H}" fill="transparent" class="hit" />
    </svg>
    <div class="chart-tip" hidden></div>`;
  const svg = el.querySelector("svg"), tip = el.querySelector(".chart-tip");
  const cross = svg.querySelector(".cross"), dot = svg.querySelector(".dotp");
  const move = (clientX) => {
    const r = svg.getBoundingClientRect();
    const sx = ((clientX - r.left) / r.width) * W;
    const t = t0 + ((sx - PL) / (W - PL - PR)) * (t1 - t0);
    let i = 0;
    while (i < first.length - 1 && Math.abs(first[i + 1][0] - t) <= Math.abs(first[i][0] - t)) i++;
    const [ti, vi] = first[i];
    cross.setAttribute("x1", x(ti)); cross.setAttribute("x2", x(ti)); cross.setAttribute("visibility", "visible");
    dot.setAttribute("cx", x(ti)); dot.setAttribute("cy", y(vi)); dot.setAttribute("r", 4.5);
    const when = span > 2 * 86400 ? new Date(ti * 1000).toLocaleDateString() : new Date(ti * 1000).toLocaleString();
    tip.innerHTML = `<b>${esc(when)}</b>` + series.map((s) => {
      const p = s.points.find((q) => q[0] === ti) || s.points[Math.min(i, s.points.length - 1)];
      return p && p[1] != null ? `<div><span class="muted">${esc(s.label)}</span><span>${esc(fmtY(p[1]))}</span></div>` : "";
    }).join("");
    tip.hidden = false;
    const px = (x(ti) / W) * r.width, tw = tip.offsetWidth;
    tip.style.transform = `translate(${Math.max(0, Math.min(r.width - tw, px - tw / 2))}px, ${((y(vi) / H) * r.height) - tip.offsetHeight - 14}px)`;
  };
  const hit = svg.querySelector(".hit");
  hit.addEventListener("mousemove", (e) => move(e.clientX));
  hit.addEventListener("touchmove", (e) => move(e.touches[0].clientX), { passive: true });
  const hide = () => { tip.hidden = true; cross.setAttribute("visibility", "hidden"); dot.setAttribute("r", 0); };
  hit.addEventListener("mouseleave", hide);
  hit.addEventListener("touchend", hide);
}

// ---------- Pieces ----------
const until = (ts) => {
  const s = Math.max(0, ts - Date.now() / 1000);
  const h = Math.floor(s / 3600), m = Math.round((s % 3600) / 60);
  return h ? `${h} h ${m} min` : `${m} min`;
};
const share = (v) => `${Math.round(v * 100)}%`;
const qty = (v) => fmt(v, { maximumSignificantDigits: 6 });

function signalCard(sig, error) {
  if (!sig) {
    return `<div class="card"><p class="card-title">Today’s signal</p><p class="muted">${esc(error || "Loading OKX daily prices…")}</p></div>`;
  }
  const rows = Object.entries(sig.coins).map(([coin, c]) => {
    const avgs = Object.entries(c.averages);
    return `<div class="signal-row">
      <span class="coin-ic c-${esc(coin)}" aria-hidden="true">${esc(coin.slice(0, 1))}</span>
      <div><b>${esc(COIN_NAME[coin] || coin)}</b> <span class="muted small">${esc(price(c.price))}</span>
        <div class="checks-mini">${avgs.map(([n, m]) => `<span class="${c.price > m ? "yes" : ""}" title="${esc(n)}-day average ${esc(price(m))}">
          ${c.price > m ? "✓" : "✕"} ${esc(n)}d</span>`).join("")}
          ${c.funding == null ? "" : `<span class="${c.funding_floor ? "yes" : ""}" title="Average futures funding over 3 days, per 8 hours">
            funding ${pct(c.funding, 3)}</span>`}</div>
        ${c.funding_floor && c.trend_weight < c.weight - 1e-9 ? `<div class="muted small">Futures traders are betting against it: holding a third anyway</div>` : ""}</div>
      <div class="target"><b>${share(c.weight)}</b><small>of the account</small></div>
    </div>`;
  }).join("");
  const cash = 1 - Object.values(sig.weights).reduce((a, b) => a + b, 0);
  return `<div class="card">
    <div class="card-head"><p class="card-title">Today’s signal</p><span class="spacer"></span>
      <span class="muted small">from the ${esc(date(sig.close_of))} close</span></div>
    ${rows}
    <div class="side-row"><span>Cash (USDT)</span><b>${share(cash)}</b></div>
    <p class="muted small">Each coin gets half the account, filled by how many of its 50-, 100- and 150-day average prices
      it’s above; below all three, that half sits in cash. When its futures funding is zero or negative (traders are paying
      to bet against it), at least a third of the half is held: crowded bets against a coin tend to get squeezed.</p>
  </div>`;
}

function holdingsCard(acct) {
  const parts = [...acct.positions.map((p) => ({ coin: p.coin, value: p.value, weight: p.weight, p })),
    { coin: "USDT", value: acct.cash, weight: acct.total ? acct.cash / acct.total : 1 }];
  const targets = acct.targets || {};
  return `<div class="card">
    <div class="card-head"><p class="card-title">Holdings</p></div>
    <div class="alloc-bar">${parts.filter((x) => x.weight > 0.001).map((x) => `<span class="c-${esc(x.coin)}" data-width="${(x.weight * 100).toFixed(2)}"></span>`).join("")}</div>
    ${parts.map((x) => `<div class="holding">
      <span class="sw c-${esc(x.coin)}"></span>
      <span class="nm">${esc(x.coin === "USDT" ? "Cash" : x.coin)}<small>${share(x.weight)}${x.coin !== "USDT" && targets[x.coin] != null ? ` · target ${share(targets[x.coin])}` : ""}</small></span>
      <span class="vl">${money(x.value)}</span>
      ${x.p ? `<span class="meta"><span>${esc(qty(x.p.qty))} ${esc(x.coin)} at ${esc(price(x.p.price))}</span>${signed(x.p.pnl, money(x.p.pnl))}</span>` : ""}
    </div>`).join("")}
  </div>`;
}

function backtestCard(bt) {
  if (!bt) return "";
  const s = bt.strategy, b = bt.hold.BTC, e = bt.hold.ETH;
  const years = Object.keys(s.by_year);
  return `<div class="card">
    <div class="card-head"><p class="card-title">How the strategy did since ${esc(new Date(bt.from * 1000).getFullYear())}</p>
      <span class="spacer"></span><span class="muted small">backtest on OKX daily prices, ${fixed(bt.cost * 100, 2)}% cost per trade</span></div>
    <div class="kpis">
      <div class="kpi"><small>Per year</small><b class="up">${pct(s.cagr, 0)}</b><span class="vs">BTC ${pct(b.cagr, 0)} · ETH ${pct(e.cagr, 0)}</span></div>
      <div class="kpi"><small>Worst drop</small><b class="down">${pct(s.max_drawdown, 0)}</b><span class="vs">BTC ${pct(b.max_drawdown, 0)} · ETH ${pct(e.max_drawdown, 0)}</span></div>
      <div class="kpi"><small>$1,000 became</small><b>${money(1000 * (1 + s.total), 0)}</b><span class="vs">holding BTC: ${money(1000 * (1 + b.total), 0)}</span></div>
      <div class="kpi"><small>Time invested</small><b>${share(s.invested)}</b><span class="vs">${fixed(s.entries / s.years, 0)} buys a year</span></div>
    </div>
    <div class="chart-head"><div class="chart-legend"><span class="key bot">Trend bot</span><span class="key btc">Hold BTC</span><span class="key eth">Hold ETH</span></div>
      <span class="muted small">growth of $1,000, log scale</span></div>
    <div class="chart-wrap" id="bt-chart"></div>
    <div class="years">${years.map((y) => `<div class="yr"><small>${esc(y)}</small><b>${signed(s.by_year[y], pct(s.by_year[y], 0))}</b>
      <span class="vs">BTC ${pct(b.by_year[y], 0)}</span></div>`).join("")}</div>
    <p class="note small">A backtest shows what the rules would have done, not what they will do. Crypto has had big bull runs
      since 2018; the bot mostly helps by stepping aside in long downtrends, and it still loses money in choppy years.</p>
  </div>`;
}

function tradesCard(trades) {
  if (!trades.length) return "";
  return `<div class="card"><div class="card-head"><p class="card-title">Trades</p></div>
    <div class="table-wrap"><table>
      <thead><tr><th>When</th><th>Trade</th><th class="num">Amount</th><th class="num">Price</th><th class="num">Value</th><th class="num">Fee</th></tr></thead>
      <tbody>${trades.map((t) => `<tr><td>${esc(dateTime(t.ts))}</td>
        <td><span class="badge ${t.side === "buy" ? "buy" : "early"}">${t.side === "buy" ? "Buy" : "Sell"}</span> ${esc(t.coin)}</td>
        <td class="num">${esc(qty(t.qty))}</td><td class="num">${esc(price(t.price))}</td>
        <td class="num">${money(t.value_usd)}</td><td class="num">${money(t.fee_usd)}</td></tr>`).join("")}</tbody>
    </table></div></div>`;
}

function realMoneyCard(acct) {
  const days = acct ? Math.floor((Date.now() / 1000 - acct.started_at) / 86400) : 0;
  return `<div class="card">
    <div class="card-head"><p class="card-title">Real money</p><span class="spacer"></span><span class="pill">Off</span></div>
    <p class="muted small">The bot trades demo money only. Before it can trade your own money on OKX:</p>
    <ul class="lock-list">
      <li class="done">Rules tested on every day since 2018, fees included</li>
      <li class="${days >= 30 ? "done" : ""}">30 days of demo trading that behave like the backtest (${Math.min(days, 30)} of 30)</li>
      <li>An OKX key that can trade but never withdraw, locked to this computer’s IP</li>
      <li>Your go-ahead, with a money limit and a stop-everything switch</li>
    </ul>
  </div>`;
}

function startCard(d) {
  const s = d.backtest?.strategy, b = d.backtest?.hold.BTC;
  return `<div class="card start-card">
    <div>
      <span class="pill demo">Demo money</span>
      <h2>Let the trend bot trade for you</h2>
      <p class="muted">It holds Bitcoin and Ethereum while they trend up and steps into cash when they don’t. One check a day,
        right after the daily close, with your OKX fees on every trade.</p>
      ${s ? `<ul>
        <li>Since ${new Date(d.backtest.from * 1000).getFullYear()}: <b class="up">${pct(s.cagr, 0)} a year</b>, holding BTC made ${pct(b.cagr, 0)}</li>
        <li>Worst drop <b>${pct(s.max_drawdown, 0)}</b>, holding BTC fell ${pct(b.max_drawdown, 0)}</li>
        <li>About ${fixed(s.entries / s.years, 0)} buys a year, no leverage, spot only</li></ul>` : ""}
    </div>
    <form class="start-form" id="bot-start">
      <label>Demo money to start with <input type="number" name="balance" min="100" step="100" value="${Math.round(d.default_balance || 10000)}" required /></label>
      <button class="btn big" type="submit">Start the bot</button>
      <span class="form-msg" id="bot-start-msg" role="status"></span>
      <span class="muted small">If BTC or ETH is trending up today it buys right away. Pause or reset any time.</span>
    </form>
  </div>`;
}

function heroCard(d) {
  const a = d.account;
  const running = a.enabled;
  const fees = d.trades.reduce((sum, t) => sum + t.fee_usd, 0);
  return `<div class="card hero">
    <div class="hero-top">
      <span class="pill ${running ? "run" : "pause"}">${running ? "Running" : "Paused"}</span><span class="pill demo">Demo</span>
      <span class="muted small">${esc(d.strategy.name)} · started ${esc(ago(a.started_at))}</span>
      <span class="hero-actions">
        <button type="button" class="btn ghost small" id="bot-toggle">${running ? "Pause" : "Resume"}</button>
        <button type="button" class="btn danger ghost small" id="bot-reset">Reset</button>
      </span>
    </div>
    <div class="value">${money(a.total, 2)}</div>
    <div class="value-sub"><span class="chg ${a.pnl >= 0 ? "up" : "down"}">${pct(a.pnl_pct, 2)}</span>${signed(a.pnl, money(a.pnl, 2))}
      <span class="muted">since start${a.btc_return != null ? ` · BTC ${pct(a.btc_return, 2)} over the same time` : ""}</span></div>
    <div class="stats">
      <div class="stat"><small>Invested</small><b>${money(a.invested)}</b></div>
      <div class="stat"><small>Cash</small><b>${money(a.cash)}</b></div>
      <div class="stat"><small>Fees paid</small><b>${money(fees, 2)}</b></div>
      <div class="stat"><small>Next check</small><b>${!running ? "paused" : a.last_run_at ? `in ${esc(until(d.next_check))}` : "starting now…"}</b></div>
    </div>
    <div class="chart-head"><div class="chart-legend"><span class="key bot">Bot</span><span class="key btc">Holding BTC instead</span></div>
      <div class="seg" id="bot-range" role="group" aria-label="Chart range">${Object.keys(RANGES).map((r) =>
        `<button type="button" data-r="${r}" class="${botState.range === r ? "on" : ""}" aria-pressed="${botState.range === r}">${r.toUpperCase()}</button>`).join("")}</div></div>
    <div class="chart-wrap" id="bot-chart"></div>
  </div>`;
}

// ---------- Page ----------
function drawAccountChart(d) {
  const el = $("bot-chart");
  if (!el) return;
  const cut = Date.now() / 1000 - RANGES[botState.range];
  const rows = d.history.filter((h) => h[0] >= cut);
  const start = d.account.start_balance;
  const btc0 = d.history.find((h) => h[2])?.[2];
  lineChart(el, {
    series: [
      { key: "bot", label: "Bot", points: rows.map((h) => [h[0], h[1]]) },
      { key: "btc", label: "Holding BTC", points: rows.map((h) => [h[0], btc0 && h[2] ? start * (h[2] / btc0) : null]) },
    ],
    fmtY: (v) => money(v, v < 1000 ? 2 : 0), baseline: start, label: "Bot account value over time compared with holding BTC",
  });
}

function drawBacktestChart(bt) {
  const el = $("bt-chart");
  if (!el || !bt) return;
  const col = (i) => bt.points.map((p) => [p[0], 1000 * p[i]]);
  lineChart(el, {
    series: [{ key: "bot", label: "Trend bot", points: col(1) }, { key: "btc", label: "Hold BTC", points: col(2) },
      { key: "eth", label: "Hold ETH", points: col(3) }],
    log: true, H: 260, fmtY: (v) => money(v, 0), label: "Backtest: growth of $1,000 with the bot, holding BTC and holding ETH",
  });
}

function renderBot(d) {
  botState.data = d;
  const el = $("bot");
  if (busyInside(el)) return;
  if (!d.account) {
    el.innerHTML = startCard(d) + `<div class="bot-grid"><div class="bot-stack">${backtestCard(d.backtest)}</div>
      <div class="bot-stack">${signalCard(d.signal, d.error)}${realMoneyCard(null)}</div></div>`;
  } else {
    el.innerHTML = `<div class="bot-grid">
      <div class="bot-stack">${heroCard(d)}${backtestCard(d.backtest)}${tradesCard(d.trades)}</div>
      <div class="bot-stack">${holdingsCard(d.account)}${signalCard(d.signal, d.error)}${realMoneyCard(d.account)}
        <div class="card"><p class="card-title">Costs</p>
          <div class="side-row"><span>Fee per trade (your OKX taker fee)</span><b>${fixed(d.fee_rate * 100, 2)}%</b></div>
          <div class="side-row"><span>Slippage allowed for</span><b>${fixed(d.slippage * 100, 2)}%</b></div>
          <div class="side-row"><span>Smallest rebalance</span><b>1% of the account</b></div>
        </div></div>
    </div>`;
    drawAccountChart(d);
  }
  applyDynamicStyles(el);
  drawBacktestChart(d.backtest);
  bindBot(d);
}

function bindBot(d) {
  $("bot-start")?.addEventListener("submit", async (e) => {
    e.preventDefault();
    const balance = parseNum(e.target.balance.value);
    if (!(balance >= 100)) return formMessage($("bot-start-msg"), "Start with at least $100 of demo money.");
    try {
      const out = await withBusy(e.target.querySelector("[type=submit]"), "Starting…", () => post("/api/bot/start", { balance }));
      document.activeElement?.blur();
      renderBot(out);
      notice("The trend bot is running. Its first check happens in a few seconds.");
      setTimeout(() => loadBot().catch(() => {}), 4000);
    } catch (err) { formMessage($("bot-start-msg"), err.message); }
  });
  $("bot-toggle")?.addEventListener("click", async (e) => {
    try {
      renderBot(await withBusy(e.target, "Saving…", () => put("/api/bot/enabled", { enabled: !d.account.enabled })));
      notice(d.account.enabled ? "Bot paused: it keeps what it holds and makes no trades." : "Bot running again.");
    } catch (err) { notice(err.message, "error"); }
  });
  $("bot-reset")?.addEventListener("click", async (e) => {
    if (!confirm("Reset the bot? Its demo account, trades and chart are deleted. (The final result is kept in the audit log.)")) return;
    try {
      renderBot(await withBusy(e.target, "Resetting…", () => post("/api/bot/reset")));
      notice("Bot reset.");
    } catch (err) { notice(err.message, "error"); }
  });
  $("bot-range")?.addEventListener("click", (e) => {
    const b = e.target.closest("button[data-r]");
    if (!b) return;
    botState.range = b.dataset.r;
    store.set("bot_range", botState.range);
    $("bot-range").querySelectorAll("button").forEach((x) => {
      x.classList.toggle("on", x === b);
      x.setAttribute("aria-pressed", String(x === b));
    });
    drawAccountChart(botState.data);
  });
}

async function loadBot() {
  renderBot(await api("/api/bot"));
}

PAGE_LOADERS.bot = () => loadBot().catch((e) => { $("bot").innerHTML = `<div class="empty">${esc(e.message)}</div>`; });
// Live value while the page is open.
setInterval(() => {
  if (!document.querySelector('.page[data-page="bot"]').hidden && !document.hidden) loadBot().catch(() => {});
}, 10000);

let chartResize;
window.addEventListener("resize", () => {
  clearTimeout(chartResize);
  chartResize = setTimeout(() => document.querySelectorAll(".chart-wrap").forEach((el) => {
    if (el.chartOpts && el.offsetParent) lineChart(el, el.chartOpts);
  }), 150);
});
