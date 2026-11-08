const form = document.querySelector("#login-form");
const button = form.querySelector("button");
const error = document.querySelector("#login-error");
button.disabled = false;

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  button.disabled = true;
  error.hidden = true;
  try {
    const response = await fetch("/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(Object.fromEntries(new FormData(form))),
    });
    if (response.ok) {
      form.reset();
      location.replace("/");
      return;
    }
    const body = await response.json().catch(() => ({}));
    error.textContent = body.detail || "登录失败，请重试。";
  } catch {
    error.textContent = "无法连接工作台，请重试。";
  }
  error.hidden = false;
  button.disabled = false;
});

const panel = document.querySelector("#register-panel");
const title = document.querySelector("#register-title");
const smsForm = document.querySelector("#sms-form");
const smsComplete = document.querySelector("#sms-complete");
const modeButton = document.querySelector("#sms-mode");
const wechat = document.querySelector("#wechat-start");
const message = document.querySelector("#register-message");
const sendButton = document.querySelector("#sms-send");
const submitButton = smsComplete.querySelector("button");
const { phone, code, password } = smsForm.elements;
let canResendAt = 0;
let intent = "register";

function showMessage(text, failed = false) {
  message.hidden = false;
  message.textContent = text;
  message.classList.toggle("bad", failed);
}

async function authRequest(path, data = {}) {
  const response = await fetch(path, {
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
    showMessage(error.message, true);
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
    await authRequest("/auth/sms/complete", {
      phone: phone.value,
      code: code.value,
      password: password.value,
      intent,
    });
    location.replace("/");
  } catch (error) {
    showMessage(error.message, true);
    submitButton.disabled = phone.readOnly = modeButton.disabled = false;
  }
});

wechat.addEventListener("click", async () => {
  wechat.disabled = true;
  try {
    const { url } = await authRequest("/auth/wechat/start");
    location.assign(url);
  } catch (error) {
    showMessage(error.message, true);
    wechat.disabled = false;
  }
});

async function showRegistrationOptions() {
  const response = await fetch("/auth/options");
  if (!response.ok) return;
  const { providers } = await response.json();
  if (!providers.length) return;
  panel.hidden = false;
  smsForm.hidden = !providers.includes("sms");
  if (!smsForm.hidden) title.textContent = "手机号注册";
  wechat.hidden = !providers.includes("wechat");
  const error = new URLSearchParams(location.search).get("error");
  const errors = new Map([
    ["wechat-state", "扫码请求已过期，请重新开始。"],
    ["wechat-cancelled", "微信授权已取消，可以重新扫码。"],
    ["wechat-failed", "微信登录未完成，请重新扫码或联系工作台管理员。"],
  ]);
  if (errors.has(error)) {
    showMessage(errors.get(error), true);
    history.replaceState(null, "", "/login");
  }
}

showRegistrationOptions().catch(() => {});
