// Settings page: password, two-step login, OKX key, notifications.

async function loadSettings() {
  const s = await api("/api/settings");
  const okx = s.okx;
  // One row per setting: what it is on the left, the controls on the right (stacked on phones).
  const row = (title, help, body) => `<div class="set-row"><div class="set-label"><h4>${title}</h4>${help ? `<p>${help}</p>` : ""}</div>
    <div class="set-body">${body}</div></div>`;
  const group = (title, rows) => `<section class="set-group"><h3>${title}</h3><div class="card set-card">${rows.join("")}</div></section>`;
  $("settings").innerHTML = `<div class="settings">
    ${group("Trading", [
      row("OKX spot fees", "Used for the trend bot’s trades (it pays the taker fee). The long/short test uses OKX futures fees: 0.05% plus 0.05% slippage per trade. On OKX: Profile → Fee rates.", `
        <form id="fees-form" class="form inline">
          <label>Maker % <input type="number" name="maker" step="0.001" min="0" max="1"
            value="${s.fees.maker != null ? +(s.fees.maker * 100).toFixed(4) : ""}" placeholder="${+(s.fees.default_maker * 100).toFixed(3)}" /></label>
          <label>Taker % <input type="number" name="taker" step="0.001" min="0" max="1"
            value="${s.fees.taker != null ? +(s.fees.taker * 100).toFixed(4) : ""}" placeholder="${+(s.fees.default_taker * 100).toFixed(3)}" /></label>
          <button class="btn small" type="submit">Save</button><span class="form-msg" id="fees-msg" role="status"></span>
        </form>
        <p class="muted small">${s.fees.taker != null ? "Using your fees." : `Not set: using ${num(s.fees.default_taker * 100, 3)}% taker.`}
          ${s.fees.okx_reported ? ` OKX’s API reports ${num(s.fees.okx_reported * 100, 3)}% for your account (some coins are in a higher fee group).` : ""}</p>`)
    ])}
    ${group("OKX connection", [
      row("Read-only API key", "Lets the app mirror your real OKX balances, trades and stop orders. It can never place orders or withdraw.",
        okx.configured
          ? `<p><span class="tag ok">Connected</span> Key ending <code>${esc(okx.key_hint)}</code> · ${okx.region === "eea" ? "European account (my.okx.com)" : "Global account"}
               ${okx.updated_at ? ` · saved ${esc(dateTime(okx.updated_at))}` : ""}</p>
             <div class="actions"><button type="button" class="btn small ghost" id="okx-test">Test connection</button>
               <button type="button" class="btn small ghost" id="okx-remove">Remove key</button><span class="form-msg" id="okx-msg" role="status"></span></div>
             <details class="set-more"><summary>Replace the key</summary>${okxForm(okx.region)}</details>`
          : `<p class="muted small">On OKX (in Europe: my.okx.com): Profile → API → Create API key, choose a passphrase and tick
               only <b>Read</b>. It’s tested before it’s saved and stored encrypted; a key that can withdraw is refused.</p>
             ${okxForm(okx.region)}`),
    ])}
    ${group("Alerts", [
      row("Phone notifications", "Sell alerts and finished demo trades on your phone. Install the ntfy app, subscribe to a hard-to-guess topic and paste its address.", `
        <form id="notify-form" class="form">
          <label>Notification address <input type="url" name="url" placeholder="https://ntfy.sh/your-secret-topic"
            autocomplete="off" spellcheck="false" autocapitalize="none" value="" /></label>
          <p class="muted small">${s.notify_url_set ? `<span class="tag ok">On</span> An address is saved. Enter a new one to replace it.` : "No address saved."}</p>
          <div class="actions"><button class="btn small" type="submit">Save</button><span class="form-msg" id="notify-msg" role="status"></span></div>
        </form>`),
    ])}
    ${group("Security", [
      row("Password", `Signed in as <b>${esc(s.user.username)}</b> (${esc(s.user.role)}). At least 12 characters; changing it logs you out everywhere else.`, `
        <details class="set-more" id="pw-box"><summary class="btn small ghost">Change password</summary>
          <form id="pw-form" class="form" autocomplete="on">
            <input type="text" name="username" value="${esc(s.user.username)}" autocomplete="username" hidden />
            <label>Current password <input type="password" name="current" autocomplete="current-password" required /></label>
            <label>New password <input type="password" name="new" autocomplete="new-password" minlength="12" required /></label>
            <label>Repeat new password <input type="password" name="repeat" autocomplete="new-password" minlength="12" required /></label>
            <div class="actions"><button class="btn small" type="submit">Save new password</button><span class="form-msg" id="pw-msg" role="status"></span></div>
          </form>
        </details>`),
      row("Two-step login", "A 6-digit code from an authenticator app (Google Authenticator, 1Password…) on every login. Recommended for admins.",
        `<div id="totp-box">${s.user.totp_enabled
          ? `<p><span class="tag ok">On</span> Logging in asks for a code from your authenticator app.</p>
             <details class="set-more"><summary class="btn small ghost">Turn off</summary>
             <form id="totp-off" class="form">
               <label>Password <input type="password" name="password" autocomplete="current-password" required /></label>
               <label>Code from your app <input type="text" name="code" inputmode="numeric" autocomplete="one-time-code" maxlength="6"
                 pattern="[0-9]{6}" spellcheck="false" required /></label>
               <div class="actions"><button class="btn small danger" type="submit">Turn off</button><span class="form-msg" id="totp-msg" role="status"></span></div>
             </form></details>`
          : `<p><span class="tag">Off</span></p>
             <div class="actions"><button type="button" class="btn small" id="totp-setup">Set up two-step login</button>
               <span class="form-msg" id="totp-setup-msg" role="status"></span></div>
             <div id="totp-setup-box"></div>`}</div>`),
    ])}
  </div>`;
  bindSettings(s);
}

function okxForm(region) {
  return `
    <form id="okx-form" class="form" autocomplete="off">
      <label>API key <input type="password" name="api_key" autocomplete="off" required /></label>
      <label>Secret key <input type="password" name="api_secret" autocomplete="off" required /></label>
      <label>Passphrase <input type="password" name="passphrase" autocomplete="off" required /></label>
      <label>Account <select name="region">
        <option value="eea" ${region !== "global" ? "selected" : ""}>European account (my.okx.com)</option>
        <option value="global" ${region === "global" ? "selected" : ""}>Global account (okx.com)</option></select></label>
      <div class="actions"><button class="btn small" type="submit">Test and save</button><span class="form-msg" id="okx-form-msg" role="status"></span></div>
    </form>`;
}

function bindSettings(s) {
  const submitOf = (form) => form.querySelector("[type=submit]");

  $("pw-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = e.target;
    if (f.new.value !== f.repeat.value) {
      formMessage($("pw-msg"), "The new passwords don’t match. Type the same password in both boxes.");
      return f.repeat.focus();
    }
    try {
      const r = await withBusy(submitOf(f), "Saving…", () => post("/api/auth/password", { current: f.current.value, new: f.new.value }));
      f.reset();
      formMessage($("pw-msg"), `Password changed${r.other_sessions_ended ? `; ${r.other_sessions_ended} other session(s) logged out` : ""}.`, "ok");
    } catch (err) { formMessage($("pw-msg"), err.message); f.current.focus(); }
  });

  const setup = $("totp-setup");
  if (setup) setup.addEventListener("click", async () => {
    let r;
    try {
      r = await withBusy(setup, "Setting up…", () => post("/api/auth/2fa/setup"));
    } catch (err) { return formMessage($("totp-setup-msg"), err.message); }
    $("totp-setup-box").innerHTML = `
      <ol class="steps">
        <li>In your authenticator app, add an account and choose to enter a key manually.</li>
        <li>Account name: <b>Copy Signals</b>. Key: <code translate="no">${esc(r.secret)}</code> ${copyButton(r.secret)}
          <div class="muted small">Or copy this setup link if your app accepts one: ${copyButton(r.uri, "Copy link")}</div></li>
        <li>Type the 6-digit code it shows below.</li>
      </ol>
      <form id="totp-on" class="form inline">
        <label>6-digit code <input type="text" name="code" inputmode="numeric" autocomplete="one-time-code" maxlength="6"
          pattern="[0-9]{6}" spellcheck="false" required /></label>
        <button class="btn small" type="submit">Turn on</button><span class="form-msg" id="totp-msg" role="status"></span>
      </form>`;
    $("totp-on").code.focus();
    $("totp-on").addEventListener("submit", async (e) => {
      e.preventDefault();
      try {
        await withBusy(submitOf(e.target), "Checking…", () => post("/api/auth/2fa/enable", { code: e.target.code.value.trim() }));
        notice("Two-step login is on.");
        loadSettings();
      } catch (err) { formMessage($("totp-msg"), err.message); e.target.code.select(); }
    });
  });
  const off = $("totp-off");
  if (off) off.addEventListener("submit", async (e) => {
    e.preventDefault();
    try {
      await withBusy(submitOf(e.target), "Turning off…",
        () => post("/api/auth/2fa/disable", { password: e.target.password.value, code: e.target.code.value.trim() }));
      notice("Two-step login is off.");
      loadSettings();
    } catch (err) { formMessage($("totp-msg"), err.message); }
  });

  const okxFormEl = $("okx-form");
  if (okxFormEl) okxFormEl.addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = e.target;
    formMessage($("okx-form-msg"), "Testing with OKX…", "ok");
    try {
      const r = await withBusy(submitOf(f), "Testing…", () => put("/api/settings/okx", { api_key: f.api_key.value,
        api_secret: f.api_secret.value, passphrase: f.passphrase.value, region: f.region.value }));
      f.reset();
      formMessage($("okx-form-msg"), `Saved. ${r.can_trade ? "This key can trade; a read-only key is safer." : "Read-only key confirmed."}`, "ok");
      setTimeout(loadSettings, 1200);
    } catch (err) { formMessage($("okx-form-msg"), err.message); }
  });
  const test = $("okx-test");
  if (test) test.addEventListener("click", async () => {
    formMessage($("okx-msg"), "Testing…", "ok");
    try {
      const r = await withBusy(test, "Testing…", () => post("/api/settings/okx/test"));
      formMessage($("okx-msg"), `Works. Permissions: ${r.permissions.join(", ") || "read"}.`, "ok");
    } catch (err) { formMessage($("okx-msg"), err.message); }
  });
  const remove = $("okx-remove");
  if (remove) remove.addEventListener("click", async () => {
    if (!confirm("Remove your OKX key from the app? You can add it again any time.")) return;
    try {
      await withBusy(remove, "Removing…", () => del("/api/settings/okx"));
    } catch (err) { return formMessage($("okx-msg"), err.message); }
    notice("OKX key removed.");
    loadSettings();
  });

  $("fees-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const maker = e.target.maker.value, taker = e.target.taker.value;
    if (maker === "" || taker === "") return formMessage($("fees-msg"), "Enter both fees, for example 0.1 and 0.2.");
    try {
      await withBusy(submitOf(e.target), "Saving…", () => put("/api/settings/prefs", { fee_maker: +maker / 100, fee_taker: +taker / 100 }));
      formMessage($("fees-msg"), "Saved.", "ok");
      setTimeout(loadSettings, 800);
    } catch (err) { formMessage($("fees-msg"), err.message); }
  });

  $("notify-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    try {
      await withBusy(submitOf(e.target), "Saving…", () => put("/api/settings/prefs", { notify_url: e.target.url.value.trim() }));
      e.target.reset();
      formMessage($("notify-msg"), "Saved.", "ok");
    } catch (err) { formMessage($("notify-msg"), err.message); e.target.url.focus(); }
  });
}

PAGE_LOADERS.settings = () => loadSettings().catch((e) => { $("settings").innerHTML = `<div class="empty">${esc(e.message)}</div>`; });
