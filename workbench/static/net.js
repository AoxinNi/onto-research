/* Transport and navigation shared by the web workbench and the desktop app.

   In a browser this is plain fetch / WebSocket / location. Inside the Tauri desktop app every request goes
   through the Rust side, which holds the session in the OS credential store and adds it as a bearer token, so
   the web view never sees a credential. Pages call these helpers instead of the browser APIs directly. */

const tauri = globalThis.__TAURI__?.core;
export const desktop = Boolean(tauri);
const invoke = (cmd, args) => tauri.invoke(cmd, args);

// ------------------------------------------------------------------ routes

const WEB = {
  login: "/login",
  projects: "/",
  view: (view) => (view === "projects" ? "/" : `/${view}`),
  project: (id) => `/p/${encodeURIComponent(id)}`,
};
const APP = {
  login: "/index.html",
  projects: "/home.html",
  view: (view) => (view === "projects" ? "/home.html" : `/home.html?view=${encodeURIComponent(view)}`),
  project: (id) => `/workspace.html?project=${encodeURIComponent(id)}`,
};
export const routes = desktop ? APP : WEB;

export function currentProject() {
  if (desktop) return new URLSearchParams(location.search).get("project") || undefined;
  const m = location.pathname.match(/^\/p\/([^/]+)/);
  return m ? decodeURIComponent(m[1]) : undefined;
}

const VIEWS = new Set(["projects", "sources", "mcp", "settings"]);
export function currentView() {
  const view = desktop ? new URLSearchParams(location.search).get("view") : location.pathname.slice(1);
  return VIEWS.has(view) ? view : "projects";
}

export function go(url, { replace = false } = {}) {
  if (replace) location.replace(url);
  else location.assign(url);
}

// ------------------------------------------------------------------ http

/** fetch() for workbench paths ("/api/…", "/auth/…"). Returns a real Response; rejects with TypeError when the
    server cannot be reached, as fetch does. */
export async function request(path, opts = {}) {
  if (!desktop) return fetch(path, opts);
  const method = (opts.method || "GET").toUpperCase();
  const body = opts.body == null ? null : String(opts.body);
  let res;
  try {
    res = await invoke("api_request", { method, path, body });
  } catch (error) {
    throw new TypeError(typeof error === "string" ? error : error?.message || "无法连接服务器");
  }
  const empty = [101, 204, 205, 304].includes(res.status);
  return new Response(empty ? null : res.body, {
    status: res.status,
    headers: res.contentType ? { "content-type": res.contentType } : {},
  });
}

/** Save a server file (an export) to disk. In the browser this is a normal download. */
export async function download(path, filename) {
  if (!desktop) {
    const a = Object.assign(document.createElement("a"), { href: path, download: filename || "" });
    document.body.append(a);
    a.click();
    a.remove();
    return true;
  }
  return invoke("download_file", { path, filename: filename || null });
}

export async function confirmAction(message) {
  return desktop ? invoke("confirm_dialog", { message }) : confirm(message);
}

export async function openExternal(url) {
  if (desktop) await invoke("open_external", { url });
  else window.open(url, "_blank", "noopener,noreferrer");
}

// ------------------------------------------------------------------ websocket

/** A WebSocket-shaped object for desktop: onopen / onmessage / onclose / onerror, send(), close(), readyState. */
class DesktopSocket {
  static CONNECTING = 0;
  static OPEN = 1;
  static CLOSING = 2;
  static CLOSED = 3;

  constructor(path) {
    this.readyState = DesktopSocket.CONNECTING;
    this.onopen = this.onmessage = this.onclose = this.onerror = null;
    const channel = new tauri.Channel();
    channel.onmessage = (event) => this.#dispatch(event);
    this.id = invoke("socket_open", { path, onEvent: channel }).catch((error) => {
      this.#dispatch({ kind: "error", message: String(error) });
      this.#dispatch({ kind: "close", code: 1006 });
      return null;
    });
  }

  #dispatch(event) {
    if (this.readyState === DesktopSocket.CLOSED) return;
    if (event.kind === "open") {
      this.readyState = DesktopSocket.OPEN;
      this.onopen?.({ type: "open" });
    } else if (event.kind === "message") {
      this.onmessage?.({ type: "message", data: event.text });
    } else if (event.kind === "error") {
      this.onerror?.({ type: "error", message: event.message });
    } else if (event.kind === "close") {
      this.readyState = DesktopSocket.CLOSED;
      this.onclose?.({ type: "close", code: event.code ?? 1006, reason: event.reason || "" });
    }
  }

  send(text) {
    if (this.readyState !== DesktopSocket.OPEN) throw new DOMException("socket is not open", "InvalidStateError");
    this.id.then((id) => id != null && invoke("socket_send", { id, text })).catch(() => {});
  }

  close() {
    if (this.readyState >= DesktopSocket.CLOSING) return;
    this.readyState = DesktopSocket.CLOSING;
    this.id.then((id) => id != null && invoke("socket_close", { id })).catch(() => {});
  }
}

/** Open the workbench socket at a path such as "/ws?project=…". */
export function socket(path) {
  if (desktop) return new DesktopSocket(path);
  return new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}${path}`);
}

// ------------------------------------------------------------------ desktop page wiring

/* Pages keep their web links ("/", "/sources", "/api/…" downloads). In the app those are rewritten to the
   bundled pages, downloads go through a save dialog, and outside links open in the system browser. */
function internalTarget(href) {
  if (href === "/" || href === "") return routes.projects;
  const view = href.slice(1);
  if (VIEWS.has(view)) return routes.view(view);
  const project = href.match(/^\/p\/([^/?#]+)/);
  if (project) return routes.project(decodeURIComponent(project[1]));
  return null;
}

function wireDesktopPage() {
  document.documentElement.classList.add("desktop");
  for (const a of document.querySelectorAll("a[href^='/']")) {
    const target = internalTarget(a.getAttribute("href"));
    if (target) a.setAttribute("href", target);
  }
  document.addEventListener("click", (event) => {
    const a = event.target.closest?.("a[href]");
    if (!a || event.defaultPrevented) return;
    const href = a.getAttribute("href");
    if (/^https?:\/\//i.test(href)) {
      event.preventDefault();
      openExternal(href).catch(() => {});
    } else if (href.startsWith("/api/") && a.hasAttribute("download")) {
      event.preventDefault();
      download(href, a.getAttribute("download") || null).catch(() => {});
    }
  });
}

if (desktop) {
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", wireDesktopPage);
  else wireDesktopPage();
}
