/* The library's close-reading tab (精读): MinerU turns the item's PDF into Markdown and figures, the model reads it,
   and the tab shows, top to bottom, a small graph of the paper's argument (graph zone) and the overview sections
   (content zone): logic, framework, core conclusions, experiment design, figures and tables, method details.

   Everything the model or the paper wrote goes into the page as text nodes, never as markup. */

import { desktop, download, go, openExternal, request, routes } from "/static/net.js";

const SVG = "http://www.w3.org/2000/svg";
const SECTIONS = [
  ["logic", "论文逻辑概览"], ["framework", "研究框架"], ["conclusions", "核心结论"],
  ["experiments", "主要实验设计"], ["figures", "图表解读"], ["methods", "方法细节"],
];
const KINDS = {
  question: ["研究问题", "#f7931a"], motivation: ["动机", "#f5b75a"], hypothesis: ["假设", "#c39bff"],
  theory: ["理论", "#9aa7ff"], data: ["数据", "#5ec8f2"], method: ["方法", "#57e39b"], experiment: ["实验", "#3fd0c0"],
  finding: ["发现", "#ffd166"], conclusion: ["结论", "#ff7a8a"], concept: ["概念", "#b8b2a7"],
};
const KIND_ORDER = Object.keys(KINDS);
const STEPS = [["uploading", "上传 PDF"], ["parsing", "MinerU 解析"], ["downloading", "取回结果"], ["summarizing", "模型精读"]];

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
const svg = (tag, attrs = {}, ...children) => {
  const node = document.createElementNS(SVG, tag);
  for (const [k, v] of Object.entries(attrs)) if (v != null) node.setAttribute(k, v);
  for (const c of children) if (c != null) node.append(c);
  return node;
};

/** "a **b** c" as text nodes with <strong> for the bold parts. */
function inline(text) {
  const out = [];
  let last = 0;
  for (const m of String(text || "").matchAll(/\*\*(.+?)\*\*/g)) {
    if (m.index > last) out.push(text.slice(last, m.index));
    out.push(el("strong", {}, m[1]));
    last = m.index + m[0].length;
  }
  if (last < String(text || "").length) out.push(String(text).slice(last));
  return out;
}

const when = (iso) => (iso ? new Date(iso).toLocaleString("zh-CN", { dateStyle: "medium", timeStyle: "short" }) : "");

// ------------------------------------------------------------------ the graph zone

/** Split a label into at most two lines that fit `em` widths (CJK counts 1, other characters about 0.56). */
function wrap(label, em = 9) {
  const lines = [""];
  let width = 0;
  const words = label.match(/[⺀-￿]|[^\s⺀-￿]+|\s+/g) || [];
  const sizeOf = (w) => (/[⺀-￿]/.test(w) ? 1 : w.trim() ? w.length * 0.56 : 0.3);
  const total = words.reduce((a, w) => a + sizeOf(w), 0);
  if (total > em && total <= em * 2) em = Math.min(em, total / 2 + 1);  // two even lines, not a full one and a stub
  for (const w of words) {
    const size = sizeOf(w);
    if (width + size > em && lines[lines.length - 1].trim()) {
      if (lines.length === 2) { lines[1] = `${lines[1].trimEnd().slice(0, -1)}…`; return lines; }
      lines.push("");
      width = 0;
      if (!w.trim()) continue;
    }
    lines[lines.length - 1] += w;
    width += size;
  }
  return lines.map((l) => l.trim()).filter(Boolean);
}

/** Columns by the longest path from a source (edges that close a cycle are ignored); without edges, by kind. */
function layers(nodes, edges) {
  const out = new Map(nodes.map((n) => [n.id, 0]));
  if (!edges.length) {
    for (const n of nodes) out.set(n.id, Math.max(0, KIND_ORDER.indexOf(n.kind)));
  } else {
    const next = new Map(nodes.map((n) => [n.id, []]));
    for (const e of edges) next.get(e.from)?.push(e.to);
    const state = new Map(), order = [];
    const visit = (id) => {
      state.set(id, 1);
      for (const t of next.get(id) || []) if (!state.has(t)) visit(t);
      state.set(id, 2);
      order.push(id);
    };
    for (const n of nodes) if (!state.has(n.id)) visit(n.id);
    const rank = new Map(order.reverse().map((id, i) => [id, i]));
    for (const id of order) {
      for (const t of next.get(id) || []) {
        if (rank.get(t) > rank.get(id)) out.set(t, Math.max(out.get(t), out.get(id) + 1));
      }
    }
  }
  const used = [...new Set(out.values())].sort((a, b) => a - b);  // close gaps
  for (const [id, l] of out) out.set(id, used.indexOf(l));
  return out;
}

function drawGraph(graph, caption) {
  const nodes = graph?.nodes || [], edges = graph?.edges || [];
  if (!nodes.length) return el("p", { class: "dim" }, "模型没有给出结构图。");
  const layer = layers(nodes, edges);
  const cols = [];
  for (const n of nodes) (cols[layer.get(n.id)] ||= []).push(n);
  // Order each column by where its predecessors sit, so edges cross less.
  const row = new Map();
  cols.forEach((col, ci) => {
    if (ci) {
      const score = (n) => {
        const ys = edges.filter((e) => e.to === n.id && row.has(e.from)).map((e) => row.get(e.from));
        return ys.length ? ys.reduce((a, b) => a + b, 0) / ys.length : 99;
      };
      col.sort((a, b) => score(a) - score(b));
    }
    col.forEach((n, i) => row.set(n.id, i));
  });
  const W = 124, H = 48, GX = 36, GY = 16, PAD = 14;
  const tallest = Math.max(...cols.map((c) => c.length));
  const width = PAD * 2 + cols.length * W + (cols.length - 1) * GX;
  const height = PAD * 2 + tallest * H + (tallest - 1) * GY;
  const pos = new Map();
  cols.forEach((col, ci) => {
    const offset = ((tallest - col.length) * (H + GY)) / 2;
    col.forEach((n, i) => pos.set(n.id, { x: PAD + ci * (W + GX), y: PAD + offset + i * (H + GY) }));
  });

  // Shrinks with the column down to 70% of full size; narrower than that, the box scrolls sideways instead.
  const root = svg("svg", { viewBox: `0 0 ${width} ${height}`, class: "rd-graph", role: "img",
    style: `max-width:${width}px;min-width:${Math.round(width * 0.7)}px`, "aria-label": `论文结构图：${nodes.length} 个节点` });
  root.append(svg("defs", {}, svg("marker", { id: "rd-arrow", viewBox: "0 0 8 8", refX: 7, refY: 4, markerWidth: 6,
    markerHeight: 6, orient: "auto-start-reverse" }, svg("path", { d: "M0,0 L8,4 L0,8 z", class: "rd-arrowhead" }))));
  const edgeEls = [];
  for (const e of edges) {
    const a = pos.get(e.from), b = pos.get(e.to);
    if (!a || !b) continue;
    let d;
    if (b.x > a.x) {
      const x1 = a.x + W, y1 = a.y + H / 2, x2 = b.x - 2, y2 = b.y + H / 2, mid = (x1 + x2) / 2;
      d = `M${x1},${y1} C${mid},${y1} ${mid},${y2} ${x2},${y2}`;
    } else {  // same column or backwards: loop out below
      const x1 = a.x + W / 2, y1 = a.y + H, x2 = b.x + W / 2, y2 = b.y + H + 2, dip = Math.max(y1, y2) + 26;
      d = `M${x1},${y1} C${x1},${dip} ${x2},${dip} ${x2},${y2}`;
    }
    const path = svg("path", { d, class: "rd-edge", "marker-end": "url(#rd-arrow)" },
      svg("title", {}, e.label || ""));
    path.dataset.from = e.from;
    path.dataset.to = e.to;
    edgeEls.push(path);
    root.append(path);
  }
  const show = (n) => {
    caption.replaceChildren(...(n ? [el("i", { style: `--c:${KINDS[n.kind]?.[1]}` }), el("b", {}, n.label),
      el("span", { class: "dim" }, ` ${KINDS[n.kind]?.[0] || ""}`), n.detail ? `　${n.detail}` : ""]
      : [el("span", { class: "dim" }, "把鼠标移到节点上看说明。")]));
    for (const p of edgeEls) p.classList.toggle("on", Boolean(n) && (p.dataset.from === n.id || p.dataset.to === n.id));
    root.classList.toggle("focus", Boolean(n));
  };
  for (const n of nodes) {
    const { x, y } = pos.get(n.id);
    const color = KINDS[n.kind]?.[1] || KINDS.concept[1];
    const lines = wrap(n.label);
    const g = svg("g", { class: "rd-node", tabindex: 0, transform: `translate(${x},${y})`, style: `--c:${color}` },
      svg("title", {}, `${n.label}${n.detail ? `\n${n.detail}` : ""}`),
      svg("rect", { width: W, height: H, rx: 10 }),
      svg("circle", { cx: 11, cy: 11, r: 3 }),
      svg("text", { x: 20, y: 14.5, class: "rd-kind" }, KINDS[n.kind]?.[0] || ""),
      ...lines.map((l, i) => svg("text", { x: 10, y: (lines.length === 1 ? 34 : 29) + i * 14, class: "rd-label" }, l)));
    g.addEventListener("mouseenter", () => show(n));
    g.addEventListener("focus", () => show(n));
    g.addEventListener("mouseleave", () => show(null));
    g.addEventListener("blur", () => show(null));
    root.append(g);
  }
  show(null);
  return root;
}

// ------------------------------------------------------------------ the tab

export function createReading({ api, q, toast, fail }) {
  let current = null;  // { box, item, data, file }

  const path = (item, file) => `/api/library/${encodeURIComponent(item.id)}/reading${file ? `?file_id=${encodeURIComponent(file)}` : ""}`;
  const busy = (d) => d && (d.status === "converting" || d.status === "summarizing");

  async function render(body, item) {
    const box = el("div", { class: "reading" }, el("p", { class: "dim" }, "读取精读…"));
    body.append(box);
    current = { box, item, data: null, file: current?.item?.id === item.id ? current.file : null };
    await load();
  }

  async function load() {
    const me = current;
    if (!me) return;
    try {
      const data = await api(path(me.item, me.file));
      if (current !== me) return;
      const before = me.data;
      me.data = data;
      me.file = data.file_id;
      // A refresh while nothing changed must not redraw: that would reset images and the reader's scroll.
      if (before && before.status === data.status && before.stage === data.stage && before.updated_at === data.updated_at
          && before.mineru.ready === data.mineru.ready) return;
      draw();
    } catch (e) { me.box.replaceChildren(el("p", { class: "bad" }, e.message)); }
  }

  async function start(reconvert = false) {
    const me = current;
    try {
      me.data = await api(path(me.item).split("?")[0], { method: "POST", json: { file_id: me.file, reconvert } });
      draw();
    } catch (e) { fail(e); load(); }
  }

  function draw() {
    const { box, data: d } = current;
    const parts = [];
    if (!d.files.length) {
      box.replaceChildren(el("div", { class: "drop-hint" }, "这条文献还没有 PDF。上传 PDF 后，可以用 MinerU 转换并生成精读。"));
      return;
    }
    if (d.files.length > 1) {
      const pick = el("select", { "aria-label": "选择 PDF", onchange: () => { current.file = pick.value; current.data = null; load(); } },
        ...d.files.map((f) => el("option", { value: f.id, selected: f.id === d.file_id }, f.filename)));
      parts.push(el("div", { class: "rd-files" }, el("span", { class: "dim" }, "PDF"), pick));
    }
    if (busy(d)) parts.push(progress(d));
    else if (!d.digest) parts.push(startCard(d));
    else if (d.status === "failed") parts.push(el("div", { class: "rd-alert" }, el("span", { class: "bad" }, `重新生成失败：${d.error}`),
      el("button", { type: "button", class: "btn sm", onclick: () => start(false) }, "重试")));
    if (d.digest) parts.push(...reading(d));
    box.replaceChildren(...parts);
    if (busy(d)) {  // progress also arrives over the WebSocket; this covers a missed message
      const me = current;
      setTimeout(() => { if (current === me && me.box.isConnected && busy(me.data)) load(); }, 6000);
    }
  }

  function startCard(d) {
    const needKey = !d.converted && !d.mineru.ready;
    return el("section", { class: "rd-start" },
      el("p", { class: "eyebrow" }, "CLOSE READING / 精读"),
      el("h3", {}, "让这篇论文先被读一遍"),
      el("p", { class: "lede" }, "MinerU 把 PDF 转成 Markdown，并切出图和表；模型再通读全文，画出论文的论证结构，写出逻辑概览、研究框架、核心结论、实验设计、图表解读和方法细节。"),
      d.status === "failed" && el("p", { class: "bad" }, d.error),
      d.converted
        ? el("p", { class: "dim" }, `已有 MinerU 转换结果（${when(d.converted.at)}），这次只需调用模型。`)
        : d.mineru.hosted && el("p", { class: "dim" }, `PDF 会上传到 ${d.mineru.host} 解析；结果保存在这台电脑上，同一个 PDF 只转换一次。`),
      needKey && (d.can_set_key
        ? el("div", { class: "rd-key" },
            el("span", { class: "dim" }, "还没有配置 MinerU 密钥。"),
            el("button", { type: "button", class: "btn sm", onclick: () => go(`${routes.view("settings")}#mineru-settings`) }, "去平台设置配置"),
            el("button", { type: "button", class: "btn sm ghost", onclick: () => openExternal("https://mineru.net/apiManage/token") }, "去 mineru.net 申请"))
        : el("p", { class: "warn" }, "需要运营者在部署环境中配置 MinerU")),
      el("div", { class: "rd-acts" },
        el("button", { type: "button", class: "btn primary", disabled: needKey, onclick: () => start(false) },
          d.status === "failed" ? "重试" : "开始精读")));
  }

  function progress(d) {
    const at = d.status === "summarizing" ? "summarizing" : d.stage || "uploading";
    const index = STEPS.findIndex(([k]) => k === at);
    const steps = d.converted && d.status === "summarizing" && !d.stage ? STEPS.slice(3) : STEPS;
    return el("section", { class: "rd-progress", "aria-live": "polite" },
      el("ol", {}, ...steps.map(([k, label]) => {
        const i = STEPS.findIndex(([s]) => s === k);
        return el("li", { class: i < index ? "done" : i === index ? "now" : "" }, label);
      })),
      el("p", { class: "dim" }, d.status === "summarizing" ? "模型正在通读全文，通常一到三分钟。" : "MinerU 正在转换，长论文可能要几分钟。可以先去做别的，完成后这里会自动更新。"));
  }

  function reading(d) {
    const g = d.digest;
    const caption = el("p", { class: "rd-caption" });
    const legend = [...new Set((g.graph?.nodes || []).map((n) => n.kind))].sort((a, b) => KIND_ORDER.indexOf(a) - KIND_ORDER.indexOf(b));
    const graphZone = el("section", { class: "rd-zone rd-graph-zone", "aria-label": "论文结构图" },
      el("header", {}, el("h3", {}, "论文结构"), el("div", { class: "rd-legend" },
        ...legend.map((k) => el("span", {}, el("i", { style: `--c:${KINDS[k]?.[1]}` }), KINDS[k]?.[0] || k)))),
      el("div", { class: "rd-graph-box" }, drawGraph(g.graph, caption)), caption);

    const nav = el("nav", { class: "rd-nav", "aria-label": "精读目录" });
    const sections = [];
    SECTIONS.forEach(([key, title], i) => {
      const content = key === "figures" ? figureList(d) : points(key, g[key]);
      if (!content) return;
      const sec = el("section", { class: `rd-sec rd-${key}`, id: `rd-${key}` },
        el("h3", {}, el("span", { class: "no" }, String(i + 1).padStart(2, "0")), title), content);
      sections.push(sec);
      nav.append(el("button", { type: "button", onclick: () => sec.scrollIntoView({ behavior: "smooth", block: "start" }) }, title));
    });
    const contentZone = el("section", { class: "rd-zone rd-content", "aria-label": "精读内容" }, nav, ...sections);

    const foot = el("footer", { class: "rd-foot" },
      el("span", { class: "dim" }, [d.converted && `MinerU 转换于 ${when(d.converted.at)}`, d.model && `模型 ${d.model}`,
        d.updated_at && `生成于 ${when(d.updated_at)}`].filter(Boolean).join(" · ")),
      el("div", { class: "rd-acts" },
        !busy(d) && el("button", { type: "button", class: "btn sm", onclick: () => start(false) }, "重新生成解读"),
        !busy(d) && d.mineru.ready && el("button", { type: "button", class: "btn sm", onclick: () => start(true) }, "重新转换 PDF"),
        d.converted && el("button", { type: "button", class: "btn sm", onclick: () => {
          const f = d.files.find((x) => x.id === d.file_id);
          download(q(`/api/library/files/${d.file_id}/mineru/markdown`), `${(f?.filename || "paper").replace(/\.pdf$/i, "")}.md`).catch(fail);
        } }, "下载 Markdown")));
    return [graphZone, contentZone, foot];
  }

  function points(key, list) {
    if (!list?.length) return null;
    if (key === "logic") {
      return el("ol", { class: "rd-chain" }, ...list.map((p) => el("li", {}, p.title && el("b", {}, p.title), el("p", {}, ...inline(p.text)))));
    }
    if (key === "conclusions") {
      return el("div", { class: "rd-cards" }, ...list.map((p, i) => el("div", { class: "rd-card" },
        el("span", { class: "no" }, String(i + 1)), el("div", {}, p.title && el("b", {}, p.title), el("p", {}, ...inline(p.text))))));
    }
    return el("dl", { class: "rd-points" }, ...list.flatMap((p) => [el("dt", {}, p.title || ""), el("dd", {}, ...inline(p.text))]));
  }

  function figureList(d) {
    const figs = new Map((d.figures || []).map((f) => [f.id, f]));
    const told = d.digest.figures || [];
    const items = told.length ? told.map((t) => ({ ...figs.get(t.id), ...t })) : (d.figures || []).slice(0, 12);
    if (!items.length) return d.converted ? el("p", { class: "dim" }, "MinerU 没有在这篇论文里找到图或表。") : null;
    return el("div", { class: "rd-figs" }, ...items.map((f) => {
      const img = el("img", { alt: f.caption || f.title || f.id, loading: "lazy" });
      if (f.name) image(img, d.file_id, f.name);
      return el("figure", { class: "rd-fig" },
        f.name ? el("button", { type: "button", class: "rd-thumb", onclick: () => zoom(img, f), "aria-label": `放大 ${f.id}` }, img) : null,
        el("figcaption", {},
          el("div", { class: "rd-fig-meta" }, el("span", { class: "tag" }, f.id), f.page && el("span", { class: "dim" }, `第 ${f.page} 页`)),
          f.caption && el("p", { class: "rd-fig-cap" }, f.caption),
          f.title && el("b", {}, f.title),
          f.text && el("p", {}, ...inline(f.text))));
    }));
  }

  async function image(img, fileId, name) {
    const url = q(`/api/library/files/${encodeURIComponent(fileId)}/mineru/images/${encodeURIComponent(name)}`);
    if (!desktop) { img.src = url; return; }
    try {  // the desktop app's requests carry text, so the image comes as base64
      const res = await request(`${url}&encoding=base64`);
      const body = await res.json();
      if (res.ok) img.src = `data:${body.media_type};base64,${body.data}`;
    } catch { /* leave the figure without its picture */ }
  }

  function zoom(img, f) {
    const dlg = el("dialog", { class: "rd-zoom glass", onclick: () => dlg.close(), onclose: () => dlg.remove() },
      el("img", { src: img.src, alt: img.alt }), (f.caption || f.title) && el("p", {}, f.caption || f.title));
    document.body.append(dlg);
    dlg.showModal();
  }

  // ---------------------------------------------------------------- 原文 as MinerU laid it out

  /** Pictures load as they scroll into view: the desktop app fetches each one as base64. */
  let seen = null;
  function lazy(img, fileId, name) {
    if (!desktop) { img.loading = "lazy"; image(img, fileId, name); return; }
    seen ||= new IntersectionObserver((entries) => {
      for (const e of entries) if (e.isIntersecting) { seen.unobserve(e.target); image(e.target, e.target.dataset.file, e.target.dataset.name); }
    }, { rootMargin: "600px" });
    img.dataset.file = fileId;
    img.dataset.name = name;
    seen.observe(img);
  }

  /** KaTeX (vendored, so it works offline) loads the first time a page has a formula. */
  let katex;
  function loadKatex() {
    katex ||= new Promise((ok, fail) => {
      document.head.append(el("link", { rel: "stylesheet", href: "/static/vendor/katex/katex.min.css" }));
      document.head.append(el("script", { src: "/static/vendor/katex/katex.min.js", onload: () => ok(window.katex), onerror: fail }));
    });
    return katex;
  }

  /** A formula: the LaTeX source shows until KaTeX has drawn it, and stays when it can't (MinerU sometimes cuts one short). */
  function mathOf(tex, display = false, failed = null) {
    const node = el(display ? "div" : "span", { class: display ? "md-eq" : "md-math", title: tex }, tex);
    loadKatex().then((k) => {
      try {
        k.render(tex, node, { displayMode: display, throwOnError: true, strict: "ignore", output: "html" });
        node.classList.add("tex");
      } catch { failed?.(node); }  // otherwise the source stays
    }, () => failed?.(node));
    return node;
  }

  const runsOf = (runs) => (runs || []).map(([k, s]) => (
    k === "b" ? el("strong", {}, s) : k === "sup" ? el("sup", {}, s) : k === "sub" ? el("sub", {}, s)
      : k === "math" ? mathOf(s) : s));
  const plainOf = (runs) => (runs || []).map(([, s]) => s).join("");
  // Table cells come as plain text; their $…$ spans are formulas too.
  const cellOf = (text) => text.split(/(?<!\\)\$([^$\n]+?)(?<!\\)\$/).map((s, i) => (i % 2 ? mathOf(s) : s));

  function tableOf(rows) {
    return el("div", { class: "md-table" }, el("table", {}, el("tbody", {}, ...rows.map((r) => el("tr", {},
      ...r.map((c) => el(c.head ? "th" : "td", { colspan: c.colspan, rowspan: c.rowspan }, ...cellOf(c.text))))))));
  }

  function picture(fileId, b, caption) {
    const img = el("img", { alt: caption || b.id || "图" });
    lazy(img, fileId, b.name);
    return el("button", { type: "button", class: "md-img", onclick: () => zoom(img, { caption }), "aria-label": `放大${b.id ? ` ${b.id}` : ""}` }, img);
  }

  /** One figure, chart or table; `group` are pictures before it that share its caption (Figure 2 (a)(b)(c)). */
  function figureOf(fileId, b, group = []) {
    const caption = plainOf(b.caption);
    const tag = group[0]?.id && b.id ? `${group[0].id}–${b.id}` : b.id;  // panels keep the 精读 tab's numbering
    const cap = b.caption.length ? el("figcaption", {}, tag && el("span", { class: "tag" }, tag), ...runsOf(b.caption)) : null;
    const notes = b.notes.length ? el("p", { class: "md-fig-note" }, ...runsOf(b.notes)) : null;
    if (b.rows) {  // a table: caption above, the cells as text, and the picture MinerU cut out on request
      const original = b.name && el("button", { type: "button", class: "btn sm ghost", onclick: async () => {
        const img = el("img", { alt: caption || "表" });
        await image(img, fileId, b.name);
        zoom(img, { caption });
      } }, "看原图");
      return el("figure", { class: "md-fig table" }, cap, tableOf(b.rows), notes, original && el("div", { class: "md-fig-acts" }, original));
    }
    const pics = [...group, b].filter((x) => x.name).map((x) => picture(fileId, x, caption));
    return el("figure", { class: `md-fig ${b.kind}` },
      b.kind === "table" && cap, el("div", { class: `md-pics n${Math.min(pics.length, 4)}` }, ...pics), b.kind !== "table" && cap, notes);
  }

  function blockOf(fileId, b) {
    if (b.t === "title") return el(`h${Math.min(b.level + 2, 6)}`, { class: `md-h md-h${b.level}` }, ...runsOf(b.runs));
    if (b.t === "equation") {
      // Drawn from the source; MinerU's cut-out picture stands in when KaTeX can't read it.
      const pic = () => {
        const img = el("img", { alt: "公式" });
        lazy(img, fileId, b.name);
        return el("div", { class: "md-eq pic" }, img);
      };
      if (!b.latex) return pic();
      return mathOf(b.latex, true, b.name && ((node) => node.replaceWith(pic())));
    }
    if (b.t === "code") return el("pre", { class: "md-code" }, b.text);
    return el("p", { class: b.t === "note" ? "md-note" : b.t === "ref" ? "md-ref" : null }, ...runsOf(b.runs));
  }

  /** The paper page by page, text with its figures and tables in place. Text is selectable for excerpts. */
  function renderLayout(reader, d) {
    const pages = d.pages.map((p) => {
      const box = el("section", { class: "pg md-page", "data-page": p.page ?? "" }, p.page != null && el("div", { class: "pg-no" }, `第 ${p.page} 页`));
      let group = [];
      p.blocks.forEach((b, i) => {
        const next = p.blocks[i + 1];
        const isPic = b.t === "figure" && !b.rows;
        // Uncaptioned pictures followed by another picture are panels of one figure: hold them for its caption.
        if (isPic && !b.caption.length && next?.t === "figure" && !next.rows && b.name) { group.push(b); return; }
        if (b.t === "figure") box.append(figureOf(d.file_id, b, isPic ? group : []));
        else { for (const g of group) box.append(figureOf(d.file_id, g)); box.append(blockOf(d.file_id, b)); }
        group = [];
      });
      for (const g of group) box.append(figureOf(d.file_id, g));
      return box;
    });
    reader.replaceChildren(...pages);
    if (d.truncated) reader.append(el("p", { class: "dim" }, "论文较长，只显示了前面一部分。"));
    if (!reader.dataset.mathCopy) {  // copying drawn formulas gives their LaTeX, not KaTeX's glyph soup
      reader.dataset.mathCopy = "1";
      reader.addEventListener("copy", (e) => {
        const sel = getSelection();
        if (!sel || sel.isCollapsed || !reader.querySelector(".tex")) return;
        e.clipboardData.setData("text/plain", selectedText(sel));
        e.preventDefault();
      });
    }
  }

  /** The selection as text, with each drawn formula given back as $LaTeX$. */
  function selectedText(sel) {
    if (!sel.rangeCount) return "";
    const range = sel.getRangeAt(0);
    const box = el("div", {}, range.cloneContents());
    // A selection inside one formula clones only its glyphs: count the whole formula.
    const inMath = (range.commonAncestorContainer.nodeType === 1 ? range.commonAncestorContainer : range.commonAncestorContainer.parentElement)?.closest(".tex");
    if (inMath) return inMath.classList.contains("md-eq") ? `$$${inMath.title}$$` : `$${inMath.title}$`;
    if (!box.querySelector(".tex")) return sel.toString();
    for (const m of box.querySelectorAll(".tex")) m.replaceWith(m.classList.contains("md-eq") ? `\n$$${m.title}$$\n` : `$${m.title}$`);
    for (const b of box.querySelectorAll("p, div, li, h3, h4, h5, h6, figcaption, tr")) b.append("\n");
    for (const c of box.querySelectorAll("td, th")) c.append("\t");
    return box.textContent;
  }

  return {
    render,
    layout: renderLayout,
    quote: selectedText,
    math: mathOf,
    /** A library message for this item: fetch the reading again (draws only if something changed). */
    refresh(itemId) { if (current && current.item.id === itemId && current.box.isConnected) load(); },
    busy: () => busy(current?.data),
  };
}
