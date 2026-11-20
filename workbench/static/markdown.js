/* Markdown drawn into DOM nodes, shared by the chat, notes, the writing preview and the reading tab: paragraphs,
   headings, lists, quotes, code, rules, pipe tables, and formulas ($…$, $$…$$, \(…\), \[…\]) drawn by KaTeX.

   Everything is built as text nodes and elements (no innerHTML), so agent output and paper text cannot inject
   markup. Callers draw their own references through ctx:
     ctx.cite(keys, locators, inText)  [@key, p. 3] and bare @key (left as text when absent)
     ctx.claim(nodeId, children)       [text]{.claim node=n_…}
     ctx.ref(id)                       [[n_…]], [[ex_…]], [[it_…]] (left as text when absent)
     ctx.intent                        show <!-- comments --> as a note instead of dropping them
     ctx.breaks                        a single line break inside a paragraph is kept (chat, notes) */

const el = (tag, attrs = {}, ...children) => {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null || v === false) continue;
    if (k === "class") node.className = v;
    else if (k === "style") node.style.cssText = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children) if (c != null && c !== false) node.append(c);
  return node;
};

const KEY = "[\\w\\u4e00-\\u9fff][\\w\\u4e00-\\u9fff:.#$%&+?<>~/-]*";
const REF_ID = /^\[\[((?:n|ex|it)_[0-9a-f]{12})\]\]/;

// ------------------------------------------------------------------ formulas

let katex;
/** KaTeX (vendored, so it works offline) loads the first time something has a formula. */
export function loadKatex() {
  katex ||= new Promise((ok, fail) => {
    document.head.append(el("link", { rel: "stylesheet", href: "/static/vendor/katex/katex.min.css" }));
    document.head.append(el("script", { src: "/static/vendor/katex/katex.min.js", onload: () => ok(window.katex), onerror: fail }));
  });
  return katex;
}

/** A formula: the LaTeX source shows until KaTeX has drawn it, and stays (or `failed(node)` runs) when it can't. */
export function mathNode(tex, display = false, failed = null) {
  const node = el(display ? "div" : "span", { class: display ? "md-eq" : "md-math", title: tex }, tex);
  loadKatex().then((k) => {
    try {
      k.render(tex, node, { displayMode: display, throwOnError: true, strict: "ignore", output: "html" });
      node.classList.add("tex");
    } catch { failed?.(node); }
  }, () => failed?.(node));
  return node;
}

/** Where an inline $…$ that starts at `i` ends, or -1. It must hug its content and not touch a letter or digit
    outside, so "costs $5 and $6" stays text. */
function dollarEnd(s, i) {
  if (/[\w$\\]/.test(s[i - 1] || "") || !s[i + 1] || /\s|\$/.test(s[i + 1])) return -1;
  for (let j = i + 1; j < s.length; j++) {
    if (s[j] === "\n") return -1;
    if (s[j] === "\\") { j++; continue; }
    if (s[j] === "$") return !/\s/.test(s[j - 1]) && !/[\w$]/.test(s[j + 1] || "") ? j : -1;
  }
  return -1;
}

// ------------------------------------------------------------------ inline

function matchBracket(s, open) {
  let depth = 0;
  for (let i = open; i < s.length; i++) {
    if (s[i] === "\\") { i++; continue; }
    if (s[i] === "[") depth++;
    else if (s[i] === "]" && --depth === 0) return i;
  }
  return -1;
}

/** Inline Markdown into nodes: emphasis, code, formulas, links (shown, not followed) and the caller's references. */
export function inline(s, ctx = {}) {
  s = String(s ?? "");
  const out = [];
  let text = "";
  const flush = () => { if (text) { out.push(text); text = ""; } };
  for (let i = 0; i < s.length;) {
    const c = s[i];
    if (c === "\\" && (s[i + 1] === "(" || s[i + 1] === "[")) {
      const end = s.indexOf(s[i + 1] === "(" ? "\\)" : "\\]", i + 2);
      if (end > i + 2) { flush(); out.push(mathNode(s.slice(i + 2, end).trim(), s[i + 1] === "[")); i = end + 2; continue; }
    }
    if (c === "\\" && i + 1 < s.length) { text += s[i + 1]; i += 2; continue; }
    if (c === "`") {
      const end = s.indexOf("`", i + 1);
      if (end > i) { flush(); out.push(el("code", {}, s.slice(i + 1, end))); i = end + 1; continue; }
    }
    if (c === "$") {
      if (s[i + 1] === "$") {
        const end = s.indexOf("$$", i + 2);
        if (end > i + 2) { flush(); out.push(mathNode(s.slice(i + 2, end).trim(), true)); i = end + 2; continue; }
      } else {
        const end = dollarEnd(s, i);
        if (end > 0) { flush(); out.push(mathNode(s.slice(i + 1, end))); i = end + 1; continue; }
      }
    }
    if (c === "!" && s[i + 1] === "[") {
      const close = matchBracket(s, i + 1);
      if (close > 0 && s[close + 1] === "(") {
        const end = s.indexOf(")", close);
        if (end > 0) { flush(); out.push(el("span", { class: "ms-img" }, `［图片：${s.slice(i + 2, close) || "无说明"}］`)); i = end + 1; continue; }
      }
    }
    if (c === "[") {
      const id = ctx.ref && s.slice(i).match(REF_ID);
      if (id) { flush(); out.push(ctx.ref(id[1])); i += id[0].length; continue; }
      const close = matchBracket(s, i);
      if (close > 0) {
        const inner = s.slice(i + 1, close);
        if (s[close + 1] === "{") {
          const end = s.indexOf("}", close);
          if (end > 0) {
            const attrs = s.slice(close + 2, end).split(/\s+/);
            const node = (attrs.find((a) => a.startsWith("node=")) || "").slice(5).replace(/["']/g, "");
            flush();
            out.push(attrs.includes(".claim") && node && ctx.claim ? ctx.claim(node, inline(inner, ctx)) : el("span", {}, ...inline(inner, ctx)));
            i = end + 1;
            continue;
          }
        }
        if (s[close + 1] === "(") {
          const end = s.indexOf(")", close);
          if (end > 0) { flush(); out.push(el("span", { class: "ms-link", title: s.slice(close + 2, end) }, ...inline(inner, ctx))); i = end + 1; continue; }
        }
        if (ctx.cite && /@/.test(inner) && !/\[/.test(inner)) {
          const keys = [], locs = {};
          for (const part of inner.split(";")) {
            const m = part.match(new RegExp(`-?@(${KEY})(.*)`));
            if (!m) {  // "[@key, p. 3; p. 9]": a second locator for the same source
              const last = keys[keys.length - 1];
              if (last && part.trim()) locs[last] = [locs[last], part.trim()].filter(Boolean).join("; ");
              continue;
            }
            const key = m[1].replace(/[.,:]+$/, "");
            keys.push(key);
            const loc = (m[1].slice(key.length) + m[2]).replace(/^[\s,，]+/, "").trim();
            if (loc) locs[key] = loc;
          }
          if (keys.length) { flush(); out.push(ctx.cite(keys, locs, false)); i = close + 1; continue; }
        }
      }
    }
    if (ctx.cite && c === "@" && !/[\w.@]/.test(s[i - 1] || "")) {
      const m = s.slice(i).match(new RegExp(`^@(${KEY})`));
      if (m) {
        const key = m[1].replace(/[.,;:]+$/, "");
        flush(); out.push(ctx.cite([key], {}, true)); i += 1 + key.length; continue;
      }
    }
    if ((c === "*" || c === "_") && s[i + 1] === c) {
      const end = s.indexOf(c + c, i + 2);
      if (end > i + 2) { flush(); out.push(el("strong", {}, ...inline(s.slice(i + 2, end), ctx))); i = end + 2; continue; }
    }
    if (c === "~" && s[i + 1] === "~") {
      const end = s.indexOf("~~", i + 2);
      if (end > i + 2) { flush(); out.push(el("del", {}, ...inline(s.slice(i + 2, end), ctx))); i = end + 2; continue; }
    }
    if (c === "*" || (c === "_" && !/\w/.test(s[i - 1] || ""))) {
      const end = s.indexOf(c, i + 1);
      if (end > i + 1 && s[i + 1] !== " " && s[end - 1] !== " " && (c === "*" || !/\w/.test(s[end + 1] || ""))) {
        flush(); out.push(el("em", {}, ...inline(s.slice(i + 1, end), ctx))); i = end + 1; continue;
      }
    }
    text += c;
    i++;
  }
  flush();
  return out;
}

// ------------------------------------------------------------------ tables

const RULE_ROW = /^\s*\|?\s*:?-{1,}:?\s*(\|\s*:?-{1,}:?\s*)*\|?\s*$/;

/** A table row's cells, split on | that is not escaped, in `code` or in $math$. */
function cells(line) {
  let s = line.trim();
  if (s.startsWith("|")) s = s.slice(1);
  if (s.endsWith("|") && !s.endsWith("\\|")) s = s.slice(0, -1);
  const out = [];
  let cur = "", code = false, math = false;
  for (let i = 0; i < s.length; i++) {
    const c = s[i];
    if (c === "\\" && s[i + 1] === "|") { cur += "|"; i++; continue; }
    if (c === "`") code = !code;
    else if (c === "$" && !code) math = !math;
    if (c === "|" && !code && !math) { out.push(cur.trim()); cur = ""; continue; }
    cur += c;
  }
  out.push(cur.trim());
  return out;
}

const isTableStart = (lines, i) => lines[i].includes("|") && i + 1 < lines.length && lines[i + 1].includes("-")
  && RULE_ROW.test(lines[i + 1]) && (lines[i + 1].includes("|") || cells(lines[i]).length > 1);

function table(lines, i, ctx) {
  const head = cells(lines[i]);
  const align = cells(lines[i + 1]).map((c) => (c.startsWith(":") && c.endsWith(":") ? "center" : c.endsWith(":") ? "right" : null));
  const rows = [];
  let j = i + 2;
  for (; j < lines.length && lines[j].trim() && lines[j].includes("|"); j++) rows.push(cells(lines[j]));
  const cell = (tag, text, k) => el(tag, { style: align[k] ? `text-align:${align[k]}` : null }, ...inline(text, ctx));
  const node = el("div", { class: "md-table" }, el("table", {},
    el("thead", {}, el("tr", {}, ...head.map((h, k) => cell("th", h, k)))),
    el("tbody", {}, ...rows.map((r) => el("tr", {}, ...head.map((_, k) => cell("td", r[k] ?? "", k)))))));
  return [node, j];
}

// ------------------------------------------------------------------ blocks

const joinLines = (lines) => lines.reduce((acc, line) => {
  if (!acc) return line.trim();
  const cjk = /[　-鿿，。；：！？）」』]$/.test(acc) || /^[　-鿿（「『]/.test(line.trim());
  return acc + (cjk ? "" : " ") + line.trim();
}, "");

/** Lines of one paragraph: joined as Markdown does, or (ctx.breaks) kept apart with <br>. */
function lines2nodes(lines, ctx) {
  if (!ctx.breaks) return inline(joinLines(lines), ctx);
  return lines.flatMap((l, k) => (k ? [el("br"), ...inline(l.trim(), ctx)] : inline(l.trim(), ctx)));
}

const LIST_ITEM = /^(\s*)([-*+]|\d+[.)])\s+(.*)$/;

/** A list from line i, nested by indentation; returns [node, next line]. */
function list(lines, i, ctx) {
  const first = lines[i].match(LIST_ITEM);
  const indent = first[1].length, ordered = /\d/.test(first[2]);
  const node = el(ordered ? "ol" : "ul", { start: ordered && parseInt(first[2], 10) !== 1 ? parseInt(first[2], 10) : null });
  let j = i;
  while (j < lines.length) {
    const m = lines[j].match(LIST_ITEM);
    if (!m || m[1].length < indent) break;
    if (m[1].length > indent) {  // a nested list belongs to the item before it
      const [sub, next] = list(lines, j, ctx);
      (node.lastElementChild || node.appendChild(el("li"))).append(sub);
      j = next;
      continue;
    }
    const body = [m[3]];
    // continuation lines: indented text that is not itself a list item
    while (j + 1 < lines.length && lines[j + 1].trim() && /^\s+/.test(lines[j + 1]) && !LIST_ITEM.test(lines[j + 1])) body.push(lines[++j]);
    node.append(el("li", {}, ...lines2nodes(body, ctx)));
    j++;
  }
  return [node, j];
}

/** Markdown text as a list of block nodes. */
export function renderMarkdown(text, ctx = {}) {
  const lines = String(text ?? "").replace(/\r\n/g, "\n").split("\n");
  const blocks = [];
  let para = [];
  const endPara = () => { if (para.length) { blocks.push(el("p", {}, ...lines2nodes(para, ctx))); para = []; } };
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i], t = line.trim();
    if (/^(```|~~~)/.test(t)) {
      endPara();
      const fence = t.slice(0, 3), code = [];
      while (++i < lines.length && !lines[i].trim().startsWith(fence)) code.push(lines[i]);
      blocks.push(el("pre", {}, el("code", {}, code.join("\n"))));
      continue;
    }
    if (t.startsWith("$$") || t.startsWith("\\[")) {  // a display formula, on one line or several
      const close = t.startsWith("$$") ? "$$" : "\\]";
      const src = [t.slice(2)];
      let j = i;
      while (!src.join("\n").includes(close) && j + 1 < lines.length && j - i < 60) src.push(lines[++j]);
      const all = src.join("\n"), end = all.indexOf(close);
      if (end >= 0 && !all.slice(end + 2).trim()) {
        endPara();
        blocks.push(mathNode(all.slice(0, end).trim(), true));
        i = j;
        continue;
      }
    }
    if (t.startsWith("<!--")) {
      endPara();
      const note = [line];
      while (!note[note.length - 1].includes("-->") && i + 1 < lines.length) note.push(lines[++i]);
      if (ctx.intent) blocks.push(el("div", { class: "ms-intent" }, note.join(" ").replace(/<!--|-->/g, "").trim()));
      continue;
    }
    if (isTableStart(lines, i)) { endPara(); const [node, next] = table(lines, i, ctx); blocks.push(node); i = next - 1; continue; }
    const h = line.match(/^(#{1,6})\s+(.*)$/);
    if (h) { endPara(); blocks.push(el(`h${Math.min(6, h[1].length + 3)}`, {}, ...inline(h[2].replace(/\s+#+\s*$/, ""), ctx))); continue; }
    if (/^\s*([-*_])(\s*\1){2,}\s*$/.test(line)) { endPara(); blocks.push(el("hr")); continue; }
    if (LIST_ITEM.test(line)) { endPara(); const [node, next] = list(lines, i, ctx); blocks.push(node); i = next - 1; continue; }
    if (/^\s*>/.test(line)) {
      endPara();
      const quote = [];
      for (; i < lines.length && /^\s*>/.test(lines[i]); i++) quote.push(lines[i].replace(/^\s*>\s?/, ""));
      i--;
      blocks.push(el("blockquote", {}, ...renderMarkdown(quote.join("\n"), ctx)));
      continue;
    }
    if (!t) { endPara(); continue; }
    para.push(line);
  }
  endPara();
  return blocks;
}
