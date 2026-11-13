/* The workspace's writing mode: a manuscript as a column of sections, each edited in Pandoc Markdown with a live
   preview, saved automatically against the revision it started from. The agent's suggestions show as a
   difference under the section they change and are accepted or rejected one by one. The right column lists the
   sources and claims of the section being edited and the citation check.

   Everything is put into the page as text nodes or elements built here (no innerHTML): section text, sources and
   agent output cannot inject markup. Every request goes through net.js, so the page works in the desktop app. */

import { STANCES, typeOf } from "/static/vocab.js";
import { confirmAction, download, request } from "/static/net.js";
import { renderMarkdown } from "/static/markdown.js";

const el = (tag, attrs = {}, ...children) => {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null || v === false) continue;
    if (k === "class") node.className = v;
    else if (k === "style") node.style.cssText = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else if (k === "value") node.value = v;
    else node.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children) if (c != null && c !== false) node.append(c);
  return node;
};
const KIND_LABEL = { article: "期刊论文", thesis: "学位论文", proposal: "开题报告", review: "综述", report: "研究报告", other: "其他" };
const KEY = "[\\w\\u4e00-\\u9fff][\\w\\u4e00-\\u9fff:.#$%&+?<>~/-]*";
const CITE_AT = new RegExp(`(?<![\\w.@])-?@(${KEY})`, "g");
const SAVE_AFTER = 1500;
const who = (a) => (a === "human" ? "你" : a === "workbench-agent" ? "Agent" : a === "report" ? "研报" : a === "import" ? "导入" : a);
const timeOf = (iso) => new Date(iso).toLocaleString("zh-CN", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit", hour12: false });

// ------------------------------------------------------------------ text difference (Myers, on words and CJK characters)

const tokens = (s) => s.match(/[㐀-鿿]|[A-Za-z0-9_]+|\s+|[^\sA-Za-z0-9_㐀-鿿]/g) || [];

export function diff(a, b) {
  let x = tokens(a), y = tokens(b);
  if (x.length + y.length > 30000) { x = a.split(/(?<=\n)/); y = b.split(/(?<=\n)/); }
  const n = x.length, m = y.length, max = n + m, off = max + 1;
  let v = new Int32Array(2 * max + 3);
  const trace = [];
  let found = false;
  for (let d = 0; d <= max && !found; d++) {
    if (d > 2000) return [{ op: "del", text: a }, { op: "ins", text: b }];
    trace.push({ lo: off - d - 1, v: v.slice(off - d - 1, off + d + 2) });   // only the diagonals step d reads
    for (let k = -d; k <= d; k += 2) {
      let i = k === -d || (k !== d && v[off + k - 1] < v[off + k + 1]) ? v[off + k + 1] : v[off + k - 1] + 1;
      let j = i - k;
      while (i < n && j < m && x[i] === y[j]) { i++; j++; }
      v[off + k] = i;
      if (i >= n && j >= m) { found = true; break; }
    }
  }
  const out = [];
  let i = n, j = m;
  for (let d = trace.length - 1; d >= 0 && (i > 0 || j > 0); d--) {
    const { lo, v: w } = trace[d], at = (kk) => w[off + kk - lo], k = i - j;
    const prevK = k === -d || (k !== d && at(k - 1) < at(k + 1)) ? k + 1 : k - 1;
    const pi = at(prevK), pj = pi - prevK;
    while (i > pi && j > pj) { out.push({ op: "eq", text: x[--i] }); j--; }
    if (d > 0) { if (i === pi) out.push({ op: "ins", text: y[--j] }); else out.push({ op: "del", text: x[--i] }); }
  }
  out.reverse();
  const merged = [];
  for (const p of out) {
    const last = merged[merged.length - 1];
    if (last && last.op === p.op) last.text += p.text; else merged.push({ ...p });
  }
  return merged;
}

function diffView(a, b) {
  const box = el("div", { class: "ms-diff" });
  for (const p of diff(a, b)) box.append(p.op === "eq" ? p.text : el(p.op === "ins" ? "ins" : "del", {}, p.text));
  return box;
}

// ------------------------------------------------------------------ the mode

export function initWriting({ root, q, toast, nodes, openNode, openItem, setContext, askAgent }) {
  let visible = false;
  let list = [], styles = [], pandoc = true;
  let ms = null;               // the open manuscript
  let state = new Map();       // section id -> {rev, heading, body, level, dirty, saving, timer, conflict, card…}
  let refs = { entries: {}, order: [], items: {}, missing: [], numeric: true };
  let check = null;
  let current = null;          // section id being edited
  let pane = "section";        // right column: "section" or "check"
  let preview = localStorage.getItem("onto.writing.preview") !== "0";
  let pendingRender = false;

  async function api(path, opts = {}) {
    const init = { ...opts };
    if (opts.json !== undefined) {
      init.body = JSON.stringify(opts.json);
      init.headers = { "content-type": "application/json" };
      delete init.json;
    }
    const res = await request(q(path), init);
    const body = await res.json().catch(() => ({}));
    if (!res.ok) {
      const err = new Error(typeof body.detail === "string" ? body.detail : `请求失败（${res.status}）`);
      err.status = res.status; err.body = body;
      throw err;
    }
    return body;
  }
  const fail = (e) => toast(e.message || String(e), true);

  // ---------------------------------------------------------------- skeleton

  const picker = el("select", { "aria-label": "论文" });
  const newBtn = el("button", { type: "button", class: "btn sm primary", onclick: () => newDialog() }, "新建");
  const styleSel = el("select", { "aria-label": "引用样式" });
  const exportBtn = el("button", { type: "button", class: "btn sm", "aria-haspopup": "menu" }, "导出");
  const exportMenu = el("div", { class: "lib-menu", role: "menu", hidden: true });
  const outline = el("ol", { class: "ms-outline", "aria-label": "大纲" });
  const issuesBtn = el("button", { type: "button", class: "ms-issues", onclick: () => { pane = "check"; renderAside(); } });
  const side = el("aside", { class: "lib-side ms-side" },
    el("div", { class: "lib-tools" }, picker, newBtn),
    el("div", { class: "lib-tools" }, styleSel, el("div", { class: "lib-add" }, exportBtn, exportMenu)),
    el("div", { class: "ms-outline-head" }, el("span", {}, "大纲"),
      el("button", { type: "button", class: "btn sm", onclick: () => addSection() }, "＋ 一节")),
    outline, issuesBtn);
  const main = el("div", { class: "lib-main ms-main" });
  const aside = el("aside", { class: "ms-aside" });
  root.replaceChildren(side, main, aside);

  exportBtn.addEventListener("click", (e) => { e.stopPropagation(); exportMenu.hidden = !exportMenu.hidden; });
  document.addEventListener("click", (e) => { if (!exportMenu.contains(e.target)) exportMenu.hidden = true; });
  picker.addEventListener("change", () => open(picker.value));
  styleSel.addEventListener("change", async () => {
    try { await api(`/api/manuscripts/${ms.id}`, { method: "PATCH", json: { csl_style: styleSel.value } }); ms.csl_style = styleSel.value; await loadRefs(); renderAllPreviews(); }
    catch (e) { fail(e); }
  });

  function renderExportMenu() {
    const item = (label, fmt, opts = {}) => el("button", { type: "button", role: "menuitem", disabled: opts.disabled, title: opts.title,
      onclick: () => { exportMenu.hidden = true; doExport(fmt, opts.formatted); } }, label);
    const noPandoc = pandoc ? {} : { disabled: true, title: "服务器未安装 pandoc" };
    exportMenu.replaceChildren(
      item("Word（DOCX）", "docx", noPandoc), item("LaTeX 工程（zip）", "tex", noPandoc), el("hr"),
      item("Markdown + BibTeX（zip）", "md"), item("Markdown（引用已排版）", "md", { formatted: true }),
      item("本文引用的 BibTeX", "bib"));
  }

  async function doExport(fmt, formatted) {
    if (!ms) return;
    await flushAll();
    const issues = check?.counts?.error || 0;
    if (issues && !await confirmAction(`引用检查还有 ${issues} 个错误（例如文献库里找不到的引用键），仍然导出？`)) return;
    const ext = { docx: ".docx", tex: "-latex.zip", bib: ".bib", md: formatted ? ".md" : "-markdown.zip" }[fmt];
    const p = new URLSearchParams({ format: fmt });
    if (formatted) p.set("formatted", "1");
    try { await download(q(`/api/manuscripts/${ms.id}/export?${p}`), `${ms.title}${ext}`); }
    catch (e) { fail(e); }
  }

  // ---------------------------------------------------------------- loading

  async function loadList() {
    const out = await api("/api/manuscripts");
    list = out.manuscripts; styles = out.styles; pandoc = out.pandoc;
    picker.replaceChildren(...(list.length ? list.map((m) => el("option", { value: m.id, selected: m.id === ms?.id }, m.title))
      : [el("option", { value: "" }, "还没有论文")]));
    styleSel.replaceChildren(...styles.map((s) => el("option", { value: s.id }, s.label)));
    renderExportMenu();
  }

  async function open(id) {
    if (!id) { ms = null; render(); return; }
    await flushAll();
    try {
      ms = await api(`/api/manuscripts/${encodeURIComponent(id)}`);
      state = new Map();
      current = ms.sections[0]?.id || null;
      localStorage.setItem(`onto.writing.open.${q("")}`, id);
      await Promise.all([loadRefs(), loadCheck()]);
      render();
      setContext({ manuscript_id: ms.id, section_id: current });
    } catch (e) { fail(e); }
  }

  async function loadRefs() {
    if (!ms) return;
    refs = await api(`/api/manuscripts/${ms.id}/references`).catch(() => refs);
  }
  async function loadCheck() {
    if (!ms) return;
    check = await api(`/api/manuscripts/${ms.id}/check`).catch(() => check);
    renderIssuesBtn();
  }
  let checkTimer = null;
  function refreshSoon() {
    clearTimeout(checkTimer);
    checkTimer = setTimeout(async () => { await Promise.all([loadRefs(), loadCheck()]); renderAllPreviews(); renderAside(); renderOutline(); }, 1800);
  }

  // ---------------------------------------------------------------- rendering

  function render() {
    pendingRender = false;
    if (!ms) {
      outline.replaceChildren();
      issuesBtn.hidden = true;
      aside.replaceChildren();
      main.replaceChildren(el("div", { class: "lib-empty" },
        el("p", { class: "eyebrow" }, "WRITING"),
        el("h2", {}, "在项目里写论文"),
        el("p", { class: "lede" }, "按节写 Markdown，用 [@引用键] 引用文献库，用 [[ 把句子锚定到图中的论断。Agent 只提建议，由你逐条接受。"),
        el("div", { class: "lib-empty-acts" }, el("button", { type: "button", class: "btn primary", onclick: () => newDialog() }, "新建论文"))));
      return;
    }
    styleSel.value = ms.csl_style;
    const head = el("div", { class: "ms-head" },
      el("input", { type: "text", class: "ms-title", value: ms.title, "aria-label": "论文标题", onchange: (e) => patchMs({ title: e.target.value }) }),
      el("div", { class: "ms-meta-row" },
        el("span", { class: "dim" }, `${KIND_LABEL[ms.kind] || ms.kind} · ${ms.words} 字 · ${ms.sections.length} 节`),
        el("button", { type: "button", class: "btn sm", onclick: () => metaDialog() }, "论文信息"),
        el("button", { type: "button", class: "btn sm", "aria-pressed": String(preview), onclick: () => {
          preview = !preview; localStorage.setItem("onto.writing.preview", preview ? "1" : "0"); render();
        } }, preview ? "隐藏预览" : "显示预览"),
        el("button", { type: "button", class: "btn sm", onclick: () => askAgent(`请读一下论文《${ms.title}》（${ms.id}），先运行 check_citations，再针对最薄弱的一节提出修改建议。`) }, "请 Agent 审读"),
        el("button", { type: "button", class: "btn sm", onclick: archiveMs }, "归档")));
    const parts = [head];
    for (const g of ms.suggestions.filter((g) => g.kind === "outline")) parts.push(suggestionCard(g));
    for (const g of ms.suggestions.filter((g) => g.kind === "insert_after" && !g.section_id)) parts.push(suggestionCard(g));
    for (const s of ms.sections) {
      parts.push(sectionCard(s));
      for (const g of ms.suggestions.filter((g) => g.kind === "insert_after" && g.section_id === s.id)) parts.push(suggestionCard(g));
    }
    if (!ms.sections.length) parts.push(el("p", { class: "dim" }, "这篇论文还没有章节。"),
      el("button", { type: "button", class: "btn sm primary", onclick: () => addSection() }, "添加第一节"));
    parts.push(bibliographyBox());
    main.replaceChildren(...parts);
    renderOutline();
    renderAside();
    renderIssuesBtn();
  }

  function renderIssuesBtn() {
    issuesBtn.hidden = !ms;
    const c = check?.counts || { error: 0, warning: 0, info: 0 };
    issuesBtn.replaceChildren(...[el("span", {}, "引用检查"),
      c.error ? el("b", { class: "bad" }, `${c.error} 错误`) : null,
      c.warning ? el("b", { class: "warn" }, `${c.warning} 提醒`) : null,
      !c.error && !c.warning ? el("b", { class: "ok" }, "没有问题") : null].filter(Boolean));
  }

  function renderOutline() {
    if (!ms) return;
    let dragging = null;
    outline.replaceChildren(...ms.sections.map((s, i) => {
      const st = state.get(s.id);
      const words = st ? countWords(st.body) : s.words;
      const pending = ms.suggestions.filter((g) => g.section_id === s.id && g.kind === "replace").length;
      const li = el("li", { draggable: "true", "data-id": s.id, class: `lv${st?.level || s.level}${current === s.id ? " on" : ""}`,
        onclick: () => focusSection(s.id, true),
        ondragstart: (e) => { dragging = s.id; e.dataTransfer.effectAllowed = "move"; li.classList.add("dragging"); },
        ondragend: () => li.classList.remove("dragging"),
        ondragover: (e) => { e.preventDefault(); li.classList.add("over"); },
        ondragleave: () => li.classList.remove("over"),
        ondrop: (e) => { e.preventDefault(); li.classList.remove("over"); if (dragging && dragging !== s.id) moveSection(dragging, i); },
      }, el("span", { class: "no" }, numbering()[i]), el("span", { class: "h" }, st?.heading ?? s.heading),
        pending ? el("i", { class: "sg-dot", title: `${pending} 条改稿建议` }) : null,
        el("small", {}, String(words)));
      return li;
    }));
  }

  function numbering() {
    const counters = [0, 0, 0];
    return ms.sections.map((s) => {
      const lv = (state.get(s.id)?.level || s.level) - 1;
      counters[lv]++;
      for (let k = lv + 1; k < 3; k++) counters[k] = 0;
      return counters.slice(0, lv + 1).map((n) => n || 1).join(".");
    });
  }

  const countWords = (body) => {
    const t = (body || "").replace(/<!--[\s\S]*?-->/g, "").replace(/\]\{[^}]*\}/g, "]");
    return (t.match(/[㐀-鿿豈-﫿]/g) || []).length + (t.match(/[A-Za-z0-9]+(?:['’-][A-Za-z0-9]+)*/g) || []).length;
  };

  // ---------------------------------------------------------------- previews: citations and claims

  const nodeById = () => new Map(nodes().map((n) => [n.id, n]));

  /* In a suggestion (draft) a key the paper does not cite yet gets its number only once accepted: it shows as
     [@key], not as an error. In the paper itself an unknown key is not in the library. */
  function citeChip(keys, locs, inText, draft = false) {
    const known = keys.filter((k) => refs.entries[k]);
    const missing = keys.filter((k) => !refs.entries[k]);
    let label;
    if (!known.length) label = `[${keys.map((k) => `@${k}`).join("; ")}]`;
    else if (refs.numeric) {
      const nums = known.map((k) => Number((refs.entries[k].label || "").replace(/\D+/g, "")) || 0).sort((a, b) => a - b);
      const ranges = [];
      for (const n of nums) {
        const last = ranges[ranges.length - 1];
        if (last && n === last[1] + 1) last[1] = n; else ranges.push([n, n]);
      }
      label = `[${ranges.map(([a, b]) => (b - a >= 2 ? `${a}–${b}` : a === b ? a : `${a}, ${b}`)).join(", ")}]`;
    } else {
      const inner = known.map((k) => refs.entries[k].label.replace(/^[(（[]|[)）\]]$/g, "") + (locs[k] ? `, ${locs[k]}` : ""));
      label = inText ? inner.join("; ") : `(${inner.join("; ")})`;
    }
    const title = keys.map((k) => refs.entries[k] ? `${refs.entries[k].text}${locs[k] ? `（${locs[k]}）` : ""}`
      : draft ? `@${k}：接受后编号` : `@${k}：文献库里没有这个引用键`).join("\n");
    return el("button", { type: "button", class: `ms-cite${missing.length ? (draft ? " draft" : " bad") : ""}`, title,
      onclick: () => { const id = refs.items[known[0]]; if (id) openItem(id); } }, label + (missing.length && known.length ? " ?" : ""));
  }

  function claimSpan(nodeId, children) {
    const n = nodeById().get(nodeId);
    return el("span", { class: `ms-claim${n ? "" : " bad"}`, style: n ? `--c:${typeOf(n.type).color}` : "", tabindex: "0", role: "link",
      title: n ? `论断：${n.name}（点击在图谱中查看）` : "锚定的节点不在当前的图里",
      onclick: () => openNode(nodeId), onkeydown: (e) => { if (e.key === "Enter") openNode(nodeId); } }, ...children);
  }

  const ctx = { cite: citeChip, claim: claimSpan, intent: true };
  const draftCtx = { cite: (keys, locs, inText) => citeChip(keys, locs, inText, true), claim: claimSpan, intent: true };

  function renderAllPreviews() {
    for (const st of state.values()) if (st.preview) st.preview.replaceChildren(...renderMarkdown(st.body, ctx));
    const old = main.querySelector(".ms-bib");
    if (old) old.replaceWith(bibliographyBox());
  }

  function bibliographyBox() {
    const box = el("section", { class: "ms-bib" }, el("h3", {}, ms?.lang === "en-US" ? "References" : "参考文献"));
    if (!refs.order.length && !refs.missing?.length) box.append(el("p", { class: "dim" }, "还没有引用。输入 [@ 从文献库中选择。"));
    const ol = el(refs.numeric ? "ol" : "ul", { class: "ms-bib-list" });
    for (const k of refs.order) {
      const e = refs.entries[k];
      ol.append(el("li", {}, el("button", { type: "button", class: "ms-bib-entry", title: `@${k}`, onclick: () => refs.items[k] && openItem(refs.items[k]) },
        refs.numeric ? el("span", { class: "n" }, e.label) : null, e.text)));
    }
    box.append(ol);
    for (const k of refs.missing || []) box.append(el("p", { class: "bad" }, `@${k}：文献库里没有这个引用键，导出时会原样留下。`));
    if (refs.engine === "plain") box.append(el("p", { class: "dim" }, "预览用的是简化格式；服务器装有 pandoc 时按 CSL 样式排版。"));
    return box;
  }

  // ---------------------------------------------------------------- a section

  function sectionCard(s) {
    let st = state.get(s.id);
    if (!st) { st = { id: s.id, rev: s.rev, heading: s.heading, body: s.body, level: s.level, dirty: false, saving: null, timer: null, conflict: null }; state.set(s.id, st); }
    const heading = el("input", { type: "text", class: "ms-h", value: st.heading, "aria-label": "节标题" });
    const level = el("select", { class: "ms-level", "aria-label": "层级" }, ...[1, 2, 3].map((n) => el("option", { value: n, selected: n === st.level }, ["一级", "二级", "三级"][n - 1])));
    const status = el("span", { class: "ms-status dim" }, st.dirty ? "未保存" : "");
    const body = el("textarea", { class: "ms-body", value: st.body, spellcheck: "false", "aria-label": `${st.heading} 正文`,
      placeholder: "用 Markdown 写这一节。输入 [@ 插入引用，[[ 锚定论断。" });
    const pv = el("div", { class: "ms-preview", "aria-label": "预览" });
    const sug = el("div", { class: "ms-sugs" });
    const banner = el("div", { class: "ms-conflict", hidden: true });
    st.status = status; st.preview = preview ? pv : null; st.banner = banner; st.textarea = body; st.headingInput = heading;
    const grow = () => { body.style.height = "auto"; body.style.height = `${Math.max(140, body.scrollHeight + 2)}px`; };
    const changed = () => {
      st.body = body.value; st.heading = heading.value.trim() || st.heading; st.level = Number(level.value);
      st.dirty = true; status.textContent = "未保存";
      clearTimeout(st.timer); st.timer = setTimeout(() => save(st), SAVE_AFTER);
      if (st.preview) st.preview.replaceChildren(...renderMarkdown(st.body, ctx));
      grow();
    };
    body.addEventListener("input", () => { changed(); complete(st); });
    body.addEventListener("keydown", (e) => {
      if (completion && completion.st === st && completeKey(e)) return;
      if ((e.metaKey || e.ctrlKey) && e.key === "s") { e.preventDefault(); save(st); }
    });
    body.addEventListener("focus", () => focusSection(s.id));
    body.addEventListener("select", () => setContext({ manuscript_id: ms.id, section_id: s.id, selection: body.value.slice(body.selectionStart, body.selectionEnd) }));
    body.addEventListener("blur", () => { setTimeout(() => { if (completion?.st === st && !completion.box.contains(document.activeElement)) closeCompletion(); }, 150); if (pendingRender) setTimeout(maybeRender, 200); });
    heading.addEventListener("input", () => { changed(); renderOutline(); });
    heading.addEventListener("focus", () => focusSection(s.id));
    level.addEventListener("change", () => { changed(); renderOutline(); });
    const tools = el("div", { class: "ms-tools" },
      el("button", { type: "button", class: "btn sm", title: "插入引用", onclick: () => { body.focus(); openCompletion(st, "cite", body.selectionStart, body.selectionStart, ""); } }, "引用"),
      el("button", { type: "button", class: "btn sm", title: "把选中的句子锚定到图中的论断", onclick: () => anchorSelection(st) }, "锚定论断"),
      el("button", { type: "button", class: "btn sm", onclick: () => revisionsDialog(st) }, "历史"),
      el("button", { type: "button", class: "btn sm", onclick: () => askAgent(`请读论文《${ms.title}》的「${st.heading}」（${s.id}），提出修改建议：论证更清楚，每个论断都有文献依据。`) }, "请 Agent 改"),
      el("button", { type: "button", class: "btn sm", onclick: () => deleteSection(st) }, "删除"));
    const card = el("section", { class: `ms-sec lv${st.level}${current === s.id ? " on" : ""}`, "data-id": s.id },
      el("div", { class: "ms-sec-head" }, level, heading, status, tools),
      banner,
      el("div", { class: preview ? "ms-split" : "ms-split solo" }, body, preview ? pv : null),
      sug);
    st.card = card;
    for (const g of ms.suggestions.filter((g) => g.kind === "replace" && g.section_id === s.id)) sug.append(suggestionCard(g, st));
    if (st.preview) st.preview.replaceChildren(...renderMarkdown(st.body, ctx));
    if (st.conflict) showConflict(st);
    requestAnimationFrame(grow);
    return card;
  }

  function focusSection(id, scroll) {
    if (current !== id) {
      current = id;
      for (const c of main.querySelectorAll(".ms-sec")) c.classList.toggle("on", c.dataset.id === id);
      for (const li of outline.children) li.classList.toggle("on", li.dataset.id === id);
      renderAside();
      setContext({ manuscript_id: ms.id, section_id: id });
    }
    if (scroll) {
      const st = state.get(id);
      st?.card?.scrollIntoView({ behavior: "smooth", block: "start" });
      st?.textarea?.focus({ preventScroll: true });
    }
  }

  async function save(st) {
    clearTimeout(st.timer);
    if (st.conflict) return;
    if (st.saving) { st.again = true; return st.saving; }
    if (!st.dirty) return;
    const sent = { body: st.body, heading: st.heading, level: st.level };
    st.status.textContent = "保存中…";
    st.saving = (async () => {
      try {
        const out = await api(`/api/sections/${st.id}`, { method: "PATCH", json: { base_rev: st.rev, ...sent } });
        st.rev = out.rev;
        if (st.body === sent.body && st.heading === sent.heading && st.level === sent.level) { st.dirty = false; st.status.textContent = "已保存"; }
        const s = ms.sections.find((x) => x.id === st.id);
        if (s) Object.assign(s, { rev: out.rev, heading: out.heading, body: out.body, level: out.level, words: out.words });
        refreshSoon();
      } catch (e) {
        if (e.status === 409 && e.body?.conflict) { st.conflict = e.body.current; showConflict(st); st.status.textContent = "有冲突"; }
        else { st.status.textContent = "保存失败"; fail(e); }
      } finally {
        st.saving = null;
        if (st.again && !st.conflict) { st.again = false; save(st); }
      }
    })();
    return st.saving;
  }

  async function flushAll() { await Promise.all([...state.values()].filter((st) => st.dirty || st.saving).map((st) => save(st))); }

  function showConflict(st) {
    const theirs = st.conflict;
    st.banner.hidden = false;
    st.banner.replaceChildren(
      el("div", { class: "ms-conflict-head" }, el("strong", {}, "另一处已修改这一节"),
        el("span", { class: "dim" }, `${who(theirs.updated_by)} · ${timeOf(theirs.updated_at)} · rev ${theirs.rev}`)),
      el("p", { class: "dim" }, "下面是对方的版本（删去的）与你的版本（新增的）的差别。选一个版本继续，或者把需要的部分复制过来再保存。"),
      diffView(theirs.body, st.body),
      el("div", { class: "lib-row end" },
        el("button", { type: "button", class: "btn sm", onclick: () => {
          clearTimeout(st.timer);
          Object.assign(st, { body: theirs.body, heading: theirs.heading, level: theirs.level, rev: theirs.rev, dirty: false, conflict: null });
          replaceCard(st.id);
        } }, "用对方的版本"),
        el("button", { type: "button", class: "btn sm primary", onclick: () => {
          st.rev = theirs.rev; st.conflict = null; st.banner.hidden = true; st.dirty = true; save(st);
        } }, "保留我的版本")));
  }

  function replaceCard(id) {
    const st = state.get(id);
    const s = ms.sections.find((x) => x.id === id);
    if (!st || !s || !st.card) return;
    Object.assign(s, { heading: st.heading, body: st.body, level: st.level, rev: st.rev });
    const old = st.card;
    old.replaceWith(sectionCard(s));   // sectionCard points st.card at the new card
  }

  async function addSection(after = current) {
    if (!ms) return;
    try {
      const out = await api(`/api/manuscripts/${ms.id}/sections`, { method: "POST", json: { heading: "新的一节", level: 1, after_section_id: after || null } });
      await reload();
      focusSection(out.id, true);
      state.get(out.id)?.headingInput?.select();
    } catch (e) { fail(e); }
  }

  async function deleteSection(st) {
    if (!await confirmAction(`删除「${st.heading}」？这一节的正文和历史版本会一起删除。`)) return;
    try {
      clearTimeout(st.timer);
      await api(`/api/sections/${st.id}`, { method: "DELETE" });
      state.delete(st.id);
      await reload();
      toast(`已删除「${st.heading}」`);
    } catch (e) { fail(e); }
  }

  async function moveSection(id, to) {
    const order = ms.sections.map((s) => s.id).filter((x) => x !== id);
    order.splice(to, 0, id);
    try {
      await flushAll();
      await api(`/api/manuscripts/${ms.id}/reorder`, { method: "POST", json: { order: order.map((x) => ({ id: x })) } });
      await reload();
    } catch (e) { fail(e); }
  }

  async function patchMs(changes) {
    try { const out = await api(`/api/manuscripts/${ms.id}`, { method: "PATCH", json: changes }); Object.assign(ms, { title: out.title, meta: out.meta, kind: out.kind, lang: out.lang }); await loadList(); }
    catch (e) { fail(e); }
  }

  async function archiveMs() {
    if (!await confirmAction(`归档论文《${ms.title}》？归档后不再出现在列表里。`)) return;
    await patchMs({ archived: true });
    ms = null;
    await loadList();
    open(list[0]?.id);
  }

  /* Fetch the manuscript again and keep what is being typed: sections with unsaved text or focus stay as they
     are (a save will tell about a conflict); others take the new text. */
  async function reload() {
    if (!ms) return;
    const fresh = await api(`/api/manuscripts/${ms.id}`);
    const structure = (m) => m.sections.map((s) => s.id).join(",");
    const sameStructure = structure(fresh) === structure(ms);
    for (const s of fresh.sections) {
      const st = state.get(s.id);
      if (!st) continue;
      const busy = st.dirty || st.saving || st.card?.contains(document.activeElement);
      if (!busy && (s.rev !== st.rev)) {
        Object.assign(st, { rev: s.rev, heading: s.heading, body: s.body, level: s.level });
        if (sameStructure && st.textarea) {
          st.textarea.value = s.body; st.headingInput.value = s.heading;
          if (st.preview) st.preview.replaceChildren(...renderMarkdown(s.body, ctx));
        }
      }
    }
    const suggestionsChanged = JSON.stringify(fresh.suggestions.map((g) => [g.id, g.status])) !== JSON.stringify(ms.suggestions.map((g) => [g.id, g.status]));
    ms = fresh;
    await Promise.all([loadRefs(), loadCheck()]);
    if (!sameStructure || suggestionsChanged) maybeRender(); else { renderOutline(); renderAllPreviews(); renderAside(); }
  }

  function maybeRender() {
    const typing = main.contains(document.activeElement) && /TEXTAREA|INPUT/.test(document.activeElement.tagName);
    if (typing || main.querySelector(".ms-dlg")) { pendingRender = true; return; }
    const top = main.scrollTop;
    render();
    main.scrollTop = top;
  }

  // ---------------------------------------------------------------- suggestions

  function suggestionCard(g, st) {
    const stale = g.status === "stale";
    const kind = { replace: "改写这一节", insert_after: "新增一节", outline: "提议大纲" }[g.kind];
    const parts = [
      el("div", { class: "sg-head" }, el("span", { class: "sg-who" }, who(g.author)), el("strong", {}, kind),
        g.heading && g.kind !== "replace" ? el("span", {}, `「${g.heading}」`) : null,
        stale ? el("span", { class: "warn", title: "建议写成后正文又改过" }, "正文已改动") : null,
        el("span", { class: "dim" }, timeOf(g.created_at))),
      el("p", { class: "sg-why" }, g.rationale),
    ];
    if (g.kind === "replace") {
      const now = st ? st.body : "";
      if (g.heading && st && g.heading !== st.heading) parts.push(el("p", { class: "dim" }, "标题：", diffView(st.heading, g.heading)));
      parts.push(diffView(now, g.body || ""));
    } else if (g.kind === "insert_after") {
      parts.push(el("div", { class: "ms-preview sg-body" }, ...renderMarkdown(g.body || "", draftCtx)));
    } else {
      parts.push(el("ol", { class: "sg-outline" }, ...(g.outline || []).map((o) => el("li", { class: `lv${o.level}` },
        el("strong", {}, o.heading), o.intent ? el("span", { class: "dim" }, ` — ${o.intent}`) : null))));
    }
    parts.push(el("div", { class: "lib-row end" },
      el("button", { type: "button", class: "btn sm", onclick: () => decide(g, "reject") }, "拒绝"),
      el("button", { type: "button", class: "btn sm primary", onclick: () => decide(g, "accept", stale) }, stale ? "仍然接受" : "接受")));
    return el("div", { class: `sg-card${stale ? " stale" : ""}`, "data-id": g.id }, ...parts);
  }

  async function decide(g, how, stale) {
    if (how === "accept" && g.kind === "replace") {
      const st = state.get(g.section_id);
      if (st?.dirty || st?.saving) { await save(st); }
      if (stale && !await confirmAction("这条建议写成之后正文又改过。上面的差别是相对当前正文的，接受会用建议的文字替换整节。继续？")) return;
    }
    try {
      await api(`/api/suggestions/${g.id}/${how}`, { method: "POST", json: how === "accept" ? { force: !!stale } : undefined });
      if (how === "accept" && g.kind === "replace") state.delete(g.section_id);
      toast(how === "accept" ? "已接受建议" : "已拒绝建议");
      await reload();
      maybeRender();
    } catch (e) { fail(e); }
  }

  // ---------------------------------------------------------------- completion: [@ sources, [[ claims

  let completion = null;

  function complete(st) {
    const ta = st.textarea, at = ta.selectionStart;
    const before = ta.value.slice(Math.max(0, at - 80), at);
    const cite = before.match(/(?:\[|;\s*)@([\w一-鿿:.-]*)$/);
    const claim = before.match(/\[\[([^\]\n]{0,40})$/);
    if (cite) openCompletion(st, "cite", at - cite[1].length - 1, at, cite[1]);
    else if (claim) openCompletion(st, "claim", at - claim[1].length - 2, at, claim[1]);
    else closeCompletion();
  }

  function openCompletion(st, kind, from, to, query) {
    if (!completion || completion.st !== st || completion.kind !== kind) {
      closeCompletion();
      const box = el("div", { class: "ms-complete glass", role: "listbox" });
      st.card.append(box);
      completion = { st, kind, box, items: [], index: 0 };
    }
    Object.assign(completion, { from, to, query });
    const ta = st.textarea;
    completion.box.style.top = `${ta.offsetTop + ta.offsetHeight + 4}px`;
    completion.box.style.width = `${Math.max(280, ta.offsetWidth)}px`;
    fill();
  }

  let fillSeq = 0;
  async function fill() {
    const c = completion, seq = ++fillSeq;
    if (!c) return;
    let items;
    if (c.kind === "cite") {
      const p = new URLSearchParams({ limit: "12" });
      if (c.query) p.set("q", c.query);
      const out = await api(`/api/library?${p}`).catch(() => ({ items: [] }));
      items = out.items.map((it) => ({ value: it.citekey, label: it.title, hint: `@${it.citekey} · ${(it.authors || "").split(";")[0]} ${it.year || ""}` }));
    } else {
      // A typed name narrows the list; a selected sentence ranks nodes by the characters and words they share.
      const needle = c.query.trim().toLowerCase();
      const grams = new Set(needle.match(/[㐀-鿿]{2}|[a-z0-9]{3,}/g) || []);
      for (let i = 0; i + 1 < needle.length; i++) if (/[㐀-鿿]{2}/.test(needle.slice(i, i + 2))) grams.add(needle.slice(i, i + 2));
      const score = (n) => {
        const names = [n.name, ...(n.aliases || [])].map((x) => x.toLowerCase());
        let s = ["Claim", "Hypothesis", "Observation", "Finding"].includes(n.type) ? 1 : 0;
        if (!needle) return s;
        if (names.some((x) => x.includes(needle) || needle.includes(x))) s += 100;
        for (const g of grams) if (names.some((x) => x.includes(g))) s += 10;
        return s;
      };
      const ranked = nodes().map((n) => [score(n), n]).sort((a, b) => b[0] - a[0]);
      const hits = needle ? ranked.filter(([s]) => s >= 10) : ranked;
      items = (hits.length ? hits : ranked).slice(0, 12)
        .map(([, n]) => ({ value: n.id, label: n.name, hint: typeOf(n.type).label, color: typeOf(n.type).color }));
    }
    if (seq !== fillSeq || completion !== c) return;
    c.items = items; c.index = 0;
    drawCompletion();
    c.box.scrollIntoView({ block: "nearest" });
  }

  function drawCompletion() {
    const c = completion;
    c.box.replaceChildren(
      el("div", { class: "ms-complete-head dim" }, c.kind === "cite" ? "插入引用 · ↑↓ 选择，回车确认" : "锚定论断 · ↑↓ 选择，回车确认"),
      ...(c.items.length ? c.items.map((it, i) => el("button", { type: "button", role: "option", class: i === c.index ? "on" : "",
        "aria-selected": String(i === c.index), onmousedown: (e) => { e.preventDefault(); c.index = i; applyCompletion(); } },
        it.color ? el("i", { style: `--c:${it.color}` }) : null, el("span", {}, it.label), el("small", {}, it.hint)))
        : [el("div", { class: "dim ms-complete-none" }, c.kind === "cite" ? "文献库里没有匹配的文献" : "没有匹配的节点")]));
  }

  function completeKey(e) {
    const c = completion;
    if (e.key === "Escape") { closeCompletion(); e.preventDefault(); return true; }
    if (!c.items.length) return false;
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      c.index = (c.index + (e.key === "ArrowDown" ? 1 : -1) + c.items.length) % c.items.length;
      drawCompletion(); e.preventDefault(); return true;
    }
    if ((e.key === "Enter" || e.key === "Tab") && !e.isComposing) { applyCompletion(); e.preventDefault(); return true; }
    return false;
  }

  function applyCompletion() {
    const c = completion, it = c.items[c.index], ta = c.st.textarea;
    if (!it) return;
    let ins, caret;
    if (c.kind === "cite") {
      const opened = ta.value.slice(c.from - 1, c.from) === "[";
      const fromInsertButton = c.from === c.to && ta.value.slice(c.from - 1, c.from) !== "@" && !opened;
      if (fromInsertButton) { ins = `[@${it.value}]`; caret = c.from + ins.length; ta.setRangeText(ins, c.from, c.to, "end"); }
      else {
        const start = c.from;            // the "@"
        const closeAfter = opened && ta.value[c.to] !== "]";
        ins = `@${it.value}${closeAfter ? "]" : ""}`;
        ta.setRangeText(ins, start, c.to, "end");
        caret = start + ins.length;
      }
    } else {
      const text = c.query.trim() || it.label;
      ins = `[${text}]{.claim node=${it.value}}`;
      ta.setRangeText(ins, c.from, c.to, "end");
      caret = c.from + ins.length;
    }
    ta.selectionStart = ta.selectionEnd = caret;
    closeCompletion();
    ta.dispatchEvent(new Event("input"));
    ta.focus();
  }

  function closeCompletion() { completion?.box.remove(); completion = null; }

  function anchorSelection(st) {
    const ta = st.textarea;
    const a = ta.selectionStart, b = ta.selectionEnd;
    if (a === b) { toast("先在正文里选中要锚定的句子", true); return; }
    const chosen = ta.value.slice(a, b).replace(/[[\]]/g, "");
    ta.setRangeText(`[[${chosen}`, a, b, "end");
    ta.focus();
    openCompletion(st, "claim", a, a + chosen.length + 2, chosen);
  }

  // ---------------------------------------------------------------- the right column

  function renderAside() {
    if (!ms) { aside.replaceChildren(); return; }
    const tab = (id, label) => el("button", { type: "button", role: "tab", "aria-selected": String(pane === id), onclick: () => { pane = id; renderAside(); } }, label);
    const body = el("div", { class: "ms-aside-body" });
    aside.replaceChildren(el("div", { class: "lib-tabs", role: "tablist" }, tab("section", "本节引用"), tab("check", "引用检查")), body);
    if (pane === "check") return renderCheck(body);
    const st = state.get(current);
    if (!st) { body.append(el("p", { class: "dim" }, "选一节开始写。")); return; }
    const keys = [...new Set([...(st.body.matchAll(CITE_AT))].map((m) => m[1].replace(/[.,;:]+$/, "")))];
    body.append(el("h4", {}, `文献 · ${keys.length}`));
    if (!keys.length) body.append(el("p", { class: "dim" }, "这一节还没有引用。"));
    for (const k of keys) {
      const e = refs.entries[k];
      body.append(el("button", { type: "button", class: `ms-src${e ? "" : " bad"}`, onclick: () => refs.items[k] && openItem(refs.items[k]) },
        el("code", {}, `@${k}`), el("span", {}, e ? e.text : "文献库里没有这个引用键")));
    }
    const claims = (check?.claims || []).filter((c) => c.section_id === current);
    const byId = nodeById();
    body.append(el("h4", {}, `论断 · ${claims.length}`));
    if (!claims.length) body.append(el("p", { class: "dim" }, "选中一句话，点「锚定论断」，把它和图里的论断连起来。"));
    for (const c of claims) {
      const n = byId.get(c.node_id);
      body.append(el("div", { class: "ms-claim-row" },
        el("button", { type: "button", class: "ref node", style: n ? `--c:${typeOf(n.type).color}` : "", onclick: () => openNode(c.node_id) }, n ? n.name : "不在图里的节点"),
        el("div", { class: "ms-claim-g" }, ...(c.groundings.length ? c.groundings.map((g) => el("span", { class: `stance ${STANCES[g.stance]?.tone || ""}`, title: `@${g.citekey}` },
          `${STANCES[g.stance]?.label || g.stance} @${g.citekey}`)) : [el("span", { class: "warn" }, "没有依据")]))));
    }
  }

  function renderCheck(body) {
    if (!check) { body.append(el("p", { class: "dim" }, "检查中…")); return; }
    const label = { error: "错误", warning: "提醒", info: "提示" };
    if (!check.issues.length) body.append(el("p", { class: "ok" }, "没有发现问题：引用键都在文献库里，锚定的论断都有依据。"));
    for (const i of check.issues) {
      body.append(el("button", { type: "button", class: `ms-issue ${i.level}`, onclick: () => focusSection(i.section_id, true) },
        el("span", { class: "lvl" }, label[i.level]), el("span", {}, i.message), el("small", {}, i.heading)));
    }
    body.append(el("button", { type: "button", class: "btn sm", onclick: async () => { await loadCheck(); renderAside(); } }, "重新检查"));
  }

  // ---------------------------------------------------------------- dialogs

  function dialog(title, ...content) {
    main.querySelector(".ms-dlg")?.remove();
    const box = el("div", { class: "ms-dlg glass", role: "dialog", "aria-label": title }, el("h3", {}, title), ...content);
    main.append(box);
    return box;
  }

  function newDialog() {
    const title = el("input", { type: "text", placeholder: "论文标题", "aria-label": "论文标题" });
    const kind = el("select", { "aria-label": "类型" }, ...Object.entries(KIND_LABEL).map(([k, v]) => el("option", { value: k }, v)));
    const style = el("select", { "aria-label": "引用样式" }, ...styles.map((s) => el("option", { value: s.id }, s.label)));
    const lang = el("select", { "aria-label": "语言" }, el("option", { value: "zh-CN" }, "中文"), el("option", { value: "en-US" }, "English"));
    const outlineBox = el("input", { type: "checkbox", checked: true, id: "ms-outline-default" });
    const box = dialog("新建论文", title, el("div", { class: "lib-row" }, kind, style, lang),
      el("label", { class: "lib-row dim", for: "ms-outline-default" }, outlineBox, "按论文类型生成默认章节"),
      el("div", { class: "lib-row end" }, el("button", { type: "button", class: "btn", onclick: () => box.remove() }, "取消"),
        el("button", { type: "button", class: "btn primary", onclick: async () => {
          if (!title.value.trim()) { title.focus(); return; }
          try {
            const m = await api("/api/manuscripts", { method: "POST", json: { title: title.value.trim(), kind: kind.value, csl_style: style.value, lang: lang.value, outline: outlineBox.checked } });
            box.remove();
            await loadList();
            open(m.id);
          } catch (e) { fail(e); }
        } }, "创建")));
    title.focus();
  }

  function metaDialog() {
    const meta = ms.meta || {};
    const authors = el("textarea", { rows: "3", "aria-label": "作者", value: (meta.authors || []).map((a) => a.affiliation ? `${a.name} | ${a.affiliation}` : a.name).join("\n") });
    const abstract = el("textarea", { rows: "5", "aria-label": "摘要", value: meta.abstract || "" });
    const keywords = el("input", { type: "text", "aria-label": "关键词", value: (meta.keywords || []).join("，") });
    const kind = el("select", { "aria-label": "类型" }, ...Object.entries(KIND_LABEL).map(([k, v]) => el("option", { value: k, selected: k === ms.kind }, v)));
    const lang = el("select", { "aria-label": "语言" }, el("option", { value: "zh-CN", selected: ms.lang === "zh-CN" }, "中文"), el("option", { value: "en-US", selected: ms.lang === "en-US" }, "English"));
    const box = dialog("论文信息",
      el("label", { class: "field" }, el("span", {}, "作者（每行一位：姓名 | 单位）"), authors),
      el("label", { class: "field" }, el("span", {}, "摘要"), abstract),
      el("label", { class: "field" }, el("span", {}, "关键词（逗号分隔）"), keywords),
      el("div", { class: "lib-row" }, kind, lang),
      el("div", { class: "lib-row end" }, el("button", { type: "button", class: "btn", onclick: () => box.remove() }, "取消"),
        el("button", { type: "button", class: "btn primary", onclick: async () => {
          const list = authors.value.split("\n").map((l) => l.trim()).filter(Boolean).map((l) => {
            const [name, affiliation] = l.split("|").map((x) => x.trim());
            return affiliation ? { name, affiliation } : { name };
          });
          await patchMs({ kind: kind.value, lang: lang.value, meta: { authors: list, abstract: abstract.value, keywords: keywords.value.split(/[,，;；]/).map((s) => s.trim()).filter(Boolean) } });
          box.remove();
          render();
        } }, "保存")));
  }

  async function revisionsDialog(st) {
    try {
      await save(st);
      const revs = await api(`/api/sections/${st.id}/revisions`);
      const view = el("div", { class: "ms-rev-view" });
      const listBox = el("ul", { class: "pick" }, ...revs.map((r) => el("li", { onclick: (e) => {
        for (const x of listBox.children) x.classList.remove("on");
        e.currentTarget.classList.add("on");
        view.replaceChildren(el("p", { class: "dim" }, `rev ${r.rev} 与当前正文的差别（删去的是当前没有的，新增的是当前多出的）`), diffView(r.body, st.body),
          r.rev !== st.rev ? el("div", { class: "lib-row end" }, el("button", { type: "button", class: "btn sm primary", onclick: async () => {
            Object.assign(st, { body: r.body, heading: r.heading, dirty: true });
            await save(st);
            box.remove();
            replaceCard(st.id);
            toast(`已恢复到 rev ${r.rev}`);
          } }, "恢复这一版")) : "");
      } }, `rev ${r.rev}`, el("span", {}, who(r.author)), el("small", {}, timeOf(r.updated_at || r.created_at)))));
      const box = dialog(`「${st.heading}」的历史版本`, listBox, view,
        el("div", { class: "lib-row end" }, el("button", { type: "button", class: "btn", onclick: () => box.remove() }, "关闭")));
    } catch (e) { fail(e); }
  }

  /* From the report tab: choose report sections and where they go; they arrive as insert suggestions. */
  async function insertReport(report) {
    await loadList();
    if (!list.length) { toast("先在写作模式里新建一篇论文", true); return false; }
    const target = el("select", { "aria-label": "论文" }, ...list.map((m) => el("option", { value: m.id, selected: m.id === ms?.id }, m.title)));
    const after = el("select", { "aria-label": "插入位置" });
    const fillAfter = async () => {
      const m = await api(`/api/manuscripts/${target.value}`).catch(() => ({ sections: [] }));
      after.replaceChildren(el("option", { value: "" }, "放在最前面"), ...m.sections.map((s, i) => el("option", { value: s.id, selected: i === m.sections.length - 1 }, `放在「${s.heading}」之后`)));
    };
    target.addEventListener("change", fillAfter);
    await fillAfter();
    const secs = report.body?.sections || [];
    const checks = secs.map((s, i) => el("label", { class: "lib-row" }, el("input", { type: "checkbox", value: i, checked: true }), s.heading));
    const overlay = el("div", { class: "ms-overlay" });
    const box = el("div", { class: "ms-dlg glass", role: "dialog", "aria-label": "插入为草稿节" },
      el("h3", {}, "把研报插入论文"),
      el("p", { class: "dim" }, "选中的段落会成为待接受的新章节：涉及论断的句子带上锚点，并引用这些论断已生效的文献依据。"),
      target, after, ...checks,
      el("div", { class: "lib-row end" }, el("button", { type: "button", class: "btn", onclick: () => overlay.remove() }, "取消"),
        el("button", { type: "button", class: "btn primary", onclick: async () => {
          const chosen = checks.map((c) => c.querySelector("input")).filter((x) => x.checked).map((x) => Number(x.value));
          if (!chosen.length) return;
          try {
            const out = await api(`/api/manuscripts/${target.value}/from-report`, { method: "POST", json: { report_id: report.id, sections: chosen, after_section_id: after.value || null } });
            overlay.remove();
            toast(`已生成 ${out.length} 条待接受的章节`);
            showAfterInsert = target.value;
            window.dispatchEvent(new CustomEvent("onto:mode", { detail: "writing" }));
          } catch (e) { fail(e); }
        } }, "生成建议")));
    overlay.append(box);
    document.body.append(overlay);
    return true;
  }
  let showAfterInsert = null;

  // ---------------------------------------------------------------- outside

  let msgTimer = null;
  function onMessage(msg) {
    if (!visible) return;
    if (msg.id && ms && msg.id !== ms.id) { loadList(); return; }
    clearTimeout(msgTimer);
    msgTimer = setTimeout(async () => { try { await loadList(); await reload(); } catch { /* the next message tries again */ } }, 250);
  }

  let loaded = false;
  return {
    async show() {
      visible = true;
      root.hidden = false;
      try {
        if (!loaded || showAfterInsert) {
          loaded = true;
          await loadList();
          const want = showAfterInsert || ms?.id || localStorage.getItem(`onto.writing.open.${q("")}`);
          showAfterInsert = null;
          await open(list.some((m) => m.id === want) ? want : list[0]?.id);
        } else {
          await loadList();
          await reload();
        }
      } catch (e) { fail(e); }
      if (ms) setContext({ manuscript_id: ms.id, section_id: current });
    },
    hide() { visible = false; root.hidden = true; flushAll(); closeCompletion(); setContext(null); },
    onMessage,
    insertReport,
    onGraph() { if (visible && ms) { loadCheck().then(() => { renderAllPreviews(); renderAside(); }); } },
  };
}
