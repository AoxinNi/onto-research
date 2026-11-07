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
const smsSend = document.querySelector("#sms-send");
const smsComplete = document.querySelector("#sms-complete");
const wechat = document.querySelector("#wechat-start");
const message = document.querySelector("#register-message");
const sendButton = smsSend.querySelector("button");
let canResendAt = 0;

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

smsSend.addEventListener("submit", async (event) => {
  event.preventDefault();
  sendButton.disabled = true;
  message.hidden = true;
  try {
    const result = await authRequest("/auth/sms/send", { phone: smsSend.elements.phone.value });
    canResendAt = Date.now() + result.retry_after * 1000;
    smsComplete.reset();
    smsComplete.hidden = false;
    smsComplete.elements.code.focus();
    showMessage("验证码已发送。");
    updateResend();
  } catch (error) {
    showMessage(error.message, true);
    updateResend();
  }
});

smsSend.elements.phone.addEventListener("input", () => {
  smsComplete.hidden = true;
});

smsComplete.addEventListener("submit", async (event) => {
  event.preventDefault();
  const submit = smsComplete.querySelector("button");
  submit.disabled = true;
  try {
    await authRequest("/auth/sms/complete", {
      phone: smsSend.elements.phone.value,
      code: smsComplete.elements.code.value,
    });
    location.replace("/");
  } catch (error) {
    showMessage(error.message, true);
    submit.disabled = false;
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
  smsSend.hidden = !providers.includes("sms");
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
