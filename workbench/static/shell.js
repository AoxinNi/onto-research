/* The workbench shell around a project: an activity bar, open-editor tabs, a status bar, quick open (⌘P) and a
   command palette (⌘⇧P). The panes (graph, library, writing) stay where they are; the shell only knows how to
   reach them, through the commands, quick-open sources and tabs that app.js registers. */

const el = (tag, attrs = {}, ...children) => {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null || v === false) continue;
    if (k === "class") node.className = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children) if (c != null && c !== false) node.append(c);
  return node;
};

const MAC = /Mac|iPhone|iPad/.test(navigator.platform);
/** "mod+shift+p" → "⌘⇧P" on a Mac, "Ctrl+Shift+P" elsewhere. */
export function keyLabel(keys) {
  if (!keys) return "";
  const parts = keys.split("+");
  const name = { mod: MAC ? "⌘" : "Ctrl", shift: MAC ? "⇧" : "Shift", alt: MAC ? "⌥" : "Alt", "\\": "\\" };
  return parts.map((p) => name[p] || p.toUpperCase()).join(MAC ? "" : "+");
}

/** Characters of the query in order, closest together and earliest first. null when they aren't all there. */
export function fuzzy(query, text) {
  const qs = [...query.toLowerCase().replace(/\s+/g, "")], t = text.toLowerCase();
  if (!qs.length) return { score: 0, hits: [] };
  const hits = [];
  let at = 0, score = 0, prev = -2;
  for (const c of qs) {
    const i = t.indexOf(c, at);
    if (i < 0) return null;
    score += i === prev + 1 ? 3 : 1;          // runs of matching characters count more
    if (i === 0 || /[\s\-_/·.:：]/.test(t[i - 1])) score += 2;  // so do word starts
    hits.push(i);
    prev = i; at = i + 1;
  }
  return { score: score - hits[0] * 0.05 - t.length * 0.002, hits };
}

function highlight(text, hits) {
  if (!hits?.length) return [text];
  const out = [], set = new Set(hits);
  let run = "";
  let inHit = false;
  [...text].forEach((ch, i) => {
    const hit = set.has(i);
    if (hit !== inHit && run) { out.push(inHit ? el("b", {}, run) : run); run = ""; }
    inHit = hit; run += ch;
  });
  if (run) out.push(inHit ? el("b", {}, run) : run);
  return out;
}

export function createShell({ storageKey, toast = () => {} }) {
  const commands = new Map();   // id → {id, title, group, keys, when, run}
  const sources = [];           // quick-open providers: async (query) → [{kind, label, detail, run}]
  const store = {
    get(k, d) { try { return JSON.parse(localStorage.getItem(`${storageKey}.${k}`)) ?? d; } catch { return d; } },
    set(k, v) { try { localStorage.setItem(`${storageKey}.${k}`, JSON.stringify(v)); } catch { /* private mode */ } },
  };

  // ---------------------------------------------------------------- activity bar

  const rail = el("nav", { id: "rail", class: "glass", "aria-label": "工作台" });
  const railTop = el("div", { class: "rail-group" }), railBottom = el("div", { class: "rail-group" });
  rail.append(railTop, el("div", { class: "rail-fill" }), railBottom);
  const railButtons = new Map();

  function railItem({ id, icon, tip, command, bottom = false, href }) {
    const c = commands.get(command);
    const label = `${tip}${c?.keys ? ` · ${keyLabel(c.keys)}` : ""}`;
    const b = href ? el("a", { class: "rail-btn", href, "data-tip": label, "aria-label": tip })
      : el("button", { type: "button", class: "rail-btn", "data-tip": label, "aria-label": tip, onclick: () => run(command) });
    b.innerHTML = icon;
    b.dataset.id = id;
    (bottom ? railBottom : railTop).append(b);
    railButtons.set(id, b);
  }
  const railGap = () => railTop.append(el("span", { class: "rail-sep", "aria-hidden": "true" }));
  function setActive(id) {
    for (const [k, b] of railButtons) {
      if (b.tagName === "A") continue;
      b.classList.toggle("on", k === id);
      if (b.hasAttribute("aria-pressed") || k === id) b.setAttribute("aria-pressed", String(k === id));
    }
  }

  // ---------------------------------------------------------------- commands

  function command(c) { commands.set(c.id, { group: "", ...c }); }
  function run(id, ...args) {
    const c = commands.get(id);
    if (!c) return;
    if (c.when && !c.when()) { toast(c.unavailable || "现在不能用这个命令", true); return; }
    Promise.resolve().then(() => c.run(...args)).catch((e) => toast(e.message || String(e), true));
  }

  const RECENT = store.get("recent", []);
  function remember(id) {
    const i = RECENT.indexOf(id);
    if (i >= 0) RECENT.splice(i, 1);
    RECENT.unshift(id);
    RECENT.length = Math.min(RECENT.length, 8);
    store.set("recent", RECENT);
  }

  function matchKeys(e, keys) {
    const parts = keys.split("+");
    const key = parts[parts.length - 1];
    const mod = parts.includes("mod"), shift = parts.includes("shift"), alt = parts.includes("alt");
    if (mod !== (e.metaKey || e.ctrlKey) || shift !== e.shiftKey || alt !== e.altKey) return false;
    return e.key.toLowerCase() === key || e.code === `Key${key.toUpperCase()}` || (key === "\\" && e.code === "Backslash");
  }
  // Only chords with ⌘/Ctrl and function keys here; single keys stay with app.js, which knows when the person is typing.
  const FKEY = /(^|\+)f\d{1,2}$/;
  addEventListener("keydown", (e) => {
    const fkey = /^F\d{1,2}$/.test(e.key);
    if (e.isComposing || !(e.metaKey || e.ctrlKey || fkey)) return;
    for (const c of commands.values()) {
      if ((c.keys?.includes("mod") || FKEY.test(c.keys || "")) && matchKeys(e, c.keys)) { e.preventDefault(); e.stopPropagation(); remember(c.id); run(c.id); return; }
    }
  }, true);

  // ---------------------------------------------------------------- palette (quick open and commands)

  const input = el("input", { type: "text", class: "pal-input", spellcheck: "false", autocomplete: "off",
    "aria-label": "快速打开", "aria-controls": "pal-list", "aria-autocomplete": "list" });
  const list = el("ul", { id: "pal-list", class: "pal-list", role: "listbox" });
  const foot = el("div", { class: "pal-foot" });
  const palette = el("div", { id: "palette", class: "glass", role: "dialog", "aria-modal": "true", "aria-label": "快速打开", hidden: true },
    input, list, foot);
  const scrim = el("div", { id: "pal-scrim", hidden: true, onpointerdown: () => closePalette() });
  let rows = [], active = 0, seq = 0, timer = null, lastFocus = null;

  function openPalette(prefix = "") {
    lastFocus = document.activeElement;
    palette.hidden = scrim.hidden = false;
    input.value = prefix;
    input.focus();
    refresh();
  }
  function closePalette() {
    if (palette.hidden) return;
    palette.hidden = scrim.hidden = true;
    clearTimeout(timer);
    seq++;
    if (lastFocus?.isConnected) lastFocus.focus?.();
  }

  function commandRows(query) {
    const all = [...commands.values()].filter((c) => !c.hidden && (!c.when || c.when()));
    if (!query) {
      const recent = RECENT.map((id) => commands.get(id)).filter((c) => c && all.includes(c));
      const rest = all.filter((c) => !recent.includes(c));
      return [...recent.map((c) => ({ c, recent: true })), ...rest.map((c) => ({ c }))]
        .map(({ c, recent }) => ({ kind: "command", label: `${c.group ? `${c.group}：` : ""}${c.title}`, keys: c.keys, detail: recent ? "最近用过" : "", run: () => { remember(c.id); run(c.id); } }));
    }
    return all.map((c) => {
      const label = `${c.group ? `${c.group}：` : ""}${c.title}`;
      const m = fuzzy(query, label) || (c.aliases && fuzzy(query, c.aliases) && { score: 0, hits: [] });
      return m && { kind: "command", label, keys: c.keys, hits: m.hits, score: m.score, run: () => { remember(c.id); run(c.id); } };
    }).filter(Boolean).sort((a, b) => b.score - a.score);
  }

  async function fileRows(query, mine) {
    const batches = await Promise.all(sources.map((s) => Promise.resolve(s(query)).catch(() => [])));
    if (mine !== seq) return null;
    const out = [];
    for (const r of batches.flat()) {
      if (!query) { out.push({ ...r, score: r.kind === "tab" ? 1 : 0 }); continue; }  // open tabs first, then each source's own order
      const m = fuzzy(query, r.label) || (r.detail && fuzzy(query, r.detail) && { score: -1, hits: [] }) || (r.matched && { score: -2, hits: [] });
      if (m) out.push({ ...r, hits: m.hits, score: m.score + (r.boost || 0) });
    }
    // One row per thing: an open tab and the same paper from the library list are the same place.
    const seen = new Set();
    return out.sort((a, b) => b.score - a.score)
      .filter((r) => !r.key || (!seen.has(r.key) && seen.add(r.key)))
      .slice(0, 60);
  }

  function refresh() {
    clearTimeout(timer);
    const mine = ++seq;
    const raw = input.value;
    const isCommand = raw.startsWith(">");
    input.setAttribute("aria-label", isCommand ? "命令" : "快速打开");
    input.placeholder = isCommand ? "输入命令名称" : "按名称查找文献、笔记、论文章节、节点…  输入 > 运行命令";
    foot.replaceChildren(...(isCommand
      ? [el("span", {}, el("kbd", {}, "↑↓"), " 选择"), el("span", {}, el("kbd", {}, "↵"), " 运行"), el("span", {}, "删掉 > 回到快速打开")]
      : [el("span", {}, el("kbd", {}, "↑↓"), " 选择"), el("span", {}, el("kbd", {}, "↵"), " 打开"), el("span", {}, el("kbd", {}, ">"), " 命令")]));
    if (isCommand) { draw(commandRows(raw.slice(1).trim())); return; }
    const query = raw.trim();
    timer = setTimeout(async () => {
      const out = await fileRows(query, mine);
      if (out) draw(out, query ? "没有找到。试试别的词，或输入 > 运行命令" : "这个项目里还没有文献、笔记或论文");
    }, query ? 120 : 0);
  }

  const KIND = { item: "文献", note: "笔记", manuscript: "论文", section: "章节", node: "节点", command: "命令", tab: "已打开" };
  function draw(next, empty = "没有匹配的命令") {
    rows = next;
    active = 0;
    list.replaceChildren(...(rows.length ? rows.map((r, i) => el("li", {
      role: "option", id: `pal-${i}`, class: "pal-row", "aria-selected": String(i === 0),
      onpointermove: () => setActiveRow(i), onclick: () => choose(i),
    }, el("span", { class: `pal-kind k-${r.kind}` }, KIND[r.kind] || r.kind),
      el("span", { class: "pal-label" }, ...highlight(r.label, r.hits)),
      r.detail ? el("span", { class: "pal-detail" }, r.detail) : null,
      r.keys ? el("kbd", { class: "pal-keys" }, keyLabel(r.keys)) : null))
      : [el("li", { class: "pal-empty" }, empty)]));
    input.setAttribute("aria-activedescendant", rows.length ? "pal-0" : "");
  }
  function setActiveRow(i) {
    if (!rows.length) return;
    active = (i + rows.length) % rows.length;
    for (const [j, li] of [...list.children].entries()) li.setAttribute("aria-selected", String(j === active));
    list.children[active]?.scrollIntoView({ block: "nearest" });
    input.setAttribute("aria-activedescendant", `pal-${active}`);
  }
  function choose(i = active) {
    const r = rows[i];
    if (!r) return;
    closePalette();
    Promise.resolve().then(r.run).catch((e) => toast(e.message || String(e), true));
  }
  input.addEventListener("input", refresh);
  input.addEventListener("keydown", (e) => {
    if (e.isComposing) return;
    if (e.key === "ArrowDown") { e.preventDefault(); setActiveRow(active + 1); }
    else if (e.key === "ArrowUp") { e.preventDefault(); setActiveRow(active - 1); }
    else if (e.key === "Enter") { e.preventDefault(); choose(); }
    else if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); closePalette(); }
    else if (e.key === "Tab") e.preventDefault();
  });

  // ---------------------------------------------------------------- open-editor tabs

  const strip = el("div", { id: "tabs", role: "tablist", "aria-label": "已打开" });
  let tabs = store.get("tabs", []);       // [{key, kind, id, title}]
  let current = null;
  const openers = {};                      // kind → (tab) => open it

  function drawTabs() {
    strip.replaceChildren(...tabs.map((t) => {
      const on = t.key === current;
      const tab = el("div", { class: `tab k-${t.kind}${on ? " on" : ""}`, role: "tab", "aria-selected": String(on), tabindex: on ? "0" : "-1",
        title: `${KIND[t.kind] || ""} · ${t.title}`,
        onclick: (e) => { if (!e.target.closest(".tab-x")) activate(t.key); },
        onauxclick: (e) => { if (e.button === 1) close(t.key); },
        onkeydown: (e) => {
          if (e.key === "Enter" || e.key === " ") { e.preventDefault(); activate(t.key); }
          else if (e.key === "Delete" || e.key === "Backspace") { e.preventDefault(); close(t.key); }
          else if (e.key === "ArrowRight" || e.key === "ArrowLeft") {
            e.preventDefault();
            const next = strip.children[(tabs.indexOf(t) + (e.key === "ArrowRight" ? 1 : tabs.length - 1)) % tabs.length];
            next?.focus();
          }
        } },
      el("span", { class: "tab-mark", "aria-hidden": "true" }),
      el("span", { class: "tab-title" }, t.title),
      el("button", { type: "button", class: "tab-x", "aria-label": `关闭 ${t.title}`, tabindex: "-1", onclick: () => close(t.key) }, "×"));
      return tab;
    }));
    strip.hidden = !tabs.length;
    strip.querySelector(".tab.on")?.scrollIntoView({ block: "nearest", inline: "nearest" });
  }
  /** A pane opened something: give it a tab (or update its title) and, when it has focus, mark it current. */
  function opened(kind, id, title, focus = true) {
    const key = `${kind}:${id}`;
    const t = tabs.find((x) => x.key === key);
    if (!focus) {  // a pane in the background redrew: keep its tab's title fresh, nothing else
      if (t && title && t.title !== title) { t.title = title; store.set("tabs", tabs); drawTabs(); }
      return;
    }
    if (t) t.title = title || t.title;
    else {
      const at = tabs.findIndex((x) => x.key === current);
      tabs.splice(at < 0 ? tabs.length : at + 1, 0, { key, kind, id, title: title || id });
      if (tabs.length > 12) tabs = tabs.filter((x, i) => x.key === key || i >= tabs.length - 12);
    }
    current = key;
    store.set("tabs", tabs);
    drawTabs();
  }
  function activate(key) {
    const t = tabs.find((x) => x.key === key);
    if (!t) return;
    current = key;
    drawTabs();
    openers[t.kind]?.(t);
  }
  function close(key) {
    const i = tabs.findIndex((x) => x.key === key);
    if (i < 0) return;
    tabs.splice(i, 1);
    store.set("tabs", tabs);
    if (current === key) current = null;
    drawTabs();
  }
  /** The thing a tab points at is gone (deleted, archived): drop it quietly. */
  function forget(kind, id) { close(`${kind}:${id}`); }
  function cycle(step) {
    if (!tabs.length) return;
    const i = tabs.findIndex((x) => x.key === current);
    activate(tabs[(i + step + tabs.length) % tabs.length].key);
  }
  function deselect() { current = null; drawTabs(); }
  sources.push(() => tabs.map((t) => ({ kind: "tab", key: t.key, label: t.title, detail: KIND[t.kind], boost: 1.5, run: () => activate(t.key) })));

  // ---------------------------------------------------------------- status bar

  const left = el("div", { class: "sb-side" }), right = el("div", { class: "sb-side right" });
  const bar = el("footer", { id: "statusbar", "aria-label": "状态栏" }, left, right);
  const items = new Map();
  /** One status entry; `text` null hides it. */
  function status(id, text, { side = "left", tip, onclick, cls } = {}) {
    let node = items.get(id);
    if (!node) {
      node = onclick ? el("button", { type: "button", class: "sb-item" }) : el("span", { class: "sb-item" });
      if (onclick) node.addEventListener("click", onclick);
      items.set(id, node);
      (side === "right" ? right : left).append(node);
    }
    node.hidden = text == null || text === "";
    node.className = `sb-item${cls ? ` ${cls}` : ""}`;
    if (tip) node.dataset.tip = tip; else delete node.dataset.tip;
    if (typeof text === "string") node.textContent = text; else if (text) node.replaceChildren(text);
  }

  document.body.append(rail, bar, scrim, palette);
  strip.hidden = !tabs.length;

  return {
    command, run, source: (fn) => sources.push(fn), railItem, railGap, setActive,
    openPalette, closePalette, isPaletteOpen: () => !palette.hidden,
    tabs: { strip, opened, activate, close, forget, cycle, deselect, opener: (kind, fn) => { openers[kind] = fn; }, list: () => tabs, current: () => current },
    status,
  };
}
