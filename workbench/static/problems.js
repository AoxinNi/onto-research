/* The Problems panel: the citation check of the open manuscript, at the bottom of the work area, like VS Code's.
   Problems are grouped by section in reading order; a click (or F8) goes to the marked text. The panel only shows
   what writing.js hands it; the check itself runs on the server (ontology/manuscripts.py: check). */

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

const LEVELS = [["error", "错误", "⊗"], ["warning", "提醒", "⚠"], ["info", "提示", "ⓘ"]];
const CODE_LABEL = {
  unknown_citekey: "引用键", archived_source: "已归档", unknown_node: "论断", archived_node: "论断", pending_node: "待审核",
  ungrounded_claim: "无依据", only_contradicted: "被反驳", unverified_excerpt: "摘录", cite_not_grounding: "引用",
  grounding_not_cited: "漏引", quote_mismatch: "引文", quote_page: "页码", number_mismatch: "数字", unchecked_source: "未核对",
};

/** opts: storageKey, onReveal(issue, manuscriptId), onRefresh() → problems, onToggle(open) */
export function createProblems({ storageKey, onReveal, onRefresh, onToggle = () => {} }) {
  const load = () => { try { return JSON.parse(localStorage.getItem(storageKey) || "{}"); } catch { return {}; } };
  const saved = load();
  const shown = new Set(saved.levels || ["error", "warning", "info"]);
  let data = { manuscript: null, issues: [], counts: { error: 0, warning: 0, info: 0 } };
  let cursor = -1;   // the problem F8 went to last
  const persist = () => { try { localStorage.setItem(storageKey, JSON.stringify({ open: !panel.hidden, levels: [...shown] })); } catch { /* private mode */ } };

  const title = el("span", { class: "pb-title" }, "问题");
  const filters = el("div", { class: "pb-filters", role: "group", "aria-label": "显示哪些问题" });
  const refresh = el("button", { type: "button", class: "pb-btn", title: "重新检查", "aria-label": "重新检查", onclick: async () => {
    refresh.disabled = true;
    try { await onRefresh(); } finally { refresh.disabled = false; }
  } }, "↻");
  const close = el("button", { type: "button", class: "pb-btn", title: "关闭（⇧⌘M）", "aria-label": "关闭问题面板", onclick: () => toggle(false) }, "×");
  const list = el("div", { class: "pb-list", role: "list" });
  const panel = el("section", { id: "problems", class: "glass", "aria-label": "问题", hidden: true },
    el("header", { class: "pb-head" }, title, filters, el("span", { class: "pb-fill" }), refresh, close), list);
  document.body.append(panel);

  function visible() { return data.issues.filter((i) => shown.has(i.level)); }

  function render() {
    const c = data.counts || {};
    title.replaceChildren("问题", data.manuscript ? el("small", {}, `《${data.manuscript.title}》`) : null);
    filters.replaceChildren(...LEVELS.map(([lvl, label, mark]) => el("button", {
      type: "button", class: `pb-filter ${lvl}`, "aria-pressed": String(shown.has(lvl)), title: `显示${label}`,
      onclick: () => { shown.has(lvl) ? shown.delete(lvl) : shown.add(lvl); persist(); render(); },
    }, `${mark} ${c[lvl] || 0}`)));
    if (!data.manuscript) { list.replaceChildren(el("p", { class: "pb-empty" }, "在写作里打开一篇论文，这里会列出引用检查发现的问题。")); return; }
    const rows = visible();
    if (!data.issues.length) { list.replaceChildren(el("p", { class: "pb-empty ok" }, "没有发现问题：引用键都在文献库里，论断都有依据，引文和数字与原文一致。")); return; }
    if (!rows.length) { list.replaceChildren(el("p", { class: "pb-empty" }, "问题都被筛掉了。")); return; }
    const groups = [];
    for (const i of rows) {
      const g = groups[groups.length - 1];
      if (g?.id === i.section_id) g.items.push(i); else groups.push({ id: i.section_id, heading: i.heading, items: [i] });
    }
    list.replaceChildren(...groups.map((g) => el("div", { class: "pb-group", role: "listitem" },
      el("div", { class: "pb-sec" }, g.heading || "（无标题）", el("span", {}, String(g.items.length))),
      ...g.items.map((i) => {
        const mark = LEVELS.find(([l]) => l === i.level)?.[2] || "·";
        return el("button", { type: "button", class: `pb-row ${i.level}${data.issues.indexOf(i) === cursor ? " on" : ""}`,
          onclick: () => go(i) },
        el("span", { class: "pb-mark", "aria-label": LEVELS.find(([l]) => l === i.level)?.[1] }, mark),
        el("span", { class: "pb-msg" }, i.message),
        el("span", { class: "pb-code" }, CODE_LABEL[i.code] || i.code),
        i.snippet ? el("span", { class: "pb-snip" }, i.snippet.length > 36 ? `${i.snippet.slice(0, 36)}…` : i.snippet) : null);
      }))));
  }

  function go(i) {
    cursor = data.issues.indexOf(i);
    render();
    onReveal(i, data.manuscript?.id);
  }

  function toggle(open = panel.hidden) {
    panel.hidden = !open;
    document.body.classList.toggle("problems-open", open);
    persist();
    onToggle(open);
    if (open) render();
  }

  if (saved.open) toggle(true);
  render();

  return {
    el: panel,
    /** New results from writing.js. */
    set(next) {
      const same = next?.manuscript?.id === data.manuscript?.id;
      data = next || data;
      if (!same) cursor = -1;
      render();
    },
    counts: () => data.counts || { error: 0, warning: 0, info: 0 },
    hasManuscript: () => Boolean(data.manuscript),
    toggle,
    isOpen: () => !panel.hidden,
    /** Go to the next (1) or previous (-1) problem that the filters show; opens the panel. */
    next(dir = 1) {
      const rows = visible();
      if (!rows.length) return false;
      const at = rows.indexOf(data.issues[cursor]);
      const k = at < 0 ? (dir > 0 ? 0 : rows.length - 1) : (at + dir + rows.length) % rows.length;
      if (panel.hidden) toggle(true);
      go(rows[k]);
      return true;
    },
  };
}
