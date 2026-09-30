// Admin page (admins only; the server checks the role on every request).

const adminState = { tab: "overview", auditOffset: 0, auditFilters: {} };
const LEVEL_TAG = { info: "", warning: "tag bad", alert: "tag alert" };

async function loadAdmin() {
  const tab = adminState.tab;
  document.querySelectorAll("#admin-tabs button").forEach((b) => {
    b.classList.toggle("on", b.dataset.tab === tab);
    b.setAttribute("aria-pressed", String(b.dataset.tab === tab));
  });
  // The open tab is part of the address (#admin/users), so it can be linked and survives a reload.
  if (location.hash.startsWith("#admin") && location.hash !== `#admin/${tab}`) history.replaceState(null, "", `#admin/${tab}`);
  try {
    await ADMIN_TABS[tab]();
  } catch (e) {
    $("admin").innerHTML = `<div class="empty">${esc(e.message)}</div>`;
  }
}

const ADMIN_TABS = {
  async overview() {
    const s = await api("/api/admin/status");
    const sec = s.security;
    $("admin").innerHTML = `
      ${sec.alerts_24h ? `<div class="alert danger"><span>${sec.alerts_24h} security alert(s) in the last 24 hours. See the audit log.</span></div>` : ""}
      <div class="tiles">
        ${tile("Users", s.counts.users)}${tile("Active sessions", s.counts.active_sessions)}
        ${tile("Failed logins (24h)", sec.failed_logins_24h)}${tile("Locked accounts", sec.locked_accounts)}
        ${tile("Security warnings (24h)", sec.warnings_24h)}${tile("Database size", `${fixed(s.db_bytes / 1e6, 1)} MB`)}
        ${tile("Followed traders", s.counts.followed_traders)}${tile("Open demo trades", s.counts.open_demo_trades)}
        ${tile("Tracked picks open / closed", `${s.counts.tracked_picks_open} / ${s.counts.tracked_picks_closed}`)}
      </div>
      <h3>Data sources</h3>
      <div class="table-wrap"><table>
        <thead><tr><th>Source</th><th>Last success</th><th>Last attempt</th><th class="num">Took</th><th>Status</th></tr></thead>
        <tbody>${s.sources.map((r) => `<tr><td>${esc(r.source)}</td><td>${r.last_ok ? esc(ago(r.last_ok)) : "never"}</td>
          <td>${r.last_attempt ? esc(ago(r.last_attempt)) : "–"}</td><td class="num">${r.duration_s ?? "–"} s</td>
          <td>${r.last_error ? `<span class="down">${esc(r.last_error)}</span>` : `<span class="up">OK</span>`}</td></tr>`).join("")}</tbody>
      </table></div>
      <p class="note">Full refresh: ${s.refreshing ? "running now" : "idle"} · live prices: ${s.live_at ? esc(ago(s.live_at)) : "–"}
        (${s.stream.connected ? `streaming ${s.stream.fresh} of ${s.stream.pairs} coins from OKX` : "price stream down, polling every 10 s"})
        · rising-now scan: ${s.scan_at ? esc(ago(s.scan_at)) : "–"}</p>`;
  },

  async users() {
    const rows = await api("/api/admin/users");
    $("admin").innerHTML = `
      <div class="table-wrap"><table>
        <thead><tr><th>User</th><th>Role</th><th>Status</th><th>Two-step</th><th>OKX</th><th>Last login</th><th class="num">Sessions</th><th><span class="sr-only">Actions</span></th></tr></thead>
        <tbody>${rows.map((u) => `<tr data-id="${u.id}" data-name="${esc(u.username)}">
          <td><b>${esc(u.username)}</b></td><td>${esc(u.role)}</td>
          <td>${u.disabled ? `<span class="tag bad">disabled</span>` : u.locked ? `<span class="tag bad">locked</span>` : `<span class="tag ok">active</span>`}</td>
          <td>${u.totp_enabled ? "on" : "off"}</td><td>${u.okx ? "connected" : "–"}</td>
          <td>${u.last_login_at ? esc(dateTime(u.last_login_at)) : "never"}</td><td class="num">${u.sessions}</td>
          <td class="row-actions">
            ${u.locked ? `<button type="button" class="btn small ghost" data-act="unlock" aria-label="Unlock ${esc(u.username)}">Unlock</button>` : ""}
            <button type="button" class="btn small ghost" data-act="role" aria-label="Make ${esc(u.username)} ${u.role === "admin" ? "a normal user" : "an admin"}">${u.role === "admin" ? "Make user" : "Make admin"}</button>
            <button type="button" class="btn small ghost" data-act="disable" aria-label="${u.disabled ? "Enable" : "Disable"} ${esc(u.username)}">${u.disabled ? "Enable" : "Disable"}</button>
            <button type="button" class="btn small ghost" data-act="password" aria-label="Reset the password of ${esc(u.username)}">Reset password</button>
          </td></tr>`).join("")}</tbody>
      </table></div>
      <div class="card">
        <h3>Add a user</h3>
        <form id="new-user" class="form" autocomplete="off">
          <label>Username <input name="username" minlength="3" maxlength="32" required autocomplete="off" spellcheck="false" autocapitalize="none" /></label>
          <label>Password <input type="password" name="password" minlength="12" required autocomplete="new-password" /></label>
          <label>Role <select name="role"><option value="user">User</option><option value="admin">Admin</option></select></label>
          <div class="actions"><button class="btn small" type="submit">Create user</button><span class="form-msg" id="new-user-msg" role="status"></span></div>
        </form>
      </div>`;
    $("admin").querySelectorAll("[data-act]").forEach((b) => b.addEventListener("click", async () => {
      const tr = b.closest("tr");
      const id = tr.dataset.id, name = tr.dataset.name;
      const u = rows.find((x) => String(x.id) === id);
      if (b.dataset.act === "password") return openPasswordReset(tr, u);
      let body;
      if (b.dataset.act === "unlock") body = { unlock: true };
      if (b.dataset.act === "role") {
        const role = u.role === "admin" ? "user" : "admin";
        if (!confirm(`Make ${name} ${role === "admin" ? "an admin" : "a normal user"}?`)) return;
        body = { role };
      }
      if (b.dataset.act === "disable") {
        if (!confirm(`${u.disabled ? "Enable" : "Disable"} ${name}?${u.disabled ? "" : " They’ll be logged out."}`)) return;
        body = { disabled: !u.disabled };
      }
      try {
        await withBusy(b, "Saving…", () => patch(`/api/admin/users/${id}`, body));
        notice(`${name} updated.`);
      } catch (e) { return notice(e.message, "error"); }
      ADMIN_TABS.users();
    }));
    $("new-user").addEventListener("submit", async (e) => {
      e.preventDefault();
      const f = e.target;
      try {
        await withBusy(f.querySelector("[type=submit]"), "Creating…",
          () => post("/api/admin/users", { username: f.username.value.trim(), password: f.password.value, role: f.role.value }));
        notice(`User ${f.username.value.trim()} created.`);
        ADMIN_TABS.users();
      } catch (err) { formMessage($("new-user-msg"), err.message); }
    });
  },

  async sessions() {
    const rows = await api("/api/admin/sessions");
    $("admin").innerHTML = `<div class="table-wrap"><table>
      <thead><tr><th>User</th><th>Started</th><th>Last active</th><th>Expires</th><th>IP</th><th>Browser</th><th><span class="sr-only">Actions</span></th></tr></thead>
      <tbody>${rows.map((r) => `<tr data-id="${esc(r.id)}"><td>${esc(r.username)}</td><td>${esc(dateTime(r.created_at))}</td>
        <td>${esc(ago(r.last_seen))}</td><td>${esc(dateTime(r.expires_at))}</td><td>${esc(r.ip)}</td>
        <td class="clip" title="${esc(r.user_agent)}">${esc(r.user_agent)}</td>
        <td><button type="button" class="btn small ghost" data-act="end" aria-label="End the session of ${esc(r.username)}">End</button></td></tr>`).join("")}</tbody></table></div>`;
    $("admin").querySelectorAll("[data-act=end]").forEach((b) => b.addEventListener("click", async () => {
      if (!confirm("End this session? That browser will have to log in again.")) return;
      try {
        await withBusy(b, "Ending…", () => del(`/api/admin/sessions/${b.closest("tr").dataset.id}`));
      } catch (e) { return notice(e.message, "error"); }
      notice("Session ended.");
      ADMIN_TABS.sessions();
    }));
  },

  async audit() {
    const f = adminState.auditFilters;
    const q = new URLSearchParams({ limit: 50, offset: adminState.auditOffset });
    for (const [k, v] of Object.entries(f)) if (v) q.set(k, v);
    const data = await api(`/api/admin/audit?${q}`);
    $("admin").innerHTML = `
      <form id="audit-filter" class="form inline audit-filter">
        <label>User <input name="username" placeholder="Any user…" value="${esc(f.username || "")}"
          autocomplete="off" spellcheck="false" autocapitalize="none" /></label>
        <label>Action <input name="action" placeholder="auth., demo., settings…" value="${esc(f.action || "")}"
          autocomplete="off" spellcheck="false" autocapitalize="none" /></label>
        <label>Level <select name="level"><option value="">All levels</option>
          ${["info", "warning", "alert"].map((l) => `<option ${f.level === l ? "selected" : ""}>${l}</option>`).join("")}</select></label>
        <button class="btn small" type="submit">Filter</button>
      </form>
      <div class="table-wrap"><table>
        <thead><tr><th>Time</th><th>Level</th><th>User</th><th>Action</th><th>Details</th><th>IP</th></tr></thead>
        <tbody>${data.rows.map((r) => `<tr><td>${esc(dateTime(r.ts))}</td>
          <td><span class="${LEVEL_TAG[r.level] || ""}">${esc(r.level)}</span></td><td>${esc(r.username || "–")}</td>
          <td><code>${esc(r.action)}</code></td><td class="clip wide" title="${esc(r.detail || "")}">${esc(r.detail || "")}</td>
          <td>${esc(r.ip || "")}</td></tr>`).join("")}</tbody></table></div>
      <div class="actions">
        <span class="muted">${data.total} entries · showing ${data.total ? adminState.auditOffset + 1 : 0}–${Math.min(data.total, adminState.auditOffset + 50)}</span>
        <button type="button" class="btn small ghost" id="audit-prev" ${adminState.auditOffset ? "" : "disabled"}>Newer</button>
        <button type="button" class="btn small ghost" id="audit-next" ${adminState.auditOffset + 50 < data.total ? "" : "disabled"}>Older</button>
      </div>`;
    $("audit-filter").addEventListener("submit", (e) => {
      e.preventDefault();
      const t = e.target;
      adminState.auditFilters = { username: t.username.value.trim(), action: t.action.value.trim(), level: t.level.value };
      adminState.auditOffset = 0;
      ADMIN_TABS.audit();
    });
    $("audit-prev").addEventListener("click", () => { adminState.auditOffset = Math.max(0, adminState.auditOffset - 50); ADMIN_TABS.audit(); });
    $("audit-next").addEventListener("click", () => { adminState.auditOffset += 50; ADMIN_TABS.audit(); });
  },

  async appsettings() {
    const s = await api("/api/admin/settings");
    $("admin").innerHTML = `<div class="card"><form id="app-settings" class="form">
      ${Object.entries(s).map(([k, v]) => `<label>${esc(v.label)}
        <input type="number" name="${esc(k)}" value="${esc(v.value)}" min="${esc(v.min)}" max="${esc(v.max)}" step="any" />
        <span class="muted small">default ${esc(v.default)}</span></label>`).join("")}
      <div class="actions"><button class="btn small" type="submit">Save</button><span class="form-msg" id="app-settings-msg" role="status"></span></div>
    </form></div>`;
    $("app-settings").addEventListener("submit", async (e) => {
      e.preventDefault();
      const values = Object.fromEntries([...e.target.querySelectorAll("input")].map((i) => [i.name, +i.value]));
      try {
        await withBusy(e.target.querySelector("[type=submit]"), "Saving…", () => put("/api/admin/settings", { values }));
        formMessage($("app-settings-msg"), "Saved.", "ok");
      } catch (err) { formMessage($("app-settings-msg"), err.message); }
    });
  },
};

// Strategy lab: the paper trades replayed under other exit rules (backend/app/replay.py).
const LAB_STYLES = { all: "All", pick: "Picks", early: "Early", pump: "Pump" };

function labTable(rule) {
  const rows = [["all", rule.all], ...Object.entries(rule.by_style)].filter(([, s]) => s.trades || s.open);
  const signed = (v) => `<span class="${v > 0 ? "up" : v < 0 ? "down" : ""}">${pct(v, 2)}</span>`;
  return `<div class="table-wrap"><table>
    <thead><tr><th>Type</th><th class="num">Closed</th><th class="num">Won</th><th class="num">Avg result</th>
      <th class="num">Total</th><th class="num">Avg hours</th><th class="num">Still open</th><th class="num">Open, marked now</th></tr></thead>
    <tbody>${rows.map(([k, s]) => `<tr><td>${esc(LAB_STYLES[k] || k)}</td><td class="num">${s.trades}</td>
      <td class="num">${s.win_rate == null ? "–" : pct(s.win_rate, 0).replace("+", "")}</td>
      <td class="num">${s.avg_net == null ? "–" : signed(s.avg_net)}</td><td class="num">${signed(s.total_net)}</td>
      <td class="num">${s.avg_hours == null ? "–" : fixed(s.avg_hours, 1)}</td><td class="num">${s.open}</td>
      <td class="num">${s.open ? signed(s.open_net) : "–"}</td></tr>`).join("")}</tbody></table></div>
    <p class="note">Exits: ${Object.entries(rule.all.by_reason).map(([r, v]) => `${esc(r)} ${v.trades} (${pct(v.avg_net, 2)})`).join(" · ") || "none yet"}</p>`;
}

ADMIN_TABS.lab = async () => {
  const data = await api("/api/admin/lab");
  const r = data.result;
  $("admin").innerHTML = `
    <div class="card">
      <p>Every paper trade is replayed on 5-minute OKX prices under each exit rule, with the same costs. A coin is held once at a time, so a
        rule that keeps a trade open also skips the re-buys made meanwhile. Totals add up equal-sized trades (share of one trade).
        Use this to compare rules before changing the live ones. It isn't proof: small samples and one market period can mislead.</p>
      <div class="actions"><button type="button" class="btn small" id="lab-run" ${data.running ? "disabled" : ""}>${data.running ? "Running…" : "Run the replay"}</button>
        <span class="muted small">${r ? `Last run ${esc(ago(r.now))} · takes about a minute` : "Not run yet · takes about a minute"}</span></div>
    </div>
    ${r ? Object.entries(r.rules).map(([k, rule]) => `<h3>${esc(rule.label)}</h3>${labTable(rule)}`).join("") : ""}
    ${r && r.missing.length ? `<p class="note">No OKX price history for: ${esc(r.missing.join(", "))}</p>` : ""}`;
  $("lab-run").addEventListener("click", async (e) => {
    try {
      await withBusy(e.target, "Running…", () => post("/api/admin/lab"));
    } catch (err) { return notice(err.message, "error"); }
    ADMIN_TABS.lab();
  });
};

// Reset password: a masked field typed twice, instead of a browser prompt that shows the password.
function openPasswordReset(tr, u) {
  const existing = document.getElementById("pw-reset-row");
  if (existing) existing.remove();
  const row = document.createElement("tr");
  row.id = "pw-reset-row";
  row.innerHTML = `<td colspan="8">
    <form class="row-form" id="pw-reset-form" autocomplete="off">
      <input type="text" name="username" value="${esc(u.username)}" autocomplete="username" hidden />
      <label>New password for ${esc(u.username)} <input type="password" name="pw" minlength="12" required autocomplete="new-password" /></label>
      <label>Repeat <input type="password" name="repeat" minlength="12" required autocomplete="new-password" /></label>
      <button class="btn small" type="submit">Set password</button>
      <button class="btn small ghost" type="button" data-act="cancel">Cancel</button>
      <span class="form-msg" role="status">At least 12 characters. They’ll be logged out everywhere.</span>
    </form></td>`;
  tr.after(row);
  const form = row.querySelector("form");
  form.pw.focus();
  form.querySelector("[data-act=cancel]").addEventListener("click", () => row.remove());
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const msg = form.querySelector(".form-msg");
    if (form.pw.value.length < 12) { formMessage(msg, "Use at least 12 characters."); return form.pw.focus(); }
    if (form.pw.value !== form.repeat.value) { formMessage(msg, "The passwords don’t match. Type the same one in both boxes."); return form.repeat.focus(); }
    try {
      await withBusy(form.querySelector("[type=submit]"), "Saving…", () => patch(`/api/admin/users/${u.id}`, { new_password: form.pw.value }));
    } catch (err) { return formMessage(msg, err.message); }
    notice(`Password for ${u.username} changed; they’ve been logged out.`);
    ADMIN_TABS.users();
  });
}

$("admin-tabs").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-tab]");
  if (!b) return;
  adminState.tab = b.dataset.tab;
  loadAdmin();
});

PAGE_LOADERS.admin = (sub) => {
  if (sub && ADMIN_TABS[sub]) adminState.tab = sub;
  return loadAdmin();
};
