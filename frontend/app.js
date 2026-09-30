// Main app: status, menus, and your OKX portfolio. The trend bot (bot.js), the long/short test (longshort.js),
// settings (settings.js) and admin (admin.js) register their own page loaders. Helpers are in common.js.
const state = { status: null, account: null };

const tile = (label, value, sub = "") => `<div class="tile"><small>${label}</small><b>${value}</b>${sub ? `<span class="sub">${sub}</span>` : ""}</div>`;
const stat = (label, value, sub = "") => `<div class="stat"><small>${label}</small><b>${value}</b>${sub ? `<span class="sub">${sub}</span>` : ""}</div>`;

// The live redraw replaces a whole page, so it waits while you're using something inside it: keyboard focus (not a
// mouse click, which leaves focus on the button) or an open form.
const busyInside = (el) => {
  const f = document.activeElement;
  const typing = f && el.contains(f) && (f.matches(":focus-visible") || f.matches("input, select, textarea"));
  return Boolean(typing || el.querySelector("form.row-form"));
};

// ---------- Your OKX portfolio ----------
function renderPortfolio(a) {
  state.account = a;
  const s = state.status;
  const el = $("portfolio");
  $("sync").hidden = !a.configured;
  if (!a.configured) {
    el.innerHTML = `<div class="connect"><b>Connect OKX (read-only)</b> so the app shows your balances, coins, stop orders and
      trades. Add a read-only key on the <a href="#settings">Settings</a> page; it’s stored encrypted with your account.</div>`;
    return;
  }
  const st = a.status || {};
  const problem = s && !s.account.ok && s.account.error
    ? `<div class="connect error"><b>${EX()} connection problem:</b> ${esc(s.account.error)}</div>` : "";
  const tradeKey = st.can_trade ? `<div class="connect">Your ${EX()} key can place trades. The app only reads, so a read-only
    key is safer: replace it on the <a href="#settings">Settings</a> page.</div>` : "";
  const coins = a.holdings.reduce((sum, h) => sum + (h.value_usd || 0), 0);
  const hero = `<div class="card hero">
    <div class="hero-top"><p class="card-title">${EX()} trading account</p><span class="spacer"></span>
      <span class="muted small">${st.synced_at ? `synced ${esc(ago(st.synced_at))}` : "not synced yet"}</span></div>
    <div class="value">${money(st.total_usd ?? 0, 2)}</div>
    <div class="stats">
      ${stat("Cash (USDT, USDC)", money(st.cash_usd ?? 0))}
      ${stat("Coins", money(coins), `${a.holdings.length} held`)}
      ${st.funding_usd >= 1 ? stat("Funding account", money(st.funding_usd), "not tradable until moved") : ""}
      ${st.earn_usd >= 1 ? stat("Earn", money(st.earn_usd), "not tradable until redeemed") : ""}
      ${stat("Your fee", st.fee_rate != null ? `${fixed(st.fee_rate * 100, 3)}%` : "–", "taker, as OKX reports it")}
    </div>
  </div>`;
  const holdings = a.holdings.length ? `<div class="card"><p class="card-title">Coins</p><div class="table-wrap"><table>
    <thead><tr><th>Coin</th><th class="num">Amount</th><th class="num">Price</th><th class="num">Value</th>
      <th class="num">Average cost</th><th class="num">Result</th><th>Stop order</th></tr></thead>
    <tbody>${a.holdings.map((h) => {
      const res = h.avg_cost ? h.price / h.avg_cost - 1 : null;
      const stop = h.stop_kind === "trailing" ? `<span class="tag ok">Trailing stop</span>`
        : h.stop_price ? `<span class="tag ok">Stop at ${price(h.stop_price)}</span>` : `<span class="tag bad">None</span>`;
      return `<tr><td>${coinIcon(h.coin)} ${esc(h.coin)}</td><td class="num">${num(h.qty)}</td><td class="num">${price(h.price)}</td>
        <td class="num">${money(h.value_usd)}</td><td class="num">${h.avg_cost ? price(h.avg_cost) : "–"}</td>
        <td class="num">${res == null ? "–" : signed(res, pct(res, 1))}</td><td>${stop}</td></tr>`;
    }).join("")}</tbody></table></div></div>`
    : `<div class="empty">No coins in your ${EX()} trading account right now${st.funding_usd >= 10
      ? `. You have ${money(st.funding_usd)} in your Funding account: on OKX, Assets → Transfer moves it to Trading` : ""}.</div>`;
  const orders = a.orders.length ? `<div class="card"><p class="card-title">Open orders</p><div class="table-wrap"><table>
    <thead><tr><th>Pair</th><th>Kind</th><th class="num">Trigger</th><th class="num">Price</th><th class="num">Amount</th></tr></thead>
    <tbody>${a.orders.map((o) => `<tr><td>${esc(o.pair)}</td><td>${esc(o.kind)}</td><td class="num">${price(o.stop_price)}</td>
      <td class="num">${price(o.price)}</td><td class="num">${num(o.qty)}</td></tr>`).join("")}</tbody></table></div></div>` : "";
  const trades = a.trades.length ? `<details class="card"><summary>Recent ${EX()} trades</summary><div class="table-wrap"><table>
    <thead><tr><th>Date</th><th>Pair</th><th>Side</th><th class="num">Price</th><th class="num">Amount</th><th class="num">Total</th></tr></thead>
    <tbody>${a.trades.map((t) => `<tr><td>${new Date(t.time).toLocaleString()}</td><td>${esc(t.pair)}</td>
      <td class="${t.is_buyer ? "up" : "down"}">${t.is_buyer ? "Buy" : "Sell"}</td><td class="num">${price(t.price)}</td>
      <td class="num">${num(t.qty)}</td><td class="num">${money(t.quote_qty)}</td></tr>`).join("")}</tbody></table></div></details>` : "";
  el.innerHTML = problem + tradeKey + hero + holdings + orders + trades;
}

async function loadPortfolio() {
  renderPortfolio(await api("/api/account"));
}

// ---------- Status ----------
// Screen readers hear the status only when it changes kind (live → offline), not every "updated 2 min ago".
let lastStatusKind = "";
function announceStatus(kind, text) {
  if (kind === lastStatusKind) return;
  lastStatusKind = kind;
  $("status-live").textContent = text;
}

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
  state.status = s;
  renderUser(s);
  if (s.updated_at) {
    const failing = s.errors.map((e) => e.source).join(", ");
    $("status-dot").className = s.errors.length ? "dot err" : "dot";
    $("status-text").textContent = `Live · updated ${ago(s.updated_at)}` + (s.errors.length ? ` · ${failing} failing` : "");
    $("status-text").title = s.errors.map((e) => `${e.source}: ${e.last_error}`).join("\n");
    announceStatus(`live:${failing}`, s.errors.length ? `Data updated, but ${failing} failing.` : "Data updated.");
  } else {
    $("status-text").textContent = "Getting the first data…";
    announceStatus("first", "Getting the first data…");
  }
  return s;
}

// A temporary password (e.g. the first admin's) must be replaced before anything else works.
function showForcedPasswordChange(username) {
  if ($("forced-pw")) return;
  document.querySelectorAll(".side-nav, .tabbar").forEach((n) => { n.hidden = true; });
  document.querySelectorAll("#account-menu a").forEach((a) => { a.hidden = true; });  // only Log out stays
  document.querySelectorAll(".page").forEach((el) => { el.hidden = true; });
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

function renderUser(s) {
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
  loadPortfolio().catch(() => {});
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
const PAGES = ["bot", "longshort", "portfolio", "settings", "admin", "how"];
const PAGE_LOADERS = {};  // each page's script registers a loader here; it gets the part after "/" (e.g. #admin/users)
PAGE_LOADERS.portfolio = () => loadPortfolio().catch((e) => { $("portfolio").innerHTML = `<div class="empty">${esc(e.message)}</div>`; });
function go(route) {
  let [page, sub] = String(route || "").split("/");
  if (!PAGES.includes(page)) { page = "bot"; sub = undefined; }  // includes old bookmarks (copies, record, demo)
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
const pageOpen = (name) => !document.querySelector(`.page[data-page="${name}"]`).hidden && !document.hidden;

poll().then((s) => {
  if (s?.user.must_change_password) return;
  go(location.hash.slice(1) || store.get("page", "bot"));
});
setInterval(poll, 15000);
setInterval(() => { if (pageOpen("portfolio") && !busyInside($("portfolio"))) loadPortfolio().catch(() => {}); }, 60000);
