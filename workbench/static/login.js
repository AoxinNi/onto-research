/* Sign-in and self-service registration. The same page is the desktop app's first screen, where it also checks
   the saved session and lets the user pick which workbench server to connect to. */
import { desktop, go, request, routes } from "/static/net.js";

const SUPPORTED_API = 1;
const $ = (sel) => document.querySelector(sel);
const invoke = (cmd, args) => globalThis.__TAURI__.core.invoke(cmd, args);
const errorText = (error) => (typeof error === "string" ? error : error?.message) || "请求失败，请重试。";

const auth = $("#auth");
const form = $("#login-form");
const button = form.querySelector("button");
const loginError = $("#login-error");

const panel = $("#register-panel");
const title = $("#register-title");
const smsForm = $("#sms-form");
const smsComplete = $("#sms-complete");
const modeButton = $("#sms-mode");
const wechat = $("#wechat-start");
const message = $("#register-message");
const sendButton = $("#sms-send");
const submitButton = smsComplete.querySelector("button");
const { phone, code, password } = smsForm.elements;
let canResendAt = 0;
let intent = "register";

function signedIn() {
  form.reset();
  go(routes.projects, { replace: true });
}

// ------------------------------------------------------------------ password sign-in

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  button.disabled = true;
  loginError.hidden = true;
  try {
    const response = await request("/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(Object.fromEntries(new FormData(form))),
    });
    if (response.ok) {
      signedIn();
      return;
    }
    const body = await response.json().catch(() => ({}));
    loginError.textContent = body.detail || "登录失败，请重试。";
  } catch {
    loginError.textContent = "无法连接工作台，请检查网络后重试。";
  }
  loginError.hidden = false;
  button.disabled = false;
});

// ------------------------------------------------------------------ tabs

const tabs = { login: $("#tab-login"), register: $("#tab-register") };
function setTab(name) {
  for (const [key, tab] of Object.entries(tabs)) tab.setAttribute("aria-selected", String(key === name));
  form.hidden = name !== "login";
  panel.hidden = name !== "register";
  (name === "login" ? form.elements.username : phone)?.focus();
}
tabs.login.addEventListener("click", () => setTab("login"));
tabs.register.addEventListener("click", () => setTab("register"));
$("#auth-tabs").addEventListener("keydown", (event) => {
  if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
  const next = tabs.login.getAttribute("aria-selected") === "true" ? "register" : "login";
  setTab(next);
  tabs[next].focus();
});

// ------------------------------------------------------------------ SMS registration and password reset

function showMessage(text, failed = false) {
  message.hidden = false;
  message.textContent = text;
  message.classList.toggle("bad", failed);
}

async function authRequest(path, data = {}) {
  const response = await request(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(data),
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.detail || "请求失败，请重试。");
  return body;
}

function updateResend() {
  const remaining = Math.ceil((canResendAt - Date.now()) / 1000);
  sendButton.disabled = remaining > 0;
  sendButton.textContent = remaining > 0 ? `${remaining} 秒后可重发` : "发送验证码";
  if (remaining > 0) setTimeout(updateResend, 1000);
}

function showSmsFields(visible) {
  smsComplete.hidden = !visible;
  code.disabled = password.disabled = submitButton.disabled = !visible;
  code.value = password.value = "";
}

sendButton.addEventListener("click", async () => {
  if (phone.readOnly || !phone.reportValidity()) return;
  sendButton.disabled = true;
  phone.readOnly = modeButton.disabled = true;
  showSmsFields(false);
  message.hidden = true;
  try {
    const result = await authRequest("/auth/sms/send", { phone: phone.value, intent });
    canResendAt = Date.now() + result.retry_after * 1000;
    showSmsFields(true);
    code.focus();
    showMessage("验证码已发送。");
  } catch (error) {
    showMessage(errorText(error), true);
  } finally {
    phone.readOnly = modeButton.disabled = false;
    updateResend();
  }
});

phone.addEventListener("input", () => {
  showSmsFields(false);
  message.hidden = true;
});

modeButton.addEventListener("click", () => {
  intent = intent === "register" ? "reset" : "register";
  const reset = intent === "reset";
  title.textContent = reset ? "设置或重设密码" : "手机号注册";
  modeButton.textContent = reset ? "返回手机号注册" : "已有账号？设置或重设密码";
  submitButton.textContent = reset ? "验证并保存密码" : "验证并注册";
  showSmsFields(false);
  message.hidden = true;
});

smsForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (submitButton.disabled) return;
  submitButton.disabled = phone.readOnly = modeButton.disabled = true;
  try {
    await authRequest("/auth/sms/complete", { phone: phone.value, code: code.value, password: password.value, intent });
    signedIn();
  } catch (error) {
    showMessage(errorText(error), true);
    submitButton.disabled = phone.readOnly = modeButton.disabled = false;
  }
});

// ------------------------------------------------------------------ WeChat

wechat.addEventListener("click", async () => {
  wechat.disabled = true;
  try {
    if (desktop) {
      // The app opens WeChat in its own sign-in window and finishes the exchange natively.
      showMessage("请在弹出的窗口中用微信扫码。");
      await invoke("wechat_login");
      signedIn();
      return;
    }
    const { url } = await authRequest("/auth/wechat/start");
    location.assign(url);
  } catch (error) {
    showMessage(errorText(error), true);
    wechat.disabled = false;
  }
});

// ------------------------------------------------------------------ start

function showProviders(providers) {
  const open = providers.length > 0;
  $("#auth-tabs").hidden = !open;
  smsForm.hidden = !providers.includes("sms");
  if (!smsForm.hidden) title.textContent = "手机号注册";
  wechat.hidden = !providers.includes("wechat");
  setTab("login");
  if (!open) panel.hidden = true;
}

function showAuth() {
  auth.hidden = false;
  button.disabled = false;
  form.elements.username.focus();
}

async function startWeb() {
  showAuth();
  const response = await request("/auth/options");
  if (!response.ok) return;
  const { providers = [] } = await response.json();
  showProviders(providers);
  const error = new URLSearchParams(location.search).get("error");
  const errors = new Map([
    ["wechat-state", "扫码请求已过期，请重新开始。"],
    ["wechat-cancelled", "微信授权已取消，可以重新扫码。"],
    ["wechat-failed", "微信登录未完成，请重新扫码或联系工作台管理员。"],
  ]);
  if (errors.has(error)) {
    setTab("register");
    showMessage(errors.get(error), true);
    history.replaceState(null, "", "/login");
  }
}

// ---- desktop: saved session, server choice, connection errors

const boot = $("#boot-status");
const connection = $("#connection-error");
const serverPanel = $("#server-panel");
const serverForm = $("#server-form");
const serverError = $("#server-error");

function showServer(server) {
  $("#server-current").textContent = server.url.replace(/^https?:\/\//, "");
  serverForm.elements.url.value = server.url;
  $("#server-reset").hidden = server.isDefault;
  serverPanel.hidden = false;
}

async function startDesktop() {
  boot.hidden = false;
  auth.hidden = connection.hidden = true;
  let state;
  try {
    state = await invoke("session_boot");
  } catch (error) {
    boot.hidden = true;
    const server = await invoke("server_get").catch(() => null);
    if (server) showServer(server);
    $("#connection-error-text").textContent = errorText(error);
    connection.hidden = false;
    return;
  }
  showServer(state.server);
  if (!(state.api >= 1) || state.api > SUPPORTED_API) {
    boot.hidden = true;
    $("#connection-error-text").textContent = "该工作台的版本与此应用不兼容，请更新应用。";
    connection.hidden = false;
    return;
  }
  if (state.mode === "local" || state.authenticated) {
    go(routes.projects, { replace: true });
    return;
  }
  boot.hidden = true;
  showProviders(state.providers || []);
  showAuth();
}

if (desktop) {
  $("#connection-retry").addEventListener("click", startDesktop);
  $("#connection-change").addEventListener("click", () => {
    serverPanel.open = true;
    serverForm.elements.url.focus();
  });
  serverForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    serverError.hidden = true;
    try {
      showServer(await invoke("server_set", { url: serverForm.elements.url.value.trim() }));
      serverPanel.open = false;
      startDesktop();
    } catch (error) {
      serverError.textContent = errorText(error);
      serverError.hidden = false;
    }
  });
  $("#server-reset").addEventListener("click", async () => {
    showServer(await invoke("server_set", { url: null }));
    serverPanel.open = false;
    startDesktop();
  });
  startDesktop();
} else {
  startWeb().catch(() => {});
}
