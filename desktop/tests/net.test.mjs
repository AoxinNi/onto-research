// The shared transport in desktop mode, against a fake Tauri bridge. Run with `npm test`.
import assert from "node:assert/strict";
import { test } from "node:test";

const calls = [];
const replies = new Map();
let lastChannel = null;

class Channel {
  constructor() { lastChannel = this; this.onmessage = null; }
}
globalThis.__TAURI__ = {
  core: {
    Channel,
    invoke: async (cmd, args) => {
      calls.push([cmd, args]);
      const reply = replies.get(cmd);
      if (reply instanceof Error) throw reply.message;
      return typeof reply === "function" ? reply(args) : reply;
    },
  },
};
globalThis.location = { search: "?project=btc-1&view=sources", pathname: "/workspace.html", assign() {}, replace() {} };
globalThis.document = { readyState: "loading", addEventListener() {} };
globalThis.DOMException ??= class extends Error {};

const net = await import("../../workbench/static/net.js");

test("desktop routes point at the bundled pages", () => {
  assert.equal(net.desktop, true);
  assert.equal(net.routes.login, "/index.html");
  assert.equal(net.routes.projects, "/home.html");
  assert.equal(net.routes.view("settings"), "/home.html?view=settings");
  assert.equal(net.routes.view("projects"), "/home.html");
  assert.equal(net.routes.project("a b"), "/workspace.html?project=a%20b");
  assert.equal(net.currentProject(), "btc-1");
  assert.equal(net.currentView(), "sources");
});

test("request goes through the Rust bridge and returns a real Response", async () => {
  replies.set("api_request", { status: 200, body: '[{"id":"p"}]', contentType: "application/json" });
  const response = await net.request("/api/projects", { method: "post", body: '{"name":"x"}' });
  assert.deepEqual(calls.at(-1), ["api_request", { method: "POST", path: "/api/projects", body: '{"name":"x"}' }]);
  assert.equal(response.ok, true);
  assert.deepEqual(await response.json(), [{ id: "p" }]);

  replies.set("api_request", { status: 204, body: "" });
  assert.equal((await net.request("/auth/login", { method: "POST", body: "{}" })).status, 204);

  replies.set("api_request", { status: 401, body: '{"detail":"请先登录。"}', contentType: "application/json" });
  const unauthorized = await net.request("/api/projects");
  assert.equal(unauthorized.status, 401);
  assert.equal((await unauthorized.json()).detail, "请先登录。");
});

test("an unreachable server rejects like fetch does", async () => {
  replies.set("api_request", new Error("无法连接服务器，请检查网络或服务器地址。"));
  await assert.rejects(net.request("/api/projects"), (error) => error instanceof TypeError && /无法连接/.test(error.message));
});

test("the desktop socket behaves like a WebSocket", async () => {
  replies.set("socket_open", 7);
  const ws = net.socket("/ws?project=btc-1");
  const seen = [];
  ws.onopen = () => seen.push("open");
  ws.onmessage = (event) => seen.push(JSON.parse(event.data).type);
  ws.onclose = (event) => seen.push(`close:${event.code}`);
  assert.equal(ws.readyState, 0);
  assert.throws(() => ws.send("{}"));

  lastChannel.onmessage({ kind: "open" });
  assert.equal(ws.readyState, 1);
  ws.send('{"type":"chat"}');
  await new Promise((resolve) => setTimeout(resolve));
  assert.deepEqual(calls.at(-1), ["socket_send", { id: 7, text: '{"type":"chat"}' }]);

  lastChannel.onmessage({ kind: "message", text: '{"type":"graph"}' });
  lastChannel.onmessage({ kind: "close", code: 1008 });
  lastChannel.onmessage({ kind: "message", text: '{"type":"late"}' });
  assert.equal(ws.readyState, 3);
  assert.deepEqual(seen, ["open", "graph", "close:1008"]);
});

test("a socket that cannot open reports an abnormal close", async () => {
  replies.set("socket_open", new Error("不支持的请求地址。"));
  const ws = net.socket("/ws");
  const closed = new Promise((resolve) => { ws.onclose = resolve; });
  assert.equal((await closed).code, 1006);
});

test("dialogs and downloads use native commands", async () => {
  replies.set("confirm_dialog", false);
  assert.equal(await net.confirmAction("归档？"), false);
  replies.set("download_file", true);
  assert.equal(await net.download("/api/projects/p/export", "p.json"), true);
  assert.deepEqual(calls.at(-1), ["download_file", { path: "/api/projects/p/export", filename: "p.json" }]);
});
