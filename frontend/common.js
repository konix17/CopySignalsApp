// Shared helpers for every page. Loaded first.
const $ = (id) => document.getElementById(id);

// API data includes third-party text (coin names, trader names, user input): always escape it.
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const store = {
  get(k, d) { try { return JSON.parse(localStorage.getItem(k)) ?? d; } catch { return d; } },
  set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch { /* private mode */ } },
};

// All displayed numbers use the browser's locale (1,234.5 or 1.234,5), so separators never mix.
const numberFormats = new Map();
const fmt = (v, opts) => {
  const key = JSON.stringify(opts);
  if (!numberFormats.has(key)) numberFormats.set(key, new Intl.NumberFormat(undefined, opts));
  return numberFormats.get(key).format(v);
};
const fixed = (v, d) => fmt(v, { minimumFractionDigits: d, maximumFractionDigits: d });
const num = (v, maxDigits = 6) => (v == null ? "–" : fmt(v, { maximumSignificantDigits: maxDigits }));

const money = (v, digits) => {
  if (v == null) return "–";
  const a = Math.abs(v);
  const d = digits ?? (a < 100 ? 2 : 0);
  const s = a >= 1e6 ? fixed(a / 1e6, 2) + "M" : fixed(a, d);
  return (v < 0 ? "−$" : "$") + s;
};
const pct = (v, d = 1) => (v == null ? "–" : (v > 0 ? "+" : v < 0 ? "−" : "") + fixed(Math.abs(v * 100), d) + "%");
const signed = (v, text) => `<span class="${v > 0 ? "up" : v < 0 ? "down" : ""}">${text}</span>`;
const price = (v) => (v == null ? "–" : v >= 1000 ? fmt(v, { maximumFractionDigits: 2 }) : v >= 1 ? fixed(v, v >= 100 ? 2 : 4)
  : fmt(v, { minimumSignificantDigits: 4, maximumSignificantDigits: 4 }));
// Reads a typed number that may use a decimal comma ("1,23") or dot; NaN if it isn't a number.
const parseNum = (text) => {
  const t = String(text ?? "").trim().replace(/\s/g, "");
  if (!t) return NaN;
  // "1.234,5" → 1234.5 ; "1,234.5" → 1234.5 ; "1,23" → 1.23
  const lastComma = t.lastIndexOf(","), lastDot = t.lastIndexOf(".");
  const normal = lastComma > lastDot ? t.replace(/\./g, "").replace(",", ".") : t.replace(/,/g, "");
  return /^-?\d*\.?\d+$/.test(normal) ? Number(normal) : NaN;
};
const orderPrice = (v) => String(+v.toPrecision(v >= 1 ? 6 : 4)); // plain number for pasting into the exchange
const EX = () => "OKX";
const days = (d) => (d < 1.5 ? `${Math.round(d * 24)} hours` : `${Math.round(d)} days`);
const ago = (ts) => {
  const s = Date.now() / 1000 - ts;
  return s < 90 ? "just now" : s < 5400 ? `${Math.round(s / 60)} min ago` : s < 172800 ? `${Math.round(s / 3600)} h ago` : `${Math.round(s / 86400)} days ago`;
};
// A round coin badge with the first letter; BTC and ETH in their own colours, others one of 12 by name.
const coinHue = (sym) => [...String(sym)].reduce((a, ch) => (a * 31 + ch.charCodeAt(0)) >>> 0, 7) % 12;
const coinIcon = (sym) => `<span class="coin-ic ${["BTC", "ETH"].includes(sym) ? `c-${sym}` : `h${coinHue(sym)}`}" aria-hidden="true">${esc(String(sym).slice(0, 1))}</span>`;
const date = (ts) => new Date(ts * 1000).toLocaleDateString(undefined, { month: "short", day: "numeric" });
const dateTime = (ts) => (ts ? new Date(ts * 1000).toLocaleString() : "–");

function cookie(name) {
  const m = document.cookie.match(new RegExp(`(?:^|; )${name}=([^;]*)`));
  return m ? decodeURIComponent(m[1]) : "";
}

// Every change is sent with the session's CSRF token; a lost session goes back to the login page.
async function api(path, opts = {}) {
  const headers = { "Content-Type": "application/json", ...(opts.headers || {}) };
  if (opts.method && opts.method !== "GET") headers["X-CSRF-Token"] = cookie("cs_csrf");
  const r = await fetch(path, { ...opts, headers, credentials: "same-origin" });
  if (r.status === 401 && !path.startsWith("/api/auth/login")) {
    location.href = "/login.html";
    throw new Error("Please log in.");
  }
  if (!r.ok) {
    const text = await r.text();
    let detail = text;
    try { detail = JSON.parse(text).detail ?? text; } catch { /* not JSON */ }
    throw new Error(typeof detail === "string" ? detail : `Error ${r.status}`);
  }
  return r.json();
}
const put = (path, body) => api(path, { method: "PUT", body: JSON.stringify(body) });
const post = (path, body = {}) => api(path, { method: "POST", body: JSON.stringify(body) });
const patch = (path, body) => api(path, { method: "PATCH", body: JSON.stringify(body) });
const del = (path) => api(path, { method: "DELETE" });

function copyButton(value, label = "Copy") {
  return `<button type="button" class="copy" data-copy="${esc(value)}" data-label="${esc(label)}">${esc(label)}</button>`;
}
document.addEventListener("click", async (e) => {
  const b = e.target.closest("[data-copy]");
  if (!b) return;
  try {
    await navigator.clipboard.writeText(b.dataset.copy);
    b.textContent = "Copied";
    b.classList.add("done");
    notice("Copied to the clipboard.");
  } catch { b.textContent = b.dataset.copy; }
  setTimeout(() => { b.textContent = b.dataset.label || "Copy"; b.classList.remove("done"); }, 1500);
});

// The security policy doesn't allow inline style attributes, so widths/positions are set here after rendering.
function applyDynamicStyles(root = document) {
  root.querySelectorAll("[data-width]").forEach((el) => { el.style.width = `${el.dataset.width}%`; });
  // The element spans its container, so translateX(n%) moves it n% of the container's width (animatable).
  root.querySelectorAll("[data-left]").forEach((el) => { el.style.transform = `translateX(${el.dataset.left}%)`; });
}

// Small form helper: shows a message under a form; kind = "ok" | "error". The element is a live region
// (role="status" in the markup) so screen readers read the message out.
function formMessage(el, text, kind = "error") {
  if (!el) return;
  el.setAttribute("role", kind === "error" ? "alert" : "status");
  el.textContent = text;
  el.className = `form-msg ${kind}`;
}

// Disables a button and shows e.g. "Saving…" while `fn` runs, so a double click can't send it twice.
async function withBusy(button, label, fn) {
  const old = button?.textContent;
  if (button) { button.disabled = true; button.textContent = label; }
  try {
    return await fn();
  } finally {
    if (button?.isConnected) { button.disabled = false; button.textContent = old; }
  }
}

// A short message at the bottom of the screen, for buttons that have no form of their own.
let noticeTimer;
function notice(text, kind = "ok") {
  let el = document.getElementById("toast");
  if (!el) {
    el = document.createElement("div");
    el.id = "toast";
    el.className = "toast";
    el.setAttribute("aria-live", "polite");
    document.body.append(el);
  }
  el.setAttribute("role", kind === "error" ? "alert" : "status");
  el.textContent = text;
  el.hidden = false;
  clearTimeout(noticeTimer);
  noticeTimer = setTimeout(() => { el.hidden = true; }, kind === "error" ? 8000 : 3000);
}
