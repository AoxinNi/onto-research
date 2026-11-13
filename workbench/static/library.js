/* The workspace's library mode: sources, a reader that turns selections into excerpts, excerpts tied to graph
   nodes (groundings), and notes. Every request goes through net.js, so the page works in the desktop app too.

   Text from sources and notes is only ever put into the page as text nodes (no innerHTML), so a source cannot
   inject markup. */

import { KINDS, STANCES, typeOf } from "/static/vocab.js";
import { confirmAction, download, onFileDrop, openExternal, pickAndUpload, request } from "/static/net.js";
import { createReading } from "/static/reading.js";

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
const READING = { unread: "未读", reading: "在读", read: "已读" };
const IMPORT_TYPES = [".bib", ".bibtex", ".ris", ".nbib", ".json"];
const FILE_TYPES = [".pdf", ".txt", ".md", ".markdown"];
// Formulas in a note: $$display$$, \[display\], \(inline\), $inline$. An inline $…$ must hug its content and not
// touch a digit or letter outside, so "costs $5 and $6" stays text.
const MATH = /\$\$([\s\S]+?)\$\$|\\\[([\s\S]+?)\\\]|\\\(([\s\S]+?)\\\)|(?<![\\$\w])\$(?=\S)([^$\n]*?\S)\$(?![\w$])/g;
const REF = /\[\[((?:n|ex|it)_[0-9a-f]{12})\]\]|(?<![\w.@])@([\w一-鿿][\w一-鿿:.#$%&+?<>~/-]*)/g;
const shortAuthors = (s) => {
  const people = (s || "").split("; ").filter(Boolean);
  if (!people.length) return "佚名";
  const first = people[0].split(",")[0];
  return people.length > 2 ? `${first} 等` : people.map((p) => p.split(",")[0]).join("、");
};
const ago = (iso) => {
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  if (s < 60) return "刚刚";
  if (s < 3600) return `${Math.floor(s / 60)} 分钟前`;
  if (s < 86400) return `${Math.floor(s / 3600)} 小时前`;
  return new Date(iso).toLocaleDateString("zh-CN");
};

export function initLibrary({ root, q, toast, nodes, openNode, setContext, askAgent }) {
  let side = "items";          // which list: "items" or "notes"
  let filter = { q: "", kind: "", tag: "" };
  let items = [], tags = [], notes = [];
  let notesArchived = false, archivedNotes = 0;  // the notes list can show archived notes, to restore them
  let view = { kind: "empty" }; // what the main pane shows
  let visible = false;
  let refreshTimer = null;

  async function api(path, opts = {}) {
    const init = { ...opts };
    if (opts.json !== undefined) {
      init.body = JSON.stringify(opts.json);
      init.headers = { "content-type": "application/json" };
      delete init.json;
    }
    const res = await request(q(path), init);
    const body = await res.json().catch(() => ({}));
    if (!res.ok) throw Object.assign(new Error(typeof body.detail === "string" ? body.detail : `请求失败（${res.status}）`), { status: res.status });
    return body;
  }
  const fail = (e) => toast(e.message || String(e), true);
  const paperReading = createReading({ api, q, toast, fail });

  // ---------------------------------------------------------------- skeleton

  const listBox = el("ul", { class: "lib-list", role: "listbox", "aria-label": "文献列表" });
  const search = el("input", { type: "search", placeholder: "搜索标题、作者、全文…", "aria-label": "搜索文献", spellcheck: "false" });
  const kindSel = el("select", { "aria-label": "按类型筛选" }, el("option", { value: "" }, "全部类型"),
    ...Object.entries(KINDS).map(([k, v]) => el("option", { value: k }, v)));
  const tagRow = el("div", { class: "lib-tags" });
  const filters = el("div", { class: "lib-filters" }, kindSel, tagRow);
  const tabBtn = (id, label) => el("button", { type: "button", role: "tab", "data-side": id, "aria-selected": String(side === id),
    onclick: () => { side = id; renderSide(); load(); } }, label);
  const addMenu = el("div", { class: "lib-menu", role: "menu", hidden: true },
    menuItem("按 DOI / arXiv / ISBN / 网址添加", () => show({ kind: "add" })),
    menuItem("上传 PDF 或文本", uploadNew),
    menuItem("导入 BibTeX / RIS / CSL-JSON", importFile),
    menuItem("检索学术文献（OpenAlex）", () => show({ kind: "scholarly" })),
    menuItem("手动填写一条", manualItem),
    el("hr"),
    menuItem("导出 BibTeX", () => download(q("/api/library/export.bib"), "library.bib").catch(fail)),
  );
  const addBtn = el("button", { type: "button", class: "btn sm primary", "aria-haspopup": "menu", onclick: (e) => {
    e.stopPropagation();
    if (side === "notes") { newNote(); return; }
    addMenu.hidden = !addMenu.hidden;
  } }, "添加");
  document.addEventListener("click", (e) => { if (!addMenu.contains(e.target)) addMenu.hidden = true; });
  const sideBox = el("aside", { class: "lib-side" },
    el("div", { class: "lib-tabs", role: "tablist" }, tabBtn("items", "文献"), tabBtn("notes", "笔记")),
    el("div", { class: "lib-tools" }, search, el("div", { class: "lib-add" }, addBtn, addMenu)),
    filters, listBox);
  const main = el("div", { class: "lib-main" });
  const selBar = el("div", { class: "lib-selbar glass", hidden: true });
  root.replaceChildren(sideBox, main, selBar);

  function menuItem(label, fn) {
    return el("button", { type: "button", role: "menuitem", onclick: () => { addMenu.hidden = true; fn(); } }, label);
  }

  let searchTimer;
  search.addEventListener("input", () => { clearTimeout(searchTimer); searchTimer = setTimeout(() => { filter.q = search.value.trim(); load(); }, 250); });
  kindSel.addEventListener("change", () => { filter.kind = kindSel.value; load(); });

  onFileDrop(root, async (drops) => {
    for (const d of drops) {
      const lower = d.name.toLowerCase();
      try {
        if (IMPORT_TYPES.some((x) => lower.endsWith(x))) { await importResult(await d.upload(q("/api/library/import"))); continue; }
        const attach = view.kind === "item" && view.data && !view.data.files.length;
        const res = await d.upload(q(attach ? `/api/library/${view.id}/files` : "/api/library/upload"));
        const out = await res.json();
        if (!res.ok) throw new Error(out.detail || `上传失败（${res.status}）`);
        if (attach) await openItem(view.id, "text"); else afterUpload(out);
      } catch (e) { fail(e); }
    }
  });

  // ---------------------------------------------------------------- side list

  function renderSide() {
    for (const b of sideBox.querySelectorAll("[data-side]")) b.setAttribute("aria-selected", String(b.dataset.side === side));
    filters.hidden = side !== "items";
    search.placeholder = side === "items" ? "搜索标题、作者、全文…" : "搜索笔记";
    addBtn.textContent = side === "items" ? "添加" : "新建笔记";
    addBtn.setAttribute("aria-haspopup", side === "items" ? "menu" : "false");
    tagRow.replaceChildren(...tags.slice(0, 16).map((t) => el("button", {
      type: "button", class: `tag-chip${filter.tag === t.tag ? " on" : ""}`,
      onclick: () => { filter.tag = filter.tag === t.tag ? "" : t.tag; load(); },
    }, t.tag, el("small", {}, String(t.n)))));
    if (side === "items") {
      if (!items.length) {
        listBox.replaceChildren(el("li", { class: "lib-none" }, filter.q || filter.kind || filter.tag ? "没有匹配的文献" : "还没有文献。添加 DOI、上传 PDF，或把文件拖进来。"));
        return;
      }
      listBox.replaceChildren(...items.map((it) => el("li", {
        role: "option", "aria-selected": String(view.kind === "item" && view.id === it.id), tabindex: "0",
        onclick: () => openItem(it.id), onkeydown: (e) => { if (e.key === "Enter") openItem(it.id); },
      },
        el("div", { class: "li-title" }, it.title),
        el("div", { class: "li-meta" }, `${shortAuthors(it.authors)} · ${it.year ?? "n.d."}`,
          el("span", { class: "li-kind" }, KINDS[it.kind] || it.kind),
          it.reading !== "unread" && el("span", { class: `li-read ${it.reading}` }, READING[it.reading])),
        el("div", { class: "li-counts" },
          it.files ? el("span", {}, `${it.files} 文件`) : el("span", { class: "dim" }, "无全文"),
          it.excerpts ? el("span", {}, `${it.excerpts} 摘录`) : null,
          it.groundings ? el("span", { class: "acc" }, `${it.groundings} 依据`) : null),
      )));
    } else {
      const toggle = (on, label) => el("li", { class: "lib-archive-toggle" },
        el("button", { type: "button", class: "btn sm ghost", onclick: () => { notesArchived = on; load(); } }, label));
      const head = notesArchived ? toggle(false, "← 返回笔记") : null;
      const foot = !notesArchived && archivedNotes ? toggle(true, `已归档的笔记（${archivedNotes}）`) : null;
      const none = notesArchived ? (filter.q ? "没有匹配的归档笔记" : "没有归档的笔记。")
        : filter.q ? "没有匹配的笔记" : "还没有笔记。";
      listBox.replaceChildren(...[head, ...(notes.length ? notes.map((n) => el("li", {
        role: "option", "aria-selected": String(view.kind === "note" && view.id === n.id), tabindex: "0",
        onclick: () => openNote(n.id), onkeydown: (e) => { if (e.key === "Enter") openNote(n.id); },
      },
        el("div", { class: "li-title" }, n.pinned && !notesArchived ? "📌 " : "", n.title),
        el("div", { class: "li-preview" }, n.preview.replace(/\s+/g, " ").slice(0, 120)),
        el("div", { class: "li-meta" }, `${n.author === "human" ? "你" : "Agent"} · ${notesArchived ? "归档于 " : ""}${ago(n.updated_at)}`)))
        : [el("li", { class: "lib-none" }, none)]), foot].filter(Boolean));
    }
  }

  async function load() {
    try {
      if (side === "items") {
        const p = new URLSearchParams();
        if (filter.kind) p.set("kind", filter.kind);
        if (filter.tag) p.set("tag", filter.tag);
        if (filter.q) {
          // full text too: search returns items whose text matches, with passages
          const hits = await api(`/api/library/search?${new URLSearchParams({ q: filter.q, ...(filter.kind ? { kind: filter.kind } : {}) })}`);
          const listed = await api(`/api/library?${p}`);
          const byId = new Map(listed.items.map((it) => [it.id, it]));
          items = hits.map((h) => byId.get(h.id)).filter(Boolean);
          tags = listed.tags;
        } else {
          const out = await api(`/api/library?${p}`);
          items = out.items; tags = out.tags;
        }
      } else {
        const p = new URLSearchParams();
        if (filter.q) p.set("q", filter.q);
        if (notesArchived) p.set("archived", "true");
        [notes, { count: archivedNotes }] = await Promise.all([api(`/api/notes?${p}`), api("/api/notes/archived/count")]);
        if (notesArchived && !archivedNotes) { notesArchived = false; return load(); }  // the last one was restored
      }
      renderSide();
    } catch (e) { fail(e); }
  }

  // ---------------------------------------------------------------- main pane

  function show(next) {
    view = next;
    selBar.hidden = true;
    renderSide();
    if (next.kind === "add") renderAdd();
    else if (next.kind === "scholarly") renderScholarly();
    else if (next.kind === "empty") renderEmpty();
    if (next.kind !== "item" && next.kind !== "note") setContext(null);
  }

  function renderEmpty() {
    main.replaceChildren(el("div", { class: "lib-empty" },
      el("p", { class: "eyebrow" }, "LIBRARY / 文献"),
      el("h2", {}, "把读过的东西，接到图上。"),
      el("p", { class: "lede" }, "添加文献，在正文里划出摘录，再把摘录关联到图中的论断和假设。Agent 也能检索、阅读和引用这里的文献。"),
      el("div", { class: "lib-empty-acts" },
        el("button", { type: "button", class: "btn primary", onclick: () => show({ kind: "add" }) }, "添加文献"),
        el("button", { type: "button", class: "btn", onclick: uploadNew }, "上传 PDF"),
        el("button", { type: "button", class: "btn", onclick: () => show({ kind: "scholarly" }) }, "检索学术文献")),
      el("p", { class: "hint" }, "也可以把 PDF、BibTeX 或 RIS 文件拖进这里。")));
  }

  function renderAdd() {
    const input = el("input", { type: "text", placeholder: "10.1038/… · arXiv:2401.01234 · 978-… · https://…", "aria-label": "标识符", spellcheck: "false" });
    const go = el("button", { type: "submit", class: "btn primary" }, "添加");
    const form = el("form", { class: "lib-form", onsubmit: async (e) => {
      e.preventDefault();
      if (!input.value.trim()) return;
      go.disabled = true; go.textContent = "查找中…";
      try {
        const out = await api("/api/library", { method: "POST", json: { identifier: input.value.trim() } });
        toast(out.created ? `已添加：${out.item.title}` : "文献库里已有这篇");
        if (out.file_error) toast(`全文没能保存：${out.file_error}`, true);
        await load();
        openItem(out.item.id);
      } catch (err) { fail(err); go.disabled = false; go.textContent = "添加"; }
    } }, el("h2", {}, "添加文献"),
      el("p", { class: "lede" }, "元数据来自 DOI、arXiv、Open Library 或 OpenAlex；arXiv 论文会一并保存 PDF，网页会保存正文。"),
      el("div", { class: "lib-row" }, input, go));
    main.replaceChildren(form);
    input.focus();
  }

  function renderScholarly() {
    const input = el("input", { type: "search", placeholder: "主题、标题或作者", "aria-label": "检索词" });
    const year = el("input", { type: "number", placeholder: "起始年份", min: "1900", max: "2100", "aria-label": "起始年份", class: "year" });
    const out = el("div", { class: "lib-results" });
    const form = el("form", { class: "lib-form", onsubmit: async (e) => {
      e.preventDefault();
      if (!input.value.trim()) return;
      out.replaceChildren(el("p", { class: "dim" }, "检索中…"));
      try {
        const p = new URLSearchParams({ q: input.value.trim() });
        if (year.value) p.set("year_from", year.value);
        const hits = await api(`/api/scholarly?${p}`);
        out.replaceChildren(...(hits.length ? hits.map(resultCard) : [el("p", { class: "dim" }, "没有结果")]));
      } catch (err) { out.replaceChildren(el("p", { class: "bad" }, err.message)); }
    } }, el("h2", {}, "检索学术文献"),
      el("p", { class: "lede" }, "来自 OpenAlex（含 arXiv 与大多数期刊）。只有你点「加入」的才进文献库。"),
      el("div", { class: "lib-row" }, input, year, el("button", { type: "submit", class: "btn primary" }, "检索")));
    main.replaceChildren(form, out);
    input.focus();
  }

  function resultCard(h) {
    const add = el("button", { type: "button", class: "btn sm", onclick: async () => {
      add.disabled = true; add.textContent = "加入中…";
      try {
        const r = await api("/api/library", { method: "POST", json: { identifier: h.doi ? `doi:${h.doi}` : h.id } });
        add.textContent = r.created ? "已加入" : "已在库中";
        load();
      } catch (e) { fail(e); add.disabled = false; add.textContent = "加入"; }
    } }, "加入");
    return el("article", { class: "res" },
      el("div", { class: "res-head" }, el("h3", {}, h.title), add),
      el("div", { class: "li-meta" }, [h.authors.join("、") || "佚名", h.year, h.venue].filter(Boolean).join(" · "),
        h.cited_by != null && el("span", { class: "li-kind" }, `被引 ${h.cited_by}`)),
      h.abstract && el("p", { class: "res-abs" }, h.abstract));
  }

  // ---------------------------------------------------------------- one item

  async function openItem(id, tab) {
    try {
      const data = await api(`/api/library/${encodeURIComponent(id)}`);
      const keepTab = view.kind === "item" && view.id === id ? view.tab : null;
      const pdf = data.files.some((f) => f.media_type === "application/pdf");
      view = { kind: "item", id, data, tab: tab || keepTab || (pdf ? "reading" : data.files.length ? "text" : "info"), text: null };
      setContext({ item_id: id });
      renderSide();
      renderItem();
    } catch (e) { fail(e); }
  }

  function renderItem() {
    const it = view.data;
    const tabs = [["reading", "精读"], ["text", "原文"], ["excerpts", `摘录 ${it.excerpts.length || ""}`], ["groundings", `依据 ${it.groundings.length || ""}`],
                  ["notes", `笔记 ${it.notes.length || ""}`], ["info", "信息"]];
    const reading = el("select", { "aria-label": "阅读状态", onchange: () => patch({ reading: reading.value }) },
      ...Object.entries(READING).map(([k, v]) => el("option", { value: k, selected: it.reading === k }, v)));
    const cite = el("button", { type: "button", class: "citekey", "data-tip": "复制引用", onclick: () => {
      navigator.clipboard?.writeText(`[@${it.citekey}]`).then(() => toast(`已复制 [@${it.citekey}]`), () => {});
    } }, `@${it.citekey}`);
    const link = it.doi ? `https://doi.org/${it.doi}` : it.url;
    const head = el("header", { class: "item-head" },
      el("div", { class: "kind" }, KINDS[it.kind] || it.kind, cite),
      el("h2", {}, it.title),
      el("p", { class: "byline" }, [it.authors || "佚名", it.year, it.csl?.["container-title"]].filter(Boolean).join(" · ")),
      el("div", { class: "item-acts" }, reading,
        link && el("button", { type: "button", class: "btn sm", onclick: () => openExternal(link) }, it.doi ? "DOI" : "原网页"),
        el("button", { type: "button", class: "btn sm", onclick: () => askAgent(`请读一下 [@${it.citekey}]，总结它的主要发现，并看看图里哪些论断可以用它作为依据。`) }, "让 Agent 读"),
        el("button", { type: "button", class: "btn sm", onclick: archiveItem }, "移除")),
      el("div", { class: "item-tabs", role: "tablist" }, ...tabs.map(([k, label]) => el("button", {
        type: "button", role: "tab", "aria-selected": String(view.tab === k), onclick: () => { view.tab = k; renderItem(); },
      }, label.trim()))));
    const body = el("div", { class: "item-body" });
    main.replaceChildren(head, body);
    selBar.hidden = true;
    ({ reading: (b) => paperReading.render(b, it), text: renderText, excerpts: renderExcerpts, groundings: renderGroundings, notes: renderItemNotes, info: renderInfo })[view.tab](body);
  }

  async function patch(changes) {
    try {
      view.data = await api(`/api/library/${view.id}`, { method: "PATCH", json: changes });
      load();
      renderItem();
    } catch (e) { fail(e); }
  }

  async function archiveItem() {
    if (!await confirmAction(`把「${view.data.title}」移出文献库？摘录和笔记会保留，可以在归档中恢复。`)) return;
    try {
      await api(`/api/library/${view.id}`, { method: "DELETE" });
      toast("已移出文献库");
      show({ kind: "empty" });
      load();
    } catch (e) { fail(e); }
  }

  async function renderText(body) {
    const it = view.data;
    const files = el("div", { class: "files" }, ...it.files.map((f) => el("div", { class: "file" },
      el("span", { class: "fname" }, f.filename),
      el("span", { class: "dim" }, [f.pages ? `${f.pages} 页` : null, `${Math.max(1, Math.round(f.size / 1024))} KB`,
        f.extract_status === "failed" ? "无法提取文字" : f.extract_status === "empty" ? "没有文字层（扫描件？）" : null].filter(Boolean).join(" · ")),
      el("button", { type: "button", class: "btn sm", onclick: () => download(q(`/api/library/files/${f.id}`), f.filename).catch(fail) }, "下载"),
      el("button", { type: "button", class: "btn sm", onclick: () => deleteFile(f) }, "删除"))),
      el("button", { type: "button", class: "btn sm", onclick: attachFile }, "添加文件"));
    body.append(files);
    if (!it.files.length) {
      body.append(el("div", { class: "drop-hint" }, "这条文献还没有全文。上传 PDF 或文本，或把文件拖到这里。"));
      return;
    }
    const reader = el("div", { class: "reader" }, el("p", { class: "dim" }, "读取正文…"));
    const modes = el("div", { class: "text-modes", role: "radiogroup", "aria-label": "原文显示方式", hidden: true });
    body.append(modes, reader);
    reader.addEventListener("mouseup", () => setTimeout(() => offerExcerpt(reader), 0));
    reader.addEventListener("scroll", () => { selBar.hidden = true; });
    const hasPdf = it.files.some((f) => f.media_type === "application/pdf" || /\.pdf$/i.test(f.filename));
    if (hasPdf && view.layout === undefined) {
      try { view.layout = await api(`/api/library/${it.id}/layout`); } catch (e) {
        if (e.status !== 404) { reader.replaceChildren(el("p", { class: "bad" }, e.message)); return; }
        view.layout = null;
      }
      if (view.kind !== "item" || view.data !== it) return;
    }
    const layout = hasPdf ? view.layout : null;
    const mode = layout ? (view.textMode || "layout") : "plain";
    if (layout) {
      modes.hidden = false;
      modes.replaceChildren(...[["layout", "图文排版"], ["plain", "纯文本"]].map(([k, label]) => el("button", {
        type: "button", role: "radio", "aria-checked": String(mode === k), onclick: () => { view.textMode = k; renderItem(); },
      }, label)), el("span", { class: "dim" }, mode === "layout" ? "MinerU 解析：文字、图和表按论文原来的顺序排列" : "PDF 文字层，按页"));
    }
    if (mode === "layout") { paperReading.layout(reader, layout); return; }
    if (hasPdf && !layout) {
      body.insertBefore(el("p", { class: "dim text-hint" }, "在「精读」里用 MinerU 解析这篇 PDF 后，这里会把文字、图和表按论文原来的顺序排好。"), reader);
    }
    try {
      const text = view.text || (view.text = await api(`/api/library/${it.id}/text?max_chars=200000`));
      if (view.kind !== "item" || view.data !== it) return;
      if (!text.chunks.length) { reader.replaceChildren(el("p", { class: "dim" }, "没有可读的文字。")); return; }
      const pages = [];
      let current = null;
      for (const c of text.chunks) {
        if (!current || current.page !== c.page || current.file !== c.file_id) {
          current = { page: c.page, file: c.file_id, box: el("section", { class: "pg", "data-page": c.page ?? "" },
            c.page != null && el("div", { class: "pg-no" }, `第 ${c.page} 页`)) };
          pages.push(current.box);
        }
        current.box.append(el("p", {}, c.text));
      }
      reader.replaceChildren(...pages);
      if (text.truncated) reader.append(el("p", { class: "dim" }, "正文较长，只显示了前面一部分。"));
    } catch (e) { reader.replaceChildren(el("p", { class: "bad" }, e.message)); }
  }

  function offerExcerpt(reader) {
    const sel = getSelection();
    const quote = sel && !sel.isCollapsed ? paperReading.quote(sel).replace(/\s+/g, " ").trim() : "";
    if (quote.length < 8 || !reader.contains(sel.anchorNode)) { selBar.hidden = true; return; }
    const pageBox = (sel.anchorNode.nodeType === 1 ? sel.anchorNode : sel.anchorNode.parentElement).closest(".pg");
    const page = pageBox?.dataset.page ? Number(pageBox.dataset.page) : null;
    const rect = sel.getRangeAt(0).getBoundingClientRect();
    const make = async (andGround) => {
      selBar.hidden = true;
      try {
        const ex = await api(`/api/library/${view.id}/excerpts`, { method: "POST", json: { quote, page } });
        getSelection().removeAllRanges();
        toast(ex.verified ? "已摘录" : "已摘录（没能在正文中定位）");
        await openItem(view.id, andGround ? "excerpts" : view.tab);
        if (andGround) groundDialog(ex);
      } catch (e) { fail(e); }
    };
    selBar.replaceChildren(
      el("button", { type: "button", onclick: () => make(false) }, "摘录"),
      el("button", { type: "button", onclick: () => make(true) }, "摘录并关联节点"),
      el("button", { type: "button", onclick: () => { selBar.hidden = true; askAgent(`关于 [@${view.data.citekey}]${page ? ` 第 ${page} 页` : ""}的这段话：「${quote.slice(0, 600)}」\n`, false); } }, "问 Agent"));
    selBar.hidden = false;
    const r = root.getBoundingClientRect();
    selBar.style.left = `${Math.max(8, Math.min(rect.left + rect.width / 2 - r.left - selBar.offsetWidth / 2, r.width - selBar.offsetWidth - 8))}px`;
    selBar.style.top = `${Math.max(8, rect.top - r.top - selBar.offsetHeight - 8)}px`;
  }

  async function attachFile() {
    try {
      const sent = await pickAndUpload(q(`/api/library/${view.id}/files`), { accept: FILE_TYPES, multiple: true });
      for (const s of sent) {
        const out = await s.response.json().catch(() => ({}));
        if (!s.response.ok) throw new Error(out.detail || `上传失败（${s.response.status}）`);
      }
      if (sent.length) { toast("文件已添加"); openItem(view.id, "text"); load(); }
    } catch (e) { fail(e); }
  }

  async function deleteFile(f) {
    if (!await confirmAction(`删除文件「${f.filename}」？从它提取的文字也会删除，摘录保留。`)) return;
    try { await api(`/api/library/files/${f.id}`, { method: "DELETE" }); openItem(view.id); load(); } catch (e) { fail(e); }
  }

  function renderExcerpts(body) {
    const exs = view.data.excerpts;
    if (!exs.length) { body.append(el("p", { class: "dim" }, "还没有摘录。在「正文」里选中一段文字即可摘录。")); return; }
    const grounded = new Map();
    for (const g of view.data.groundings) if (g.excerpt_id) grounded.set(g.excerpt_id, [...(grounded.get(g.excerpt_id) || []), g]);
    for (const ex of exs) {
      const comment = el("textarea", { rows: "1", placeholder: "批注…", "aria-label": "批注", value: ex.comment || "" });
      comment.addEventListener("change", async () => {
        try { await api(`/api/excerpts/${ex.id}`, { method: "PATCH", json: { comment: comment.value } }); } catch (e) { fail(e); }
      });
      body.append(el("article", { class: "excerpt" },
        el("blockquote", {}, ex.quote),
        el("div", { class: "ex-meta" }, ex.page != null ? `第 ${ex.page} 页` : "", !ex.verified && el("span", { class: "warn" }, "未在正文中定位"),
          el("span", { class: "dim" }, ex.author === "human" ? "你" : "Agent")),
        comment,
        ...(grounded.get(ex.id) || []).map(groundingRow),
        el("div", { class: "ex-acts" },
          el("button", { type: "button", class: "btn sm primary", onclick: () => groundDialog(ex) }, "关联到节点"),
          el("button", { type: "button", class: "btn sm", onclick: () => { setContext({ item_id: view.id, excerpt_id: ex.id }); askAgent("这条摘录能支持或反驳图里的哪些论断？", false); } }, "问 Agent"),
          el("button", { type: "button", class: "btn sm", onclick: () => deleteExcerpt(ex) }, "删除"))));
    }
  }

  async function deleteExcerpt(ex) {
    if (!await confirmAction("删除这条摘录？")) return;
    try { await api(`/api/excerpts/${ex.id}`, { method: "DELETE" }); openItem(view.id); load(); } catch (e) { fail(e); }
  }

  function groundingRow(g) {
    const s = STANCES[g.stance] || { label: g.stance, tone: "dim" };
    const t = typeOf(g.target_type);
    return el("div", { class: "grounding" },
      el("span", { class: `stance ${s.tone}` }, s.label),
      el("button", { type: "button", class: "target", style: `--c:${t.color}`, onclick: () => openNode(g.target_id) },
        el("i", {}), g.target_name || g.target_id),
      g.pending && el("span", { class: "warn" }, "待审核"),
      el("span", { class: "why" }, g.rationale),
      el("button", { type: "button", class: "x", "aria-label": "移除依据", onclick: () => removeGrounding(g) }, "×"));
  }

  async function removeGrounding(g) {
    if (!await confirmAction(`移除这条依据（${g.target_name || g.target_id}）？可以撤销。`)) return;
    try { await api(`/api/groundings/${g.id}`, { method: "DELETE" }); toast("已移除依据"); openItem(view.id); load(); } catch (e) { fail(e); }
  }

  function renderGroundings(body) {
    const gs = view.data.groundings;
    body.append(el("p", { class: "dim" }, "依据把这篇文献接到图上：它陈述、支持、反驳或限定了哪些节点。"));
    if (!gs.length) body.append(el("p", { class: "dim" }, "还没有依据。"));
    for (const g of gs) body.append(groundingRow(g));
    body.append(el("button", { type: "button", class: "btn sm primary", onclick: () => groundDialog(null) }, "关联到节点"));
  }

  function groundDialog(excerpt) {
    const all = nodes();
    const input = el("input", { type: "search", placeholder: "搜索节点", "aria-label": "搜索节点" });
    const list = el("ul", { class: "pick" });
    let target = null;
    const stance = el("select", { "aria-label": "关系" }, ...Object.entries(STANCES).map(([k, v]) => el("option", { value: k, title: v.hint }, `${v.label} — ${v.hint}`)));
    const why = el("input", { type: "text", placeholder: "这篇文献怎样支撑它（必填）", "aria-label": "理由", value: excerpt?.comment || "" });
    const ok = el("button", { type: "submit", class: "btn primary", disabled: true }, "关联");
    const draw = () => {
      const needle = input.value.trim().toLowerCase();
      const hits = all.filter((n) => !needle || n.name.toLowerCase().includes(needle) || (n.aliases || []).some((a) => a.toLowerCase().includes(needle)))
        .sort((a, b) => (["Claim", "Hypothesis", "Observation"].includes(b.type) - ["Claim", "Hypothesis", "Observation"].includes(a.type)))
        .slice(0, 30);
      list.replaceChildren(...(hits.length ? hits.map((n) => el("li", {
        class: target?.id === n.id ? "on" : "", style: `--c:${typeOf(n.type).color}`,
        onclick: () => { target = n; ok.disabled = !why.value.trim(); draw(); },
      }, el("i", {}), n.name, el("small", {}, typeOf(n.type).label))) : [el("li", { class: "dim" }, all.length ? "没有匹配的节点" : "图里还没有节点")]));
    };
    input.addEventListener("input", draw);
    why.addEventListener("input", () => { ok.disabled = !target || !why.value.trim(); });
    const dlg = el("form", { class: "ground-dlg glass", onsubmit: async (e) => {
      e.preventDefault();
      if (!target || !why.value.trim()) return;
      ok.disabled = true;
      try {
        await api("/api/groundings", { method: "POST", json: { target_id: target.id, item_id: view.id, stance: stance.value,
          rationale: why.value.trim(), excerpt_id: excerpt?.id || null } });
        toast(`已关联到「${target.name}」`);
        dlg.remove();
        openItem(view.id);
        load();
      } catch (err) { fail(err); ok.disabled = false; }
    } },
      el("h3", {}, "关联到节点"),
      excerpt && el("blockquote", {}, excerpt.quote.slice(0, 280)),
      input, list, el("div", { class: "lib-row" }, stance), why,
      el("div", { class: "lib-row end" }, el("button", { type: "button", class: "btn", onclick: () => dlg.remove() }, "取消"), ok));
    main.querySelector(".ground-dlg")?.remove();
    main.append(dlg);
    draw();
    input.focus();
  }

  function renderItemNotes(body) {
    const it = view.data;
    if (!it.notes.length) body.append(el("p", { class: "dim" }, "还没有笔记引用这篇文献。"));
    for (const n of it.notes) body.append(el("button", { type: "button", class: "note-link", onclick: () => { side = "notes"; load(); openNote(n.id); } },
      el("strong", {}, n.title), el("span", {}, (n.preview || "").replace(/\s+/g, " ").slice(0, 140))));
    body.append(el("button", { type: "button", class: "btn sm primary", onclick: () => newNote(`读《${it.title.slice(0, 40)}》`, `[@${it.citekey}]\n\n`) }, "写笔记"));
  }

  function renderInfo(body) {
    const it = view.data;
    const csl = it.csl || {};
    const field = (label, value, name, multi) => {
      const input = el(multi ? "textarea" : "input", { name, "aria-label": label, value: value ?? "", rows: multi ? "4" : null });
      return el("label", { class: "field" }, el("span", {}, label), input);
    };
    const names = (csl.author || []).map((p) => p.literal || [p.family, p.given].filter(Boolean).join(", ")).join("\n");
    const year = csl.issued?.["date-parts"]?.[0]?.[0] ?? "";
    const kind = el("select", { name: "kind", "aria-label": "类型" }, ...Object.entries(KINDS).map(([k, v]) => el("option", { value: k, selected: it.kind === k }, v)));
    const form = el("form", { class: "info-form", onsubmit: (e) => {
      e.preventDefault();
      const f = new FormData(form);
      const authors = String(f.get("authors")).split("\n").map((s) => s.trim()).filter(Boolean).map((s) => {
        if (/[一-鿿]/.test(s) || !s.includes(",")) return /[一-鿿]/.test(s) ? { literal: s } : { family: s };
        const [family, given] = s.split(",").map((x) => x.trim());
        return { family, given };
      });
      const next = { ...csl, title: f.get("title"), author: authors, "container-title": f.get("container") || undefined,
        DOI: f.get("doi") || undefined, URL: f.get("url") || undefined, abstract: f.get("abstract") || undefined };
      if (f.get("year")) next.issued = { "date-parts": [[Number(f.get("year"))]] }; else delete next.issued;
      const tagList = String(f.get("tags")).split(/[,，;；]/).map((s) => s.trim()).filter(Boolean);
      patch({ csl: next, kind: kind.value, tags: tagList, citekey: f.get("citekey") || undefined });
    } },
      field("标题", it.title, "title"),
      field("作者（每行一位：姓, 名）", names, "authors", true),
      el("div", { class: "lib-row" }, field("年份", year, "year"), el("label", { class: "field" }, el("span", {}, "类型"), kind)),
      field("期刊 / 出版物", csl["container-title"], "container"),
      el("div", { class: "lib-row" }, field("DOI", it.doi, "doi"), field("引用键", it.citekey, "citekey")),
      field("网址", it.url, "url"),
      field("标签（逗号分隔）", (it.tags || []).join("，"), "tags"),
      field("摘要", it.abstract, "abstract", true),
      el("p", { class: "dim" }, `来源：${originText(it.origin)} · 添加者 ${it.added_by === "human" ? "你" : it.added_by}`),
      el("div", { class: "lib-row end" }, el("button", { type: "submit", class: "btn primary" }, "保存")));
    body.append(form);
  }

  const originText = (o) => ({ doi: `DOI ${o?.ref}`, arxiv: `arXiv ${o?.ref}`, isbn: `ISBN ${o?.ref}`, openalex: `OpenAlex ${o?.ref}`,
    url: "网页", upload: `上传的文件 ${o?.filename || ""}`, import: `导入（${o?.format || "文件"}）`, manual: "手动填写" })[o?.kind] || "未知";

  // ---------------------------------------------------------------- adding

  async function uploadNew() {
    try {
      const sent = await pickAndUpload(q("/api/library/upload"), { accept: FILE_TYPES, multiple: true });
      let last = null;
      for (const s of sent) {
        const out = await s.response.json().catch(() => ({}));
        if (!s.response.ok) { toast(`${s.name}：${out.detail || `上传失败（${s.response.status}）`}`, true); continue; }
        last = out;
      }
      if (last) afterUpload(last, sent.length);
    } catch (e) { fail(e); }
  }

  function afterUpload(out, count = 1) {
    toast(out.existing ? "文献库里已有这篇，文件已加到它下面" : count > 1 ? `已添加 ${count} 个文件` : out.needs_metadata ? "已添加。没有找到 DOI，请补全题录信息。" : `已添加：${out.item.title}`);
    load();
    openItem(out.item.id, out.needs_metadata ? "info" : "text");
  }

  async function importFile() {
    try {
      const sent = await pickAndUpload(q("/api/library/import"), { accept: IMPORT_TYPES });
      for (const s of sent) await importResult(s.response);
    } catch (e) { fail(e); }
  }

  async function importResult(res) {
    const out = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(out.detail || `导入失败（${res.status}）`);
    toast(`导入 ${out.added.length} 条${out.existing.length ? `，${out.existing.length} 条已存在` : ""}${out.skipped.length ? `，跳过 ${out.skipped.length} 条` : ""}`, !!out.skipped.length && !out.added.length);
    load();
  }

  async function manualItem() {
    try {
      const out = await api("/api/library", { method: "POST", json: { csl: { title: "未命名文献", type: "document" } } });
      await load();
      openItem(out.item.id, "info");
    } catch (e) { fail(e); }
  }

  // ---------------------------------------------------------------- notes

  async function newNote(title = "新笔记", body = "") {
    try {
      const n = await api("/api/notes", { method: "POST", json: { title, body } });
      side = "notes";
      await load();
      openNote(n.id, true);
    } catch (e) { fail(e); }
  }

  async function openNote(id, focus) {
    try {
      const n = await api(`/api/notes/${encodeURIComponent(id)}`);
      view = { kind: "note", id, data: n };
      setContext({ note_id: id });
      renderSide();
      renderNote(focus);
    } catch (e) { fail(e); }
  }

  function renderNote(focus) {
    const n = view.data;
    const title = el("input", { type: "text", class: "note-title", value: n.title, "aria-label": "标题" });
    const body = el("textarea", { class: "note-body", value: n.body, "aria-label": "正文", placeholder: "用 [@引用键] 引用文献，[[n_…]] 指向节点。" });
    const preview = el("div", { class: "note-preview" });
    const status = el("span", { class: "dim" }, n.author === "human" ? "" : "Agent 写的笔记");
    let timer = null, saving = Promise.resolve();
    const save = () => {
      clearTimeout(timer);
      status.textContent = "保存中…";
      saving = saving.then(() => api(`/api/notes/${n.id}`, { method: "PATCH", json: { title: title.value || "无标题", body: body.value } }))
        .then((saved) => { view.data = saved; status.textContent = "已保存"; renderPreview(preview, saved.body); refreshNotesSoon(); })
        .catch((e) => { status.textContent = "保存失败"; fail(e); });
    };
    const later = () => { clearTimeout(timer); status.textContent = "未保存"; timer = setTimeout(save, 900); };
    title.addEventListener("input", later);
    body.addEventListener("input", () => { later(); renderPreview(preview, body.value); });
    body.addEventListener("keydown", (e) => { if ((e.metaKey || e.ctrlKey) && e.key === "s") { e.preventDefault(); save(); } });
    const cite = el("button", { type: "button", class: "btn sm", onclick: () => citePicker(body, later) }, "插入引用");
    const pin = el("button", { type: "button", class: "btn sm", onclick: async () => {
      try { view.data = await api(`/api/notes/${n.id}`, { method: "PATCH", json: { pinned: !n.pinned } }); load(); renderNote(); } catch (e) { fail(e); }
    } }, n.pinned ? "取消置顶" : "置顶");
    const archived = n.status === "archived";
    const restore = async () => {
      try {
        await api(`/api/notes/${n.id}`, { method: "PATCH", json: { archived: false } });
        toast(`已恢复「${n.title}」`);
        notesArchived = false;
        await load();
        openNote(n.id);
      } catch (e) { fail(e); }
    };
    const del = archived ? el("button", { type: "button", class: "btn sm primary", onclick: restore }, "恢复")
      : el("button", { type: "button", class: "btn sm", onclick: async () => {
        if (!await confirmAction(`归档笔记「${n.title}」？之后可以在笔记列表底部的「已归档的笔记」里恢复。`)) return;
        try { await api(`/api/notes/${n.id}`, { method: "PATCH", json: { archived: true } }); show({ kind: "empty" }); load(); } catch (e) { fail(e); }
      } }, "归档");
    main.replaceChildren(el("div", { class: "note" },
      archived && el("div", { class: "note-archived" }, "这篇笔记已归档，不在笔记列表里。",
        el("button", { type: "button", class: "btn sm", onclick: restore }, "恢复到笔记")),
      el("div", { class: "note-bar" }, title, status, cite, !archived && pin, del,
        el("button", { type: "button", class: "btn sm", onclick: () => askAgent("请在这篇笔记末尾补充一段，把相关文献和图中的节点联系起来。", false) }, "让 Agent 补充")),
      el("div", { class: "note-split" }, body, preview)));
    renderPreview(preview, n.body);
    if (focus) body.focus();
  }

  function citePicker(textarea, changed) {
    const box = el("div", { class: "cite-pick glass" });
    const input = el("input", { type: "search", placeholder: "搜索文献", "aria-label": "搜索文献" });
    const list = el("ul", { class: "pick" });
    const draw = async () => {
      const p = new URLSearchParams({ limit: "20" });
      if (input.value.trim()) p.set("q", input.value.trim());
      const out = await api(`/api/library?${p}`).catch(() => ({ items: [] }));
      list.replaceChildren(...out.items.map((it) => el("li", { onclick: () => {
        const at = textarea.selectionStart ?? textarea.value.length;
        const ins = `[@${it.citekey}]`;
        textarea.setRangeText(ins, at, textarea.selectionEnd ?? at, "end");
        textarea.dispatchEvent(new Event("input"));
        changed();
        box.remove();
        textarea.focus();
      } }, it.title, el("small", {}, `@${it.citekey}`))));
    };
    input.addEventListener("input", draw);
    box.append(input, list, el("button", { type: "button", class: "btn sm", onclick: () => box.remove() }, "关闭"));
    main.querySelector(".cite-pick")?.remove();
    main.append(box);
    draw();
    input.focus();
  }

  /* Paragraphs of plain text with chips for [@citekey], [[n_…]], [[ex_…]], [[it_…]], and formulas ($…$, $$…$$,
     \(…\), \[…\]) drawn by KaTeX. Other Markdown is shown as typed. */
  function renderPreview(box, text) {
    const byId = new Map(nodes().map((n) => [n.id, n]));
    box.replaceChildren(...(text || "").split(/\n{2,}/).map((para) => {
      const p = el("p");
      let at = 0, block = false;
      // A display formula is its own block, so the line breaks around it would show as blank lines (pre-wrap).
      const text = (s, display) => refsInto(p, (block ? s.replace(/^\n/, "") : s).replace(display ? /\n$/ : /$^/, ""), byId);
      for (const m of para.matchAll(MATH)) {
        const display = m[1] != null || m[2] != null;
        text(para.slice(at, m.index), display);
        at = m.index + m[0].length;
        block = display;
        p.append(paperReading.math((m[1] ?? m[2] ?? m[3] ?? m[4]).trim(), display));
      }
      text(para.slice(at), false);
      return p;
    }));
  }

  /** Text with its [@citekey], [[n_…]], [[ex_…]] and [[it_…]] turned into chips, appended to `p`. */
  function refsInto(p, para, byId) {
    let last = 0;
    for (const m of para.matchAll(REF)) {
      let before = para.slice(last, m.index);
      last = m.index + m[0].length;
      if (m[2]) {
        const key = m[2].replace(/[.,;:]+$/, "");
        const tail = m[2].slice(key.length);
        if (before.endsWith("[") && !tail && para[last] === "]") { before = before.slice(0, -1); last += 1; } // [@key] -> chip
        p.append(before, el("button", { type: "button", class: "ref cite", onclick: () => openCitekey(key) }, `@${key}`), tail);
        continue;
      }
      p.append(before);
      if (m[1].startsWith("n_")) {
        const n = byId.get(m[1]);
        p.append(el("button", { type: "button", class: "ref node", style: n ? `--c:${typeOf(n.type).color}` : "", onclick: () => openNode(m[1]) }, n ? n.name : "已删除的节点"));
      } else if (m[1].startsWith("ex_")) {
        p.append(el("button", { type: "button", class: "ref ex", onclick: () => openExcerpt(m[1]) }, "摘录"));
      } else {
        p.append(el("button", { type: "button", class: "ref cite", onclick: () => openItem(m[1]) }, "文献"));
      }
    }
    p.append(para.slice(last));
  }

  async function openCitekey(key) {
    try {
      const out = await api(`/api/library?q=${encodeURIComponent(key)}&limit=20`);
      const it = out.items.find((x) => x.citekey === key);
      if (it) openItem(it.id); else toast(`文献库里没有 @${key}`, true);
    } catch (e) { fail(e); }
  }

  async function openExcerpt(id) {
    try {
      const ex = await api(`/api/excerpts/${id}`);
      side = "items";
      await openItem(ex.item_id, "excerpts");
      load();
    } catch (e) { fail(e); }
  }

  function refreshNotesSoon() { if (side === "notes") { clearTimeout(refreshTimer); refreshTimer = setTimeout(load, 600); } }

  // ---------------------------------------------------------------- outside

  function onMessage(msg) {
    if (!visible) return;
    clearTimeout(refreshTimer);
    refreshTimer = setTimeout(() => {
      load();
      // Re-draw the open item or note only when that cannot lose anything: no dialog, nothing being typed, and
      // not the reader (which would jump back to the top). Changes made on this page re-draw by themselves.
      // The reading tab fetches its own state and redraws only when the job moved on.
      if (view.kind === "item" && view.tab === "reading") { if (!msg.item || msg.item === view.id) paperReading.refresh(view.id); return; }
      const busy = main.querySelector(".ground-dlg, .cite-pick") || main.contains(document.activeElement);
      if (busy) return;
      if (view.kind === "item" && view.tab !== "text" && (!msg.item || msg.item === view.id)) openItem(view.id);
      else if (view.kind === "note" && (!msg.note || msg.note === view.id)) openNote(view.id);
    }, 300);
  }

  let loaded = false;
  return {
    show() {
      visible = true;
      root.hidden = false;
      if (!loaded) { loaded = true; renderSide(); show({ kind: "empty" }); }
      load();
      if (view.kind === "item") setContext({ item_id: view.id });
      else if (view.kind === "note") setContext({ note_id: view.id });
    },
    hide() { visible = false; root.hidden = true; selBar.hidden = true; setContext(null); },
    onMessage,
    openItem: (id, tab) => { side = "items"; return openItem(id, tab); },
  };
}
