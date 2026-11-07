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
