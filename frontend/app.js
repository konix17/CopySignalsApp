// Main app: picks, rising now, portfolio (real or demo), track record. Helpers are in common.js.
const state = { status: null, account: null, portfolio: null, demo: null, bankroll: null, rising: null };
const demoMode = () => Boolean(state.status?.demo_mode);

// ---------- Pick cards (Picks and Rising now) ----------
function pickCard(p, i) {
  const connected = state.status?.account.ok;
  const early = p.strength === "Early" || p.strength === "Pump";
  const pump = p.strength === "Pump";
  const passed = p.checks.filter((c) => c.passed).length;
  const copy = p.strength === "Copy";
  const grade = pump ? "Pump starting" : early ? "Early mover" : copy ? "Swing copy" : p.strength;
  const sub = early ? `Score ${Math.round(p.score)}${p.flag ? ` · flagged ${ago(p.flag.flagged_at)}` : ""}`
    : copy ? `copying one trader` : `${passed} of 3 checks passed`;
  // The ✓/✕ symbol and colour are hidden from screen readers, which get the word instead.
  const mark = (c) => `<span class="mark" aria-hidden="true">${c.passed ? "✓" : early ? "!" : "✕"}</span>`
    + `<span class="sr-only">${c.passed ? "Passed: " : early ? "Caution: " : "Failed: "}</span>`;
  // In demo mode, amounts are sized to the demo account instead of your real bankroll.
  const shown = demoMode() ? demoAmount(p) : p.size_usd;
  const k = p.size_usd ? shown / p.size_usd : 1;
  return `
    <article class="pick ${early ? "early" : ""}" data-i="${i}">
      <div class="pick-head">${coinIcon(p.symbol)}
        <div class="pick-title"><span class="sym">${esc(p.symbol)}</span><span class="muted small">${esc(sub)}</span></div>
        <span class="grade g-${esc(p.strength)}">${esc(grade)}</span></div>
      <div class="pick-amount">
        <div><small>${demoMode() ? "Demo buy" : "Buy"}</small><b>${money(shown, 0)}</b></div>
        <div class="right"><small>${copy ? "Hold" : "Hold about"}</small><b>${copy ? "while they do" : days(p.hold_days)}</b></div></div>
      <div class="levels">
        <div><small>Buy around</small><b>${price(p.price)}</b></div>
        <div><small>${pump ? "Trailing stop" : copy ? "Safety stop" : "Stop-loss"}</small><b class="down">${pump ? `${Math.round(p.trail_pct * 100)}% below top` : price(p.stop_price)}</b>
          <span class="muted">${pump ? `starts ${price(p.stop_price)}` : pct(p.stop_pct)}</span></div>
        ${copy ? `<div><small>Sell when</small><b>the trader sells</b><span class="muted">checked every minute</span></div>`
          : `<div><small>Target</small><b class="up">${price(p.target_price)}</b><span class="muted">${pct(p.target_pct)}</span></div>`}
      </div>
      <ul class="checks">${p.checks.map((c) => `
        <li class="${c.passed ? "pass" : early ? "warn" : "fail"}">${mark(c)}
          <span><span class="name">${esc(c.name)}</span><span class="detail">${esc(c.detail)}</span></span></li>`).join("")}
      </ul>
      ${early && p.flag ? `<div class="since">Since flagged at ${price(p.flag.flag_price)}: ${signed(p.flag.since_flag, pct(p.flag.since_flag))}</div>` : ""}
      ${p.notes.length ? `<ul class="pick-notes">${p.notes.map((n) => `<li>${esc(n)}</li>`).join("")}</ul>` : ""}
      <div class="net"><span class="muted">After fees</span>${copy ? `<span>worst case ${signed(-1, money(p.net_loss_usd * k))} at the safety stop</span>`
        : `<span>win ${signed(1, money(p.net_win_usd * k))}</span><span>lose ${signed(-1, money(p.net_loss_usd * k))}</span>`}${early && !demoMode() ? `<span class="muted">high-risk budget</span>` : ""}</div>
      <div class="actions">
        ${demoMode()
          ? (p.demo_running ? `<span class="held">✓ Demo trade running</span>` : `<button class="btn small" data-act="demo">Demo buy</button>`)
          : (p.held ? `<span class="held">✓ In your portfolio</span>` : `
            <button class="btn small" data-act="ticket">How to buy on ${EX()}</button>
            ${connected ? "" : `<button class="btn small ghost" data-act="bought">I bought it</button>`}`)}
      </div>
    </article>`;
}

function bindCards(container, list) {
  container.querySelectorAll("[data-act]").forEach((b) => b.addEventListener("click", () => {
    const card = b.closest(".pick");
    const p = list[+card.dataset.i];
    if (b.dataset.act === "ticket") toggleTicket(card, p);
    if (b.dataset.act === "bought") openBuyForm(card, p);
    if (b.dataset.act === "demo") openDemoForm(card, p);
  }));
}

function renderPicks(data) {
  const r = data.regime;
  $("regime").className = `regime ${r.risk_on ? "on" : "off"}`;
  $("regime").textContent = r.text;
  const copies = data.copies || [];
  $("copies-section").hidden = !copies.length;
  if (!busyInside($("copies-section"))) {
    $("copies").innerHTML = copies.map(pickCard).join("");
    bindCards($("copies"), copies);
  }
  if (busyInside($("picks"))) return;
  if (!data.picks.length) {
    $("picks").innerHTML = `<div class="empty">No coins pass the checks right now. That’s normal, because the app only shows setups where
      proven traders, the price trend and positioning line up. New picks can appear any minute.</div>`;
    return;
  }
  $("picks").innerHTML = data.picks.map(pickCard).join("");
  bindCards($("picks"), data.picks);
}

// ---------- Rising now ----------
const MOVER_NOTIFY_SCORE = 70;

const PHASE = { starting: "Pump starting", running: "Pump running", topping: "Topping out", dumping: "Dumping" };

function renderBudget(b) {
  if (document.activeElement?.id === "budget-input") return;  // don't redraw while typing
  const pctOf = b.amount ? Math.round((b.risk_budget / b.amount) * 100) : 0;
  const used = b.risk_budget ? Math.min(1, b.risk_budget_used / b.risk_budget) : 0;
  $("budget").innerHTML = `
    <div class="budget">
      <div class="budget-row">
        <label><b>High-risk budget</b> $<input type="number" id="budget-input" min="0" step="10" value="${Math.round(b.risk_budget)}" /></label>
        <span class="muted">${pctOf}% of your ${money(b.amount, 0)} bankroll · each trade uses ${money(b.per_trade, 0)}</span>
      </div>
      <div class="meter"><div data-width="${(used * 100).toFixed(1)}"></div></div>
      <div class="muted small">${money(b.risk_budget_used)} in open risky trades · ${money(b.risk_budget_free)} free</div>
    </div>`;
  applyDynamicStyles($("budget"));
  $("budget-input").addEventListener("change", async (e) => {
    const v = +e.target.value;
    if (!(v >= 0)) return notice("Enter a budget of $0 or more.", "error");
    try {
      await put("/api/settings/prefs", { risk_budget: v });
      notice("High-risk budget saved.");
    } catch (err) { return notice(err.message, "error"); }
    e.target.blur();
    loadAll();
  });
}

function renderRising(data) {
  state.rising = data;
  renderBudget(data.budget);
  $("avoid-box").hidden = !data.avoid.length;
  $("avoid").innerHTML = data.avoid.map((a) => `
    <div class="avoid-item ${esc(a.phase)}"><b>${esc(a.coin)}</b> <span class="phase">${PHASE[a.phase]}</span>
      <span class="muted">${esc(a.summary)}. Don’t buy; sell if you hold it.</span></div>`).join("");
  const n = data.rising.length;
  document.querySelectorAll("#rising-count, .rising-count-m").forEach((el) => { el.hidden = !n; el.textContent = n; });
  $("scan-status").textContent = data.scanned_at ? `Last scan ${ago(data.scanned_at)} · scans every 15 seconds` : "The first scan runs about 20 seconds after the app starts.";
  // Don't wipe a half-filled demo form or an open order ticket.
  if (!$("rising").querySelector(".buy-form, .ticket")) {
    $("rising").innerHTML = n ? data.rising.map(pickCard).join("")
      : `<div class="empty">Nothing is breaking out right now. Coins show up here within seconds of starting to rise.</div>`;
    bindCards($("rising"), data.rising);
  }
  const e = data.earlier;
  $("rising-earlier-box").hidden = !e.length;
  $("rising-earlier").innerHTML = e.length ? `<div class="table-wrap"><table>
    <thead><tr><th>Coin</th><th>Flagged</th><th>Now</th><th class="num">Score</th><th class="num">Price then</th><th class="num">Now</th><th class="num">Since flag</th><th class="num">Best since flag</th></tr></thead>
    <tbody>${e.map((x) => `<tr><td>${esc(x.symbol)}</td><td>${ago(x.flagged_at)}</td><td>${x.pump ? esc(PHASE[x.pump.phase]) : "–"}</td><td class="num">${Math.round(x.score)}</td>
      <td class="num">${price(x.flag_price)}</td><td class="num">${price(x.last_price)}</td>
      <td class="num">${signed(x.since_flag, pct(x.since_flag))}</td><td class="num">${signed(x.best_since_flag, pct(x.best_since_flag))}</td></tr>`).join("")}
    </tbody></table></div>` : "";

  // Desktop notification for strong new flags.
  const seen = new Set(store.get("movers_seen", []));
  const fresh = data.rising.filter((p) => !seen.has(`${p.symbol}:${p.flag.flagged_at}`));
  if ("Notification" in window && Notification.permission === "granted") {
    fresh.filter((p) => p.strength === "Pump" || p.score >= MOVER_NOTIFY_SCORE).forEach((p) =>
      new Notification(`${p.strength === "Pump" ? "Pump starting" : "Rising"}: ${p.symbol}`, { body: `${p.checks[0].detail}. ${p.checks[1].detail}.` }));
  }
  fresh.forEach((p) => seen.add(`${p.symbol}:${p.flag.flagged_at}`));
  store.set("movers_seen", [...seen].slice(-300));
}

async function loadRising() {
  renderRising(await api("/api/movers"));
}

// OKX order steps: market buy, then a TP/SL (take-profit + stop-loss) order, or a trailing stop for pump rides.
function ticketSteps(p) {
  const qty = Math.round(p.size_usd);
  const trail = p.trail_pct ? Math.round(p.trail_pct * 100) : null;
  return `
    <li>Open <a href="${esc(p.trade_url)}" target="_blank" rel="noopener">${esc(p.symbol)}/USDT on OKX</a> (Spot).</li>
    <li>Buy → <b>Market</b> → amount in USDT <code>${qty}</code> ${copyButton(qty)}</li>
    ${trail ? `
    <li>Protect it: Sell → order type <b>Trailing stop</b>, amount = all the ${esc(p.symbol)} you just bought.
      <div>Callback rate <code>${trail}%</code> ${copyButton(trail)}</div>
      <div class="muted">Watch for the app’s sell alert too: pumps can collapse faster than a stop fills.</div>
    </li>` : `
    <li>Protect it: Sell → order type <b>TP/SL</b>, amount = all the ${esc(p.symbol)} you just bought.
      <div>TP trigger price <code>${orderPrice(p.target_price)}</code> ${copyButton(orderPrice(p.target_price))}</div>
      <div>SL trigger price <code>${orderPrice(p.stop_price)}</code> ${copyButton(orderPrice(p.stop_price))}</div>
      <div class="muted">Leave both order prices on “Market” so they sell straight away when triggered.</div>
    </li>`}`;
}

function toggleTicket(card, p) {
  const existing = card.querySelector(".ticket");
  if (existing) return existing.remove();
  const connected = state.status?.account.ok;
  const t = document.createElement("div");
  t.className = "ticket";
  t.innerHTML = `
    <ol>${ticketSteps(p)}</ol>
    <div class="muted">You place the order yourself. The app never sees your password or funds.
      ${connected ? "Once you’ve bought, it shows up in your portfolio after the next sync." : ""}</div>`;
  card.querySelector(".actions").before(t);
}

// Real <form>s, so Enter submits; the button is disabled while saving and errors show inline.
function openBuyForm(card, p) {
  if (card.querySelector(".buy-form")) return card.querySelector(".buy-form input").focus();
  const form = document.createElement("form");
  form.className = "buy-form";
  form.noValidate = true;
  form.innerHTML = `
    <label>Amount $ <input type="number" name="size" min="1" step="any" value="${Math.round(p.size_usd)}" /></label>
    <label>Bought at <input type="text" name="price" inputmode="decimal" autocomplete="off" spellcheck="false"
      value="${orderPrice(p.price)}" /></label>
    <button class="btn small" type="submit">Save</button>
    <button class="btn small ghost" type="button" data-act="cancel">Cancel</button>
    <span class="form-msg" role="status"></span>`;
  card.querySelector(".actions").before(form);
  form.size.focus();
  form.querySelector("[data-act=cancel]").addEventListener("click", () => form.remove());
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const msg = form.querySelector(".form-msg");
    const size = +form.size.value;
    const entry = parseNum(form.price.value);
    if (!(size > 0)) { formMessage(msg, "Enter the amount you spent, above $0."); return form.size.focus(); }
    if (!(entry > 0)) { formMessage(msg, "Enter the price you paid, for example 1.23 or 1,23."); return form.price.focus(); }
    try {
      await withBusy(form.querySelector("[type=submit]"), "Saving…",
        () => post("/api/positions", { symbol: p.symbol, size_usd: size, entry_price: entry }));
    } catch (err) { return formMessage(msg, err.message); }
    await loadAll();
  });
}

function openDemoForm(card, p) {
  if (card.querySelector(".demo-form")) return card.querySelector(".demo-form input").focus();
  const form = document.createElement("form");
  form.className = "buy-form demo-form";
  form.noValidate = true;
  form.innerHTML = `
    <label>Demo amount $ <input type="number" name="size" min="1" step="any" value="${demoAmount(p)}" /></label>
    <span class="muted">${money(state.demo?.account.cash ?? 0, 0)} demo cash available</span>
    <button class="btn small" type="submit">Start demo</button>
    <button class="btn small ghost" type="button" data-act="cancel">Cancel</button>
    <span class="form-msg" role="status"></span>`;
  card.querySelector(".actions").before(form);
  form.size.focus();
  form.querySelector("[data-act=cancel]").addEventListener("click", () => form.remove());
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const msg = form.querySelector(".form-msg");
    const size = +form.size.value;
    if (!(size > 0)) { formMessage(msg, "Enter an amount above $0."); return form.size.focus(); }
    try {
      await withBusy(form.querySelector("[type=submit]"), "Starting…",
        () => post("/api/demo", { symbol: p.symbol, size_usd: size }));
    } catch (err) {
      return formMessage(msg, err.message || "Couldn’t start the demo. Refresh the page and try again.");
    }
    await loadAll();
    go("portfolio");
    notice(`Demo trade for ${p.symbol} started.`);
  });
}

// ---------- Shared: account panel + trade rows (used by your portfolio and the demo account) ----------
const REASON_TEXT = { stop: "stop-loss hit", target: "take-profit reached", time: "hold time over", trend: "uptrend broke",
  exited: "top traders left", flipped: "top traders turned against it", selling: "top traders sold",
  sold: "sold", manual: "sold", pump_fading: "pump topped out", pump_dump: "pump dumping" };
const checkLabel = (c) => ({ ok: ["ok", `✓ Matches ${EX()}`], qty_mismatch: ["bad", `Amount differs from ${EX()}`],
  missing: ["bad", `Not found on ${EX()}`] })[c];
const ADVICE_LABEL = { SELL: "Sell now", TAKE_PROFIT: "Take profit", WATCH: "Check", HOLD: "Hold" };

const tile = (label, value, sub = "") => `<div class="tile"><small>${label}</small><b>${value}</b>${sub ? `<span class="sub">${sub}</span>` : ""}</div>`;
const isLive = (ts) => ts && Date.now() / 1000 - ts < 60;

const stat = (label, value, sub = "") => `<div class="stat"><small>${label}</small><b>${value}</b>${sub ? `<span class="sub">${sub}</span>` : ""}</div>`;

function accountPanel({ label, value, headline, a, live, extraTiles = [], footer = "" }) {
  const running = a.running;
  return `
    <div class="card hero account-hero">
      <div class="hero-top"><p class="card-title">${label}</p><span class="spacer"></span>
        ${running ? `<span class="pill ${live ? "run" : ""}">${live ? "Live prices" : "Waiting for prices…"}</span>` : ""}</div>
      <div class="value">${money(value, 2)}</div>
      ${headline ? `<div class="value-sub">${headline}</div>` : ""}
      <div class="stats">
        ${extraTiles.join("")}
        ${stat("Invested now", money(a.invested), running ? `${running} position${running === 1 ? "" : "s"} open` : "")}
        ${stat("Open result", running ? signed(a.open_result, money(a.open_result)) : "–", "after fees, live")}
        ${stat("If all targets hit", running ? signed(a.best_case, money(a.best_case)) : "–")}
        ${stat("If all stops hit", running ? signed(a.worst_case, money(a.worst_case)) : "–")}
        ${stat("Finished trades", a.finished ? `${a.successful} of ${a.finished} won` : "–",
               a.finished ? signed(a.realized, money(a.realized) + " total") : "")}
      </div>
      ${footer}
    </div>`;
}

function rangeBar(t) {
  const stop = t.stop_now ?? t.stop_price;
  const progress = Math.min(1, Math.max(0, (t.last_price - stop) / (t.target_price - stop)));
  const label = t.trail_pct ? `Trailing stop ${price(stop)}` : `Stop ${price(stop)}`;
  return `
    <div class="range" title="Where the price is between the stop-loss and the take-profit">
      <span class="${t.if_stop_usd >= 0 ? "up" : "down"}">${label}<br>${signed(t.if_stop_usd, money(t.if_stop_usd))}</span>
      <div class="bar"><div class="marker" data-left="${(progress * 100).toFixed(1)}"></div></div>
      <span class="up">Target ${price(t.target_price)}<br>+${money(t.if_target_usd)}</span>
    </div>`;
}

function openRow(t, { tags = "", actions = "", status = "", extraDetails = "" }) {
  const now = Date.now() / 1000;
  const planned = (t.hold_until - t.opened_at) / 86400;
  const day = Math.min(Math.ceil((now - t.opened_at) / 86400) || 1, Math.ceil(planned));
  return `
    <div class="pos ${t.source === "demo" ? "demo" : esc(t.advice)}" data-id="${t.id}">
      <div class="pos-row">
        ${coinIcon(t.symbol)}<span class="sym">${esc(t.symbol)}</span>${t.style === "pump" ? `<span class="tag pump">pump ride</span>` : t.style === "early" ? `<span class="tag">early</span>` : ""}${tags}
        <span class="pnl">${signed(t.net_now, `${pct(t.net_now, 2)} (${money((t.net_now || 0) * t.size_usd)})`)}</span>
        <span class="grow"></span>
        <span class="muted">day ${day} of ${Math.ceil(planned)}</span>
        ${status}${actions}
      </div>
      ${rangeBar(t)}
      <div class="pos-details">${money(t.size_usd)} bought at ${price(t.entry_price)} → now ${price(t.last_price)} · planned until ${date(t.hold_until)}${extraDetails}</div>
      ${t.advice_reasons.length ? `<ul class="why">${t.advice_reasons.map((r) => `<li>${esc(r)}</li>`).join("")}</ul>` : ""}
    </div>`;
}

function closedRow(t, { actions = "" } = {}) {
  if (t.net_return == null) {  // closed before results were recorded
    return `<div class="pos closed" data-id="${t.id}"><div class="pos-row">${coinIcon(t.symbol)}<span class="sym">${esc(t.symbol)}</span>
      <span class="muted">closed ${date(t.closed_at)}</span><span class="grow"></span>${actions}</div></div>`;
  }
  const won = t.net_return > 0;
  const fees = t.fees_usd != null ? ` · exchange fees ${money(t.fees_usd)}` : "";
  const vsBtc = t.btc_return == null ? "" : ` · BTC did ${pct(t.btc_return)} over the same days`;
  return `
    <div class="pos demo ${won ? "won" : "lost"}" data-id="${t.id}">
      <div class="pos-row">
        ${coinIcon(t.symbol)}<span class="sym">${esc(t.symbol)}</span>
        <span class="result ${won ? "won" : "lost"}">${won ? "Successful" : "Unsuccessful"}</span>
        <span class="pnl">${signed(t.net_return, `${pct(t.net_return)} (${money(t.net_return * t.size_usd)})`)}</span>
        <span class="grow"></span>${actions}
      </div>
      <div class="pos-details">${money(t.size_usd)} bought at ${price(t.entry_price)} on ${date(t.opened_at)}, ended at ${price(t.exit_price)}
        on ${date(t.closed_at)}: ${esc(REASON_TEXT[t.exit_reason] || t.exit_reason)} · after fees${fees}${vsBtc}</div>
    </div>`;
}

// ---------- Demo account ----------
// Same share of the account as the pick suggests for your real bankroll, capped at the cash left.
function demoAmount(p) {
  const acct = state.demo?.account;
  const bankroll = state.bankroll?.amount;
  if (!acct || !bankroll) return Math.round(p.size_usd) || 100;
  return Math.max(1, Math.min(Math.round((p.size_usd / bankroll) * acct.value), Math.floor(acct.cash)));
}

const TYPE_ORDER = ["Strong", "Good", "Copy", "Early", "Pump", "Other"];
// Shown next to the auto-trading checkboxes: what the 6-month backtest found (research/rising_backtest.py).
const RISKY_NOTE = { Early: " · lost money in a 6-month backtest", Pump: " · lost money in a 6-month backtest",
  Copy: " · new, +1.33% a trade in a 6,500-trade test" };
const TYPE_HELP = { Strong: "3 of 3 checks", Good: "2 of 3 checks", Copy: "swing copies of one trader", Early: "rising-now early movers",
  Pump: "pump rides", Other: "other" };

function resultsTable(byType, title) {
  const rows = TYPE_ORDER.filter((t) => byType[t]);
  if (!rows.length) return "";
  return `<div class="card"><p class="card-title">${title}</p><div class="table-wrap"><table>
    <thead><tr><th>Type</th><th class="num">Trades</th><th class="num">Win rate</th><th class="num">Average after fees</th>
      <th class="num">Total</th><th class="num">BTC same days (avg)</th></tr></thead>
    <tbody>${rows.map((t) => { const r = byType[t]; return `<tr><td>${t} <span class="muted small">${TYPE_HELP[t]}</span></td>
      <td class="num">${r.trades}</td><td class="num">${Math.round(r.win_rate * 100)}%</td>
      <td class="num">${signed(r.avg_return, pct(r.avg_return, 2))}</td><td class="num">${signed(r.total_usd, money(r.total_usd))}</td>
      <td class="num">${r.avg_btc == null ? "–" : signed(r.avg_btc, pct(r.avg_btc, 2))}</td></tr>`; }).join("")}</tbody></table></div></div>`;
}

// Demo account value over time vs. simply holding BTC with the same starting money (drawn with bot.js's lineChart).
function drawDemoChart(history, start) {
  const el = $("demo-chart");
  if (!el) return;
  const btc0 = history.find((h) => h.btc_price)?.btc_price;
  lineChart(el, {
    series: [{ key: "demo", label: "Demo account", points: history.map((h) => [h.ts, h.value]) },
      { key: "btc", label: "Holding BTC", points: history.map((h) => [h.ts, btc0 && h.btc_price ? start * (h.btc_price / btc0) : null]) }],
    fmtY: (v) => money(v, v < 1000 ? 2 : 0), baseline: start, label: "Demo account value over time compared with holding BTC",
  });
}

const AT_STATE = (on) => on ? "On: new picks of the ticked types are bought with demo money automatically." : "Off";

function renderAutotrade(cfg) {
  if ($("autotrade").querySelector(":focus")) return;  // don't redraw while you're editing
  $("autotrade").innerHTML = `
    <div class="card autotrade">
      <div class="card-head">
        <label class="switch"><input type="checkbox" id="at-enabled" ${cfg.enabled ? "checked" : ""} /><span class="slider"></span>
          <span class="switch-label"><b>Automatic demo trading</b></span></label>
        <span class="muted small" id="at-state">${AT_STATE(cfg.enabled)}</span>
      </div>
      <p class="muted small">Buys every new pick of the ticked types with demo money, right away, and sells each by its own plan
        (stop-loss, target, trailing stop, sell signals, hold time), checked every 2 seconds. Only demo money is ever traded.</p>
      <div class="chip-checks" role="group" aria-label="Pick types to buy automatically">
        ${["Strong", "Good", "Copy", "Early", "Pump"].map((t) => `<label class="chip-check"><input type="checkbox" name="at-type" value="${t}" ${cfg.types.includes(t) ? "checked" : ""} />
          <span><b>${t}</b><small>${TYPE_HELP[t]}${RISKY_NOTE[t] || ""}</small></span></label>`).join("")}
      </div>
      <div class="at-limits">
        <label>At most <input type="number" id="at-max-open" min="1" max="50" value="${cfg.max_open}" /> open trades</label>
        <label>At most <input type="number" id="at-max-pct" min="5" max="100" step="5" value="${Math.round(cfg.max_invested_pct * 100)}" />% of the account invested</label>
        <button type="button" class="btn small" id="at-save">Save</button><span id="at-msg" class="form-msg" role="status"></span>
      </div>
    </div>`;
  $("at-enabled").addEventListener("change", (e) => { $("at-state").textContent = AT_STATE(e.target.checked); });
  $("at-save").addEventListener("click", async () => {
    const body = {
      enabled: $("at-enabled").checked,
      types: [...document.querySelectorAll("input[name=at-type]:checked")].map((i) => i.value),
      max_open: +$("at-max-open").value,
      max_invested_pct: +$("at-max-pct").value / 100,
    };
    try {
      await withBusy($("at-save"), "Saving…", () => put("/api/settings/autotrade", body));
      formMessage($("at-msg"), "Saved.", "ok");
      loadDemo(true);
    } catch (e) { formMessage($("at-msg"), e.message); }
  });
}

function renderDemo(data) {
  state.demo = data;
  renderAutotrade(data.autotrade);
  const a = data.account;
  // The chart is only redrawn when a new hourly point arrives, so hovering it isn't interrupted by the live redraw.
  const chartKey = `${data.history.length}:${data.history.at(-1)?.ts}`;
  if (state.demoChartKey !== chartKey || !$("demo-chart")) {
    state.demoChartKey = chartKey;
    $("demo-progress").innerHTML = `<div class="card"><div class="chart-head"><p class="card-title">Demo account over time</p>
        <div class="chart-legend"><span class="key demo">Demo account</span><span class="key btc">Holding BTC instead</span></div></div>
        <div class="chart-wrap" id="demo-chart"></div></div>` + resultsTable(data.results_by_type, "Results by pick type");
    drawDemoChart(data.history, a.start_balance);
  }
  const remove = (t) => `<button type="button" class="btn small ghost" data-act="remove" aria-label="Delete the ${esc(t.symbol)} demo trade">✕</button>`;
  const sellNow = (t) => `<button type="button" class="btn small ghost" data-act="sell" aria-label="Sell the ${esc(t.symbol)} demo trade now">Sell now</button>`;
  const panel = accountPanel({
    label: "Demo account value", value: a.value, a, live: isLive(data.live_at),
    headline: `<span class="chg ${a.total_result >= 0 ? "up" : "down"}">${pct(a.total_result_pct, 2)}</span>
      ${signed(a.total_result, money(a.total_result, 2))}<span class="muted">since the start with ${money(a.start_balance, 0)}</span>`,
    extraTiles: [stat("Cash available", money(a.cash))],
    footer: `<details class="reset"><summary>Reset demo account</summary>
        <form class="buy-form" id="demo-reset-form" novalidate>
          <label>Start again with $ <input type="number" id="demo-start" name="start" min="1" step="any" value="${Math.round(a.start_balance)}" /></label>
          <button class="btn small ghost" type="submit">Reset</button>
          <span class="muted">Deletes all demo trades (the old results are kept in the audit log).</span>
          <span class="form-msg" role="status"></span></form>
      </details>`,
  });
  const rows = data.trades.map((t) => t.status === "open"
    ? openRow(t, { tags: `<span class="tag">${t.auto ? "auto" : "demo"}</span>`, actions: sellNow(t) + remove(t) })
    : closedRow(t, { actions: remove(t) })).join("");

  $("demo").innerHTML = panel + (rows || `<div class="empty">No demo trades yet. Turn on automatic demo trading above, or use “Demo buy” on any pick.</div>`);
  applyDynamicStyles($("demo"));
  $("demo").querySelectorAll("[data-act=remove]").forEach((b) => b.addEventListener("click", async () => {
    const row = b.closest(".pos");
    const t = data.trades.find((x) => String(x.id) === row.dataset.id);
    if (!confirm(`Delete the ${t.symbol} demo trade? Its money goes back to demo cash as if it never happened.`)) return;
    try {
      await withBusy(b, "…", () => del(`/api/positions/${t.id}`));
    } catch (err) { return rowMessage(row, err.message); }
    notice(`${t.symbol} demo trade deleted.`);
    loadDemo(true);
  }));
  $("demo").querySelectorAll("[data-act=sell]").forEach((b) => b.addEventListener("click", async () => {
    const row = b.closest(".pos");
    const t = data.trades.find((x) => String(x.id) === row.dataset.id);
    if (!confirm(`Sell the ${t.symbol} demo trade now at the live price? It counts in your demo results.`)) return;
    try {
      await withBusy(b, "Selling…", () => post(`/api/positions/${t.id}/close`, {}));
    } catch (err) { return rowMessage(row, err.message); }
    notice(`${t.symbol} demo trade sold at the live price.`);
    loadDemo(true);
  }));
  $("demo-reset-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const msg = e.target.querySelector(".form-msg");
    const v = +$("demo-start").value;
    if (!(v > 0)) { formMessage(msg, "Enter a starting balance above $0."); return $("demo-start").focus(); }
    if (!confirm(`Reset the demo account to ${money(v, 0)}? All demo trades will be deleted.`)) return;
    try {
      await withBusy(e.target.querySelector("[type=submit]"), "Resetting…", () => post("/api/demo/reset", { start_balance: v }));
    } catch (err) { return formMessage(msg, err.message); }
    notice(`Demo account reset to ${money(v, 0)}.`);
    loadAll(true);
  });
}

// An error under one position row (for its Sell / Delete buttons).
function rowMessage(row, text) {
  let el = row.querySelector(":scope > .form-msg");
  if (!el) {
    el = document.createElement("div");
    row.append(el);
  }
  formMessage(el, text);
  setTimeout(() => el.remove(), 10000);  // then the live redraw can carry on
}

// The live redraw replaces the whole list, so it waits while you're using something inside it: keyboard focus
// (not a mouse click, which leaves focus on the button), an open form, or an error you haven't read yet.
// `force` redraws right after your own change.
const busyInside = (el) => {
  const f = document.activeElement;
  const typing = f && el.contains(f) && (f.matches(":focus-visible") || f.matches("input, select, textarea"));
  return Boolean(typing || el.querySelector("form.row-form, .pos > .form-msg"));
};

async function loadDemo(force = false) {
  if (!force && busyInside($("demo-view"))) return;
  const wasOpen = document.querySelector("#demo details.reset")?.open;
  renderDemo(await api("/api/demo"));
  if (wasOpen) document.querySelector("#demo details.reset").open = true;
}

// ---------- Your portfolio (real money) ----------
function renderPortfolio(data) {
  state.portfolio = data;
  const s = state.status;
  const connected = s?.account.configured;
  const a = data.account;
  const st = state.account?.status;

  const extra = [];
  if (a.connected) {
    extra.push(stat("Cash (USDT, USDC)", money(a.cash)));
    if (st?.funding_usd >= 1) extra.push(stat("Funding account", money(st.funding_usd), "not tradable until moved"));
    if (st?.earn_usd >= 1) extra.push(stat("Earn", money(st.earn_usd), "not tradable until redeemed"));
    if (a.outside_exchange >= 1) extra.push(stat(`Tracked outside ${EX()}`, money(a.outside_exchange), "entered by hand"));
  }
  const panel = (a.connected || a.running || a.finished) ? accountPanel({
    label: a.connected ? `${EX()} spot value (live)` : "Value of your open positions",
    value: a.value, a, live: isLive(data.live_at), extraTiles: extra,
  }) : "";

  const rows = data.positions.map((p) => {
    const manual = p.source !== "synced";
    const remove = manual ? `<button type="button" class="btn small ghost" data-act="remove" aria-label="Delete the ${esc(p.symbol)} entry">✕</button>` : "";
    if (p.status !== "open") return closedRow(p, { actions: remove });
    const check = checkLabel(p.exchange_check);
    const stopTag = connected && p.exchange_check === "ok"
      ? (p.stop_order_kind === "trailing" ? `<span class="tag ok">✓ Trailing stop order</span>`
        : p.stop_order_price ? `<span class="tag ok">✓ Stop order at ${price(p.stop_order_price)}</span>` : `<span class="tag bad">No stop order</span>`) : "";
    return openRow(p, {
      tags: `${p.source === "paper" ? `<span class="tag">paper</span>` : ""}${check ? `<span class="tag ${check[0]}">${check[1]}</span>` : ""}${stopTag}`,
      status: `<span class="advice ${esc(p.advice)}">${ADVICE_LABEL[p.advice] || "Hold"}</span>`,
      actions: `${manual ? `<button type="button" class="btn small ${p.advice === "SELL" ? "danger" : "ghost"}" data-act="sold" aria-label="I sold ${esc(p.symbol)}">I sold</button>` : ""}${remove}`,
      extraDetails: p.advice === "SELL" || p.advice === "TAKE_PROFIT" ? ` · <a href="${esc(p.trade_url)}" target="_blank" rel="noopener">Sell on ${EX()}</a>` : "",
    });
  }).join("");

  $("portfolio").innerHTML = panel + (rows || `<div class="empty">No positions yet. ${connected
    ? `Coins you buy on ${EX()} appear here automatically.`
    : `Buy a pick and mark it with “I bought it”, or connect ${EX()} so the app tracks it for you.`}</div>`);
  applyDynamicStyles($("portfolio"));
  $("real-types").innerHTML = resultsTable(data.results_by_type || {}, "Results by pick type");

  $("portfolio").querySelectorAll("[data-act]").forEach((b) => b.addEventListener("click", async () => {
    const row = b.closest(".pos");
    const id = row.dataset.id;
    const pos = data.positions.find((x) => String(x.id) === id);
    if (b.dataset.act === "sold") return openSoldForm(row, pos);
    if (b.dataset.act === "remove") {
      if (!confirm(`Delete the ${pos.symbol} entry? It won’t count in your history.`)) return;
      try {
        await withBusy(b, "…", () => del(`/api/positions/${id}`));
      } catch (err) { return rowMessage(row, err.message); }
      notice(`${pos.symbol} entry deleted.`);
      await loadAll(true);
    }
  }));

  // Connection card / hints
  $("sync").hidden = !connected;
  if (!connected) {
    $("connect").innerHTML = `
      <div class="connect"><b>Connect OKX (read-only)</b> so the app sees what you hold, your trades and your stop orders.
        Add your read-only OKX key on the <a href="#settings">Settings</a> page. It’s stored encrypted with your account.</div>`;
  } else if (!s.account.ok && s.account.error) {
    $("connect").innerHTML = `<div class="connect error"><b>${EX()} connection problem:</b> ${esc(s.account.error)}</div>`;
  } else if (s.bankroll.spot_empty) {
    const elsewhere = (st?.funding_usd || 0) + (st?.earn_usd || 0);
    $("connect").innerHTML = `<div class="connect"><b>${EX()} connected.</b> Your trading account is empty, so picks are sized
      from the fallback bankroll on the <a href="#settings">Settings</a> page.${elsewhere >= 10 ? ` You have ${money(elsewhere)} in your ${st.funding_usd ? "Funding account" : ""}${st.funding_usd && st.earn_usd ? " and " : ""}${st.earn_usd ? "Simple Earn" : ""}.
      To trade it, ${st.funding_usd ? "on OKX go to Assets → Transfer, from Funding to Trading" : "redeem it from Earn first"}.` : ""}</div>`;
  } else if (s.account.can_trade) {
    $("connect").innerHTML = `<div class="connect">Your ${EX()} key can place trades. The app only reads, so a read-only key is safer: replace it on the <a href="#settings">Settings</a> page.</div>`;
  } else {
    $("connect").innerHTML = "";
  }

  const trades = state.account?.trades || [];
  $("trades-box").hidden = !trades.length;
  $("trades").innerHTML = trades.length ? `<div class="table-wrap"><table>
    <thead><tr><th>Date</th><th>Pair</th><th>Side</th><th class="num">Price</th><th class="num">Amount</th><th class="num">Total</th></tr></thead>
    <tbody>${trades.map((t) => `<tr><td>${new Date(t.time).toLocaleString()}</td><td>${esc(t.pair)}</td>
      <td class="${t.is_buyer ? "up" : "down"}">${t.is_buyer ? "Buy" : "Sell"}</td><td class="num">${price(t.price)}</td>
      <td class="num">${num(t.qty)}</td><td class="num">${money(t.quote_qty)}</td></tr>`).join("")}</tbody></table></div>` : "";
}

// "I sold": the sell price, typed with a dot or a comma (1.23 or 1,23), checked before anything is saved.
function openSoldForm(row, pos) {
  const open = row.querySelector("form.row-form");
  if (open) return open.price.focus();
  const form = document.createElement("form");
  form.className = "row-form";
  form.noValidate = true;
  form.innerHTML = `
    <label>Sell price for ${esc(pos.symbol)} <input type="text" name="price" inputmode="decimal" autocomplete="off"
      spellcheck="false" value="${orderPrice(pos.last_price || pos.entry_price)}" /></label>
    <button class="btn small" type="submit">Save sale</button>
    <button class="btn small ghost" type="button" data-act="cancel">Cancel</button>
    <span class="form-msg" role="status"></span>`;
  row.append(form);
  form.price.select();
  form.querySelector("[data-act=cancel]").addEventListener("click", () => form.remove());
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const msg = form.querySelector(".form-msg");
    const v = parseNum(form.price.value);
    if (!(v > 0)) { formMessage(msg, "Enter the price you sold at, for example 1.23 or 1,23."); return form.price.focus(); }
    try {
      await withBusy(form.querySelector("[type=submit]"), "Saving…", () => post(`/api/positions/${pos.id}/close`, { exit_price: v }));
    } catch (err) { return formMessage(msg, err.message); }
    notice(`${pos.symbol} marked as sold at ${price(v)}.`);
    form.remove();
    await loadAll(true);
  });
}

async function loadPortfolio(force = false) {
  if (!force && busyInside($("real-view"))) return;
  renderPortfolio(await api("/api/portfolio"));
}

// ---------- Alerts ----------
let alertsKey = null;
async function loadAlerts() {
  const alerts = await api("/api/alerts");
  // #alerts is a live region: only redraw when the list really changed, so nothing is read out twice.
  const key = alerts.map((a) => a.id).join(",");
  if (key !== alertsKey) {
    alertsKey = key;
    $("alerts").innerHTML = alerts.map((a) => `
      <div class="alert ${esc(a.level)}" data-id="${a.id}">
        <span>${esc(a.message)}</span><button type="button" class="btn small ghost" data-act="dismiss" aria-label="Dismiss: ${esc(a.message)}">Dismiss</button>
      </div>`).join("");
    $("alerts").querySelectorAll("[data-act=dismiss]").forEach((b) => b.addEventListener("click", async () => {
      try {
        await withBusy(b, "…", () => post(`/api/alerts/${b.closest(".alert").dataset.id}/seen`));
      } catch (err) { return notice(err.message, "error"); }
      loadAlerts();
    }));
  }
  const shown = new Set(store.get("notified", []));
  const fresh = alerts.filter((a) => !shown.has(a.id) && a.level !== "warning");
  if (fresh.length && "Notification" in window && Notification.permission === "granted") {
    fresh.forEach((a) => new Notification("Copy Signals", { body: a.message }));
  }
  alerts.forEach((a) => shown.add(a.id));
  store.set("notified", [...shown].slice(-500));
}

// ---------- Exiting + track record ----------
function renderExiting(rows) {
  $("exiting-section").hidden = !rows.length;
  $("exiting").innerHTML = rows.map((e) => `
    <div class="exit-item">${coinIcon(e.symbol)}<div><b>${esc(e.symbol)}</b><span class="muted small"><span class="down">${e.sellers} sold</span>
      · ${e.buyers} bought · ${e.holders} still hold</span></div></div>`).join("");
}


const STRENGTH_HELP = { Strong: "picks, 3 of 3 checks", Good: "picks, 2 of 3 checks", Copy: "swing copies", Early: "rising-now early movers",
  Pump: "pump rides" };

async function loadPerformance() {
  const perf = await api("/api/performance");
  const o = perf.overall;
  const kpi = (label, value, sub = "") => `<div class="kpi"><small>${label}</small><b>${value}</b>${sub ? `<span class="vs">${sub}</span>` : ""}</div>`;
  const types = Object.entries(perf.by_strength).filter(([, x]) => x.trades);
  $("performance").innerHTML = `
    <div class="kpis">
      ${kpi("Closed trades", o.trades || 0, perf.since ? `since ${date(perf.since)}` : "none yet")}
      ${kpi("Won", o.trades ? `${Math.round(o.win_rate * 100)}%` : "–", "after fees")}
      ${kpi("Average per trade", o.trades ? signed(o.avg_net, pct(o.avg_net, 2)) : "–", "after fees")}
      ${kpi("BTC over the same days", o.avg_btc == null ? "–" : signed(o.avg_btc, pct(o.avg_btc, 2)), "average")}
    </div>
    ${types.length ? `<div class="card"><p class="card-title">By type</p><div class="table-wrap"><table>
      <thead><tr><th>Type</th><th class="num">Trades</th><th class="num">Won</th><th class="num">Average after fees</th><th class="num">BTC same days</th></tr></thead>
      <tbody>${types.map(([k, x]) => `<tr><td><b>${esc(k)}</b> <span class="muted small">${STRENGTH_HELP[k] || ""}</span></td>
        <td class="num">${x.trades}</td><td class="num">${Math.round(x.win_rate * 100)}%</td>
        <td class="num">${signed(x.avg_net, pct(x.avg_net, 2))}</td><td class="num">${x.avg_btc == null ? "–" : signed(x.avg_btc, pct(x.avg_btc, 2))}</td></tr>`).join("")}
      </tbody></table></div></div>` : ""}
    <div class="card"><p class="card-title">Being followed now</p>
      ${perf.open.length ? `<div class="exiting">${perf.open.map((t) => `<div class="exit-item">${coinIcon(t.symbol)}<div><b>${esc(t.symbol)}</b>
        <span class="muted small">${esc(t.strength)} · ${signed(t.net_now, pct(t.net_now, 2))} now</span></div></div>`).join("")}</div>`
        : `<p class="muted">Nothing open. A paper trade opens as soon as a coin becomes a pick or starts rising.</p>`}</div>
    ${perf.recent.length ? `<div class="card"><p class="card-title">Recently closed</p><div class="table-wrap"><table>
      <thead><tr><th>Coin</th><th>Type</th><th>Dates</th><th>Why it closed</th><th class="num">Result after fees</th><th class="num">BTC same days</th></tr></thead>
      <tbody>${perf.recent.map((t) => `<tr><td>${esc(t.symbol)}</td><td>${esc(t.strength)}</td><td>${date(t.opened_at)} – ${date(t.closed_at)}</td>
        <td>${esc(REASON_TEXT[t.exit_reason] || t.exit_reason)}</td><td class="num">${signed(t.net_return, pct(t.net_return, 2))}</td>
        <td class="num">${t.btc_return == null ? "–" : signed(t.btc_return, pct(t.btc_return, 2))}</td></tr>`).join("")}</tbody></table></div></div>`
      : `<div class="empty">No trades have closed yet. Results appear when a trade hits its stop, its target, a sell signal or the end of its hold time.</div>`}`;
}

// ---------- Status ----------
// (The bankroll used to size picks when OKX isn't connected is set on the Settings page.)

// Screen readers hear the status only when it changes kind (updating → live → offline), not every "updated 2 min ago".
let lastStatusKind = "";
function announceStatus(kind, text) {
  if (kind === lastStatusKind) return;
  lastStatusKind = kind;
  $("status-live").textContent = text;
}

let wasRefreshing = false;
let lastUpdatedAt = null;
async function poll() {
  let s;
  try { s = await api("/api/status"); } catch {
    $("status-text").textContent = "App offline";
    $("status-dot").className = "dot err";
    announceStatus("offline", "The app is offline. Check that it’s running.");
    return;
  }
  if (s.user.must_change_password) {
    showForcedPasswordChange(s.user.username);
    return s;
  }
  const modeChanged = state.status && state.status.demo_mode !== s.demo_mode;
  state.status = s;
  state.bankroll = s.bankroll;
  renderMode(s);
  if (modeChanged) loadAll();
  if (s.refreshing) {
    $("status-dot").className = "dot idle";
    $("status-text").textContent = "Updating…";
    announceStatus("updating", "Updating data…");
  } else if (s.updated_at) {
    const failing = s.errors.map((e) => e.source).join(", ");
    $("status-dot").className = s.errors.length ? "dot err" : "dot";
    $("status-text").textContent = `Live · updated ${ago(s.updated_at)}` + (s.errors.length ? ` · ${failing} failing` : "");
    $("status-text").title = s.errors.map((e) => `${e.source}: ${e.last_error}`).join("\n");
    announceStatus(`live:${failing}`, s.errors.length ? `Data updated, but ${failing} failing.` : "Data updated.");
  } else {
    $("status-text").textContent = "Getting the first data…";
    announceStatus("first", "Getting the first data…");
  }
  // New data arrives every minute (and after a full refresh): redraw the pages when it does.
  if ((wasRefreshing && !s.refreshing) || (lastUpdatedAt && s.updated_at !== lastUpdatedAt)) loadAll();
  wasRefreshing = s.refreshing;
  lastUpdatedAt = s.updated_at;
  return s;
}

// `force`: redraw the portfolio views even if you're using something in them (right after your own change).
async function loadAll(force = false) {
  try {
    if (!state.status) await poll();
    const [picks, portfolio, account, demo, rising] = await Promise.all([
      api("/api/picks"), api("/api/portfolio"), api("/api/account"), api("/api/demo"), api("/api/movers")]);
    state.account = account;
    state.demo = demo;
    state.portfolio = portfolio;
    renderPicks(picks);
    renderRising(rising);
    if (force || !busyInside($("demo-view"))) renderDemo(demo);
    renderExiting(picks.exiting);
    if (force || !busyInside($("real-view"))) renderPortfolio(portfolio);
    await Promise.all([loadAlerts(), loadPerformance()]);
    $("load-error").hidden = true;
  } catch (e) {
    // Shown above every page, not only on Picks, so it's seen wherever you are.
    $("load-error").hidden = false;
    $("load-error").querySelector("span").textContent = `Couldn’t load the latest data: ${e.message} It tries again at the next update.`;
  }
}

// A temporary password (e.g. the first admin's) must be replaced before anything else works.
function showForcedPasswordChange(username) {
  if ($("forced-pw")) return;
  document.querySelectorAll(".side-nav, .tabbar").forEach((n) => { n.hidden = true; });
  document.querySelectorAll("#account-menu a").forEach((a) => { a.hidden = true; });  // only Log out stays
  document.querySelectorAll(".page, #alerts").forEach((el) => { el.hidden = true; });
  const box = document.createElement("section");
  box.id = "forced-pw";
  box.innerHTML = `
    <div class="card">
      <h2>Choose a new password</h2>
      <p class="muted">Your current password is temporary. Choose a new one of at least 12 characters to continue.</p>
      <form id="forced-pw-form" class="form">
        <input type="text" name="username" value="${esc(username)}" autocomplete="username" hidden />
        <label>Current password <input type="password" name="current" autocomplete="current-password" required /></label>
        <label>New password <input type="password" name="new" autocomplete="new-password" minlength="12" required /></label>
        <label>Repeat new password <input type="password" name="repeat" autocomplete="new-password" minlength="12" required /></label>
        <div class="actions"><button class="btn small" type="submit">Save and continue</button><span class="form-msg" id="forced-pw-msg" role="status"></span></div>
      </form>
    </div>`;
  document.querySelector("main").prepend(box);
  $("forced-pw-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = e.target;
    if (f.new.value !== f.repeat.value) {
      formMessage($("forced-pw-msg"), "The new passwords don’t match. Type the same password in both boxes.");
      return f.repeat.focus();
    }
    try {
      await withBusy(f.querySelector("[type=submit]"), "Saving…", () => post("/api/auth/password", { current: f.current.value, new: f.new.value }));
      location.reload();
    } catch (err) { formMessage($("forced-pw-msg"), err.message); }
  });
}

// Demo on: the portfolio page shows the demo account and buy buttons become demo buys.
function renderMode(s) {
  const demo = Boolean(s.demo_mode);
  document.body.classList.toggle("demo-mode", demo);
  $("demo-badge").hidden = !demo;
  if (document.activeElement !== $("demo-toggle")) $("demo-toggle").checked = demo;
  $("real-view").hidden = demo;
  $("demo-view").hidden = !demo;
  document.querySelectorAll(".portfolio-label").forEach((el) => { el.textContent = demo ? "Demo portfolio" : "Portfolio"; });
  $("user-name").textContent = s.user.username;
  $("avatar").textContent = s.user.username.slice(0, 1);
  $("account-who").textContent = `Signed in as ${s.user.username}${s.user.role === "admin" ? " (admin)" : ""}`;
  document.querySelectorAll(".admin-only").forEach((a) => { a.hidden = s.user.role !== "admin" || s.user.must_change_password; });
}

// ---------- Account dropdown ----------
// A disclosure: the button opens a short list of links. Escape or a click outside closes it; ↑/↓ move between items.
const accountItems = () => [...$("account-menu").querySelectorAll("a:not([hidden]), button")];
function setAccountMenu(open, { focusFirst = false, returnFocus = false } = {}) {
  const menu = $("account-menu");
  menu.hidden = !open;
  $("account-button").setAttribute("aria-expanded", String(open));
  if (open) {
    menu.classList.remove("align-left");
    if (menu.getBoundingClientRect().left < 8) menu.classList.add("align-left");  // don't run off the left edge
    if (focusFirst) accountItems()[0]?.focus();
  } else if (returnFocus) {
    $("account-button").focus();
  }
}
$("account-button").addEventListener("click", (e) => {
  // e.detail is 0 for a keyboard "click" (Enter/Space): then move focus into the menu.
  setAccountMenu($("account-menu").hidden, { focusFirst: e.detail === 0 });
});
$("account-button").addEventListener("keydown", (e) => {
  if (e.key === "ArrowDown") { e.preventDefault(); setAccountMenu(true, { focusFirst: true }); }
});
$("account").addEventListener("keydown", (e) => {
  // Keys on the button itself are handled above (otherwise ↓ would open the menu and then skip an item).
  if ($("account-menu").hidden || (e.target === $("account-button") && e.key !== "Escape")) return;
  if (e.key === "Escape") { e.preventDefault(); setAccountMenu(false, { returnFocus: true }); return; }
  if (e.key === "ArrowDown" || e.key === "ArrowUp") {
    const items = accountItems();
    const i = items.indexOf(document.activeElement);
    if (i < 0) return;
    e.preventDefault();
    items[(i + (e.key === "ArrowDown" ? 1 : items.length - 1)) % items.length].focus();
  }
});
$("account-menu").addEventListener("click", (e) => {
  if (e.target.closest("a")) setAccountMenu(false);
});
// On phones the tab bar's "More" opens the same menu.
$("tab-more").addEventListener("click", (e) => {
  setAccountMenu($("account-menu").hidden, { focusFirst: e.detail === 0 });
});
document.addEventListener("click", (e) => {
  if (!$("account-menu").hidden && !$("account").contains(e.target) && !$("tab-more").contains(e.target)) setAccountMenu(false);
});
// Tabbing out of the menu closes it.
$("account").addEventListener("focusout", (e) => {
  if (e.relatedTarget && !$("account").contains(e.relatedTarget)) setAccountMenu(false);
});

$("demo-toggle").addEventListener("change", async (e) => {
  try {
    await put("/api/settings/prefs", { demo_mode: e.target.checked });
  } catch (err) {
    e.target.checked = !e.target.checked;
    notice(err.message, "error");
    return;
  }
  notice(e.target.checked ? "Demo on: showing your demo account." : "Demo off: showing your real account.");
  await poll();
  loadAll(true);
});

$("logout").addEventListener("click", async () => {
  $("logout").disabled = true;
  $("logout").textContent = "Logging out…";
  try { await post("/api/auth/logout"); } finally { location.href = "/login.html"; }
});

$("sync").addEventListener("click", async () => {
  try {
    await withBusy($("sync"), "Syncing…", () => post("/api/account/sync"));
    notice("Synced with OKX.");
  } catch (err) { notice(err.message, "error"); }
  await poll();
  loadAll(true);
});

// Skip link: focus the page content (a plain #content link would be read as a page change by the menu).
$("skip-link").addEventListener("click", (e) => {
  e.preventDefault();
  $("content").focus();
});
if ("Notification" in window && Notification.permission === "default") {
  $("notify").hidden = false;
  $("notify").addEventListener("click", async () => { await Notification.requestPermission(); $("notify").hidden = true; });
}

// ---------- Menu ----------
const PAGES = ["bot", "picks", "rising", "portfolio", "record", "settings", "admin", "how"];
const PAGE_LOADERS = {};  // settings.js and admin.js register loaders here; they get the part after "/" (e.g. #admin/users)
function go(route) {
  let [page, sub] = String(route || "").split("/");
  if (page === "demo") page = "portfolio";
  if (!PAGES.includes(page)) { page = "bot"; sub = undefined; }
  const hash = sub ? `#${page}/${sub}` : `#${page}`;
  if (location.hash !== hash) history.replaceState(null, "", hash);
  if (PAGE_LOADERS[page]) PAGE_LOADERS[page](sub);
  document.querySelectorAll(".page").forEach((s) => { s.hidden = s.dataset.page !== page; });
  document.querySelectorAll("a[data-page]").forEach((a) => {
    const on = a.dataset.page === page;
    a.classList.toggle("on", on);
    if (on) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current");
  });
  // Highlight the account button (and "More" on phones) while you're on one of the menu's pages.
  const inMenu = Boolean($("account-menu").querySelector(`a[data-page="${page}"]`));
  $("account-button").classList.toggle("on", ["settings", "admin"].includes(page));
  $("tab-more").classList.toggle("on", inMenu);
  store.set("page", page);
  window.scrollTo(0, 0);
}
window.addEventListener("hashchange", () => go(location.hash.slice(1)));

poll().then((s) => {
  if (s?.user.must_change_password) return;
  go(location.hash.slice(1) || store.get("page", "bot"));
  loadAll();
});
setInterval(poll, 5000);
setInterval(() => loadRising().catch(() => {}), 15000);
setInterval(loadAlerts, 30000);
// Live: re-draw open positions and demo trades every 5 seconds while any are running.
setInterval(() => {
  if (demoMode() && state.demo?.account.running) loadDemo().catch(() => {});
  if (!demoMode() && state.portfolio?.account.running) loadPortfolio().catch(() => {});
}, 5000);
