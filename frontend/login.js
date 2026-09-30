const form = document.getElementById("login-form");
const msg = document.getElementById("login-msg");

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  msg.textContent = "";
  const body = { username: form.username.value.trim(), password: form.password.value };
  if (!document.getElementById("code-row").hidden && form.code.value) body.code = form.code.value.trim();
  const button = form.querySelector("button");
  button.disabled = true;
  button.textContent = "Logging in…";
  try {
    const r = await fetch("/api/auth/login", {
      method: "POST", headers: { "Content-Type": "application/json" }, credentials: "same-origin", body: JSON.stringify(body),
    });
    const data = await r.json().catch(() => ({}));
    if (r.ok) {
      location.href = "/";
      return;
    }
    if (data.code_required) {
      document.getElementById("code-row").hidden = false;
      form.code.focus();
    }
    msg.textContent = data.detail || "Couldn’t log in.";
    msg.className = "form-msg error";
  } catch {
    msg.textContent = "The app isn’t reachable. Check that it’s running, then try again.";
    msg.className = "form-msg error";
  } finally {
    button.disabled = false;
    button.textContent = "Log in";
  }
});
