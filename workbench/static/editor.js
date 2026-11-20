/* The manuscript editor: CodeMirror 6 set up for Pandoc Markdown with citations and claim anchors.

   It knows the syntax, not the project. writing.js passes in where suggestions come from (the library for
   [@key, the graph for [[claim) and what to do with a passage dropped from the reader. */

import {
  EditorState, EditorView, EditorSelection, Compartment, Prec, Annotation, StateEffect, StateField, keymap, placeholder as placeholderExt, dropCursor,
  Decoration, ViewPlugin, MatchDecorator, defaultKeymap, history, historyKeymap, syntaxHighlighting, HighlightStyle,
  markdown, markdownLanguage, autocompletion, completionKeymap, startCompletion, search, searchKeymap,
  highlightSelectionMatches, tags as t,
} from "/static/vendor/codemirror.js";

/** Set on changes that come from outside (a reload, the other version in a conflict): they are not edits. */
const External = Annotation.define();

/** The drag type the reader uses for a passage: JSON {item_id, citekey, page, quote, excerpt_id?}. */
export const PASSAGE_TYPE = "application/x-onto-passage";

// [@key], [see @a, p. 3; @b] — the bracketed form. In-text @key after a space or at the start of a line.
const CITE = /\[[^[\]\n]*?@[\w㐀-鿿][\w㐀-鿿:.\-/]*[^[\]\n]*\]|(?<![\w@.])@[\w㐀-鿿][\w㐀-鿿:.\-/]*/g;
// [text]{.claim node=n_…}
const CLAIM = /\[([^\]\n]+)\](\{\.claim node=[\w-]+\})/g;

/* Problems from the citation check, marked in the text. They move with edits until the next check replaces them. */
const setProblems = StateEffect.define();
const problemField = StateField.define({
  create: () => Decoration.none,
  update(deco, tr) {
    deco = deco.map(tr.changes);
    for (const e of tr.effects) if (e.is(setProblems)) deco = e.value;
    return deco;
  },
  provide: (f) => EditorView.decorations.from(f),
});

const theme = EditorView.theme({
  "&": { color: "var(--text)", backgroundColor: "rgba(255, 255, 255, .04)", border: "1px solid var(--line)", borderRadius: "9px", transition: "border-color .2s" },
  "&.cm-focused": { outline: "none", borderColor: "rgba(247, 147, 26, .5)" },
  ".cm-scroller": { fontFamily: "var(--mono)", fontSize: "13.5px", lineHeight: "1.75", overflow: "visible" },
  ".cm-content": { padding: "8px 0", minHeight: "124px", caretColor: "var(--accent)" },
  ".cm-line": { padding: "0 11px" },
  ".cm-cursor, .cm-dropCursor": { borderLeftColor: "var(--accent)", borderLeftWidth: "1.5px" },
  "&.cm-focused .cm-selectionBackground, .cm-selectionBackground, .cm-content ::selection": { backgroundColor: "rgba(247, 147, 26, .22) !important" },
  ".cm-placeholder": { color: "var(--text-3)" },
  ".cm-cite": { color: "#7dd3fc", backgroundColor: "rgba(76, 201, 240, .1)", borderRadius: "4px", padding: "0 1px" },
  ".cm-claim": { borderBottom: "1.5px solid rgba(182, 156, 255, .7)" },
  ".cm-claim-attr": { color: "var(--text-3)", fontSize: "11.5px" },
  ".cm-problem-error": { textDecoration: "underline wavy rgba(255, 107, 107, .9)", textDecorationSkipInk: "none", textUnderlineOffset: "3px" },
  ".cm-problem-warning": { textDecoration: "underline wavy rgba(247, 190, 26, .85)", textDecorationSkipInk: "none", textUnderlineOffset: "3px" },
  ".cm-problem-info": { textDecoration: "underline dotted rgba(125, 211, 252, .7)", textUnderlineOffset: "3px" },
  ".cm-selectionMatch": { backgroundColor: "rgba(255, 255, 255, .07)" },
  ".cm-searchMatch": { backgroundColor: "rgba(247, 147, 26, .25)" },
  ".cm-panels": { backgroundColor: "rgba(12, 14, 20, .97)", color: "var(--text)", borderRadius: "0 0 9px 9px" },
  ".cm-panels.cm-panels-bottom": { borderTop: "1px solid var(--line)" },
  ".cm-panel.cm-search": { fontFamily: "var(--sans)", fontSize: "12px", padding: "6px 8px" },
  ".cm-panel.cm-search input, .cm-panel.cm-search button": { fontSize: "12px" },
  ".cm-textfield": { background: "rgba(255, 255, 255, .05)", border: "1px solid var(--line-2)", borderRadius: "6px", color: "var(--text)" },
  ".cm-button": { background: "rgba(255, 255, 255, .06)", backgroundImage: "none", border: "1px solid var(--line-2)", borderRadius: "6px", color: "var(--text-2)" },
  ".cm-tooltip": { border: "1px solid var(--line)", backgroundColor: "rgba(12, 14, 20, .97)", borderRadius: "12px", boxShadow: "0 24px 50px -20px rgba(0, 0, 0, .8)" },
  ".cm-tooltip.cm-tooltip-autocomplete > ul": { fontFamily: "var(--sans)", maxHeight: "300px", minWidth: "280px", maxWidth: "min(560px, 80vw)", padding: "6px" },
  ".cm-tooltip.cm-tooltip-autocomplete > ul > li": { padding: "6px 8px", borderRadius: "8px", color: "var(--text-2)", fontSize: "13px", lineHeight: "1.4", display: "flex", gap: "8px", alignItems: "baseline" },
  ".cm-tooltip.cm-tooltip-autocomplete > ul > li[aria-selected]": { backgroundColor: "rgba(255, 255, 255, .08)", color: "var(--text)" },
  ".cm-completionLabel": { flex: "1", minWidth: "0", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" },
  ".cm-completionDetail": { fontStyle: "normal", color: "var(--text-3)", font: "500 10.5px/1 var(--mono)", whiteSpace: "nowrap", marginLeft: "0" },
  ".cm-completionMatchedText": { textDecoration: "none", color: "var(--accent)" },
  ".cm-completionInfo": { padding: "8px 10px", maxWidth: "320px", fontSize: "12px", color: "var(--text-2)", lineHeight: "1.6" },
}, { dark: true });

const highlight = HighlightStyle.define([
  { tag: t.heading, color: "var(--text)", fontWeight: "600" },
  { tag: t.strong, fontWeight: "650", color: "var(--text)" },
  { tag: t.emphasis, fontStyle: "italic" },
  { tag: t.strikethrough, textDecoration: "line-through" },
  { tag: t.link, color: "#7dd3fc" },
  { tag: t.url, color: "var(--text-3)" },
  { tag: t.monospace, color: "#d6c25a" },
  { tag: t.quote, color: "var(--text-2)", fontStyle: "italic" },
  { tag: [t.processingInstruction, t.meta], color: "var(--text-3)" },
  { tag: t.contentSeparator, color: "var(--text-3)" },
  { tag: t.list, color: "var(--accent)" },
  { tag: t.comment, color: "var(--text-3)", fontStyle: "italic" },
]);

function decorations(citeTitle) {
  const cites = new MatchDecorator({
    regexp: CITE,
    decoration: (m) => {
      const keys = [...m[0].matchAll(/@([\w㐀-鿿][\w㐀-鿿:.\-/]*)/g)].map((k) => k[1].replace(/[.:]+$/, ""));
      const title = citeTitle ? keys.map(citeTitle).filter(Boolean).join("\n") : "";
      return Decoration.mark({ class: "cm-cite", attributes: title ? { title } : {} });
    },
  });
  const claims = new MatchDecorator({
    regexp: CLAIM,
    decorate: (add, from, to, m) => {
      const attrFrom = to - m[2].length;
      add(from, attrFrom, Decoration.mark({ class: "cm-claim" }));
      add(attrFrom, to, Decoration.mark({ class: "cm-claim-attr" }));
    },
  });
  const plugin = (matcher) => ViewPlugin.define((view) => ({
    decorations: matcher.createDeco(view),
    update(u) { this.decorations = matcher.updateDeco(u, this.decorations); },
  }), { decorations: (v) => v.decorations });
  return [plugin(cites), plugin(claims)];
}

/* Completion: "[@" or "; @" asks the library, "[[" asks the graph. Both sources do their own matching (a cite
   query also matches titles and authors), so CodeMirror's own filter is off. */
function sources({ cite, claim }) {
  const citeSource = async (ctx) => {
    const m = ctx.matchBefore(/(?:\[|;\s*|^|\s)@[\w㐀-鿿:.\-/]*$/);
    if (!m) return null;
    const at = m.from + m.text.indexOf("@");
    const query = ctx.state.sliceDoc(at + 1, ctx.pos);
    const opened = /^\[/.test(m.text) || /^;/.test(m.text.trimStart());
    const items = await cite(query);
    if (ctx.aborted) return null;
    return {
      from: at + 1, filter: false,
      options: items.map((it) => ({
        label: it.key, displayLabel: it.title, detail: it.hint, info: it.info,
        apply: (view, _c, from, to) => {
          const close = opened && view.state.sliceDoc(to, to + 1) !== "]" && /^\[/.test(m.text);
          const ins = it.key + (close ? "]" : "");
          view.dispatch({ changes: { from, to, insert: ins }, selection: { anchor: from + ins.length }, userEvent: "input.complete" });
        },
      })),
    };
  };
  const claimSource = async (ctx) => {
    const m = ctx.matchBefore(/\[\[[^\]\n]{0,240}$/);
    if (!m) return null;
    const query = m.text.slice(2);
    const items = await claim(query);
    if (ctx.aborted) return null;
    return {
      from: m.from, filter: false,
      options: items.map((it) => ({
        label: `[[${query}`, displayLabel: it.label, detail: it.hint,
        apply: (view, _c, from, to) => {
          const text = query.trim() || it.label;
          const ins = `[${text}]{.claim node=${it.value}}`;
          view.dispatch({ changes: { from, to, insert: ins }, selection: { anchor: from + ins.length }, userEvent: "input.complete" });
        },
      })),
    };
  };
  return [claimSource, citeSource];
}

/**
 * opts: doc, label (aria), placeholder, onChange(text), onFocus(), onBlur(), onSelect(text),
 *       onSave(), complete: {cite(query) → [{key, title, hint, info}], claim(query) → [{value, label, hint}]},
 *       citeTitle(key) → hover text, onPassage(passage, pos) → text to insert (or a promise of it),
 *       actions: [{label, title, run({from, to, text})}] shown in a small bar over a selection
 */
export function createEditor(opts) {
  const deco = new Compartment();
  const parent = document.createElement("div");
  parent.className = "ms-editor";
  const listener = EditorView.updateListener.of((u) => {
    if (u.docChanged && !u.transactions.some((tr) => tr.annotation(External))) opts.onChange?.(u.state.doc.toString());
    if (u.focusChanged) (u.view.hasFocus ? opts.onFocus : opts.onBlur)?.();
    if (u.selectionSet && !u.docChanged) {
      const r = u.state.selection.main;
      if (!r.empty) opts.onSelect?.(u.state.sliceDoc(r.from, r.to));
    }
    // Only a selection the person made opens the bar; going to a problem selects text too, and should not.
    if (u.selectionSet && !u.docChanged && u.transactions.some((tr) => tr.isUserEvent("select"))) placeBar(u.view);
    else if (u.selectionSet || u.focusChanged || u.docChanged) hideBar();
  });

  // The selection bar: commands on the selected text, above its start. Its buttons do not take focus.
  let bar = null, barTimer = null;
  function hideBar() { clearTimeout(barTimer); if (bar) bar.hidden = true; }
  function placeBar(v) {
    clearTimeout(barTimer);
    const r = v.state.selection.main;
    if (!opts.actions?.length || r.empty || !v.hasFocus || r.to - r.from < 2) { hideBar(); return; }
    barTimer = setTimeout(() => v.requestMeasure({
      read: () => v.coordsAtPos(r.from),
      write: (c) => {
        if (!c || !v.hasFocus) return;
        if (!bar) {
          bar = document.createElement("div");
          bar.className = "ms-selbar";
          bar.setAttribute("role", "toolbar");
          bar.setAttribute("aria-label", "选中文字");
          bar.addEventListener("mousedown", (e) => e.preventDefault());
          for (const a of opts.actions) {
            const b = document.createElement("button");
            b.type = "button"; b.textContent = a.label; if (a.title) b.title = a.title;
            b.addEventListener("click", () => { const sel = api.selection(); hideBar(); a.run(sel); });
            bar.append(b);
          }
          document.body.append(bar);
        }
        bar.hidden = false;
        const w = bar.offsetWidth || 260;
        bar.style.left = `${Math.max(8, Math.min(c.left, innerWidth - w - 8))}px`;
        bar.style.top = `${c.top > 48 ? c.top - 38 : c.bottom + 8}px`;
      },
    }), 220);
  }
  const onScroll = () => hideBar();
  addEventListener("scroll", onScroll, true);
  const drops = EditorView.domEventHandlers({
    dragover(e) {
      if (e.dataTransfer?.types.includes(PASSAGE_TYPE)) { e.preventDefault(); e.dataTransfer.dropEffect = "copy"; }
      return false;
    },
    drop(e, view) {
      const raw = e.dataTransfer?.getData(PASSAGE_TYPE);
      if (!raw || !opts.onPassage) return false;
      e.preventDefault();
      let passage;
      try { passage = JSON.parse(raw); } catch { return true; }
      const pos = view.posAtCoords({ x: e.clientX, y: e.clientY }) ?? view.state.selection.main.head;
      api.insertPassage(passage, pos);
      return true;
    },
  });
  const view = new EditorView({
    parent,
    state: EditorState.create({
      doc: opts.doc || "",
      extensions: [
        history(),
        dropCursor(),
        EditorView.lineWrapping,
        markdown({ base: markdownLanguage }),
        syntaxHighlighting(highlight),
        highlightSelectionMatches(),
        search({ top: false }),
        autocompletion({ override: sources(opts.complete), icons: false, activateOnTyping: true, closeOnBlur: true, maxRenderedOptions: 40 }),
        Prec.highest(keymap.of([{ key: "Mod-s", preventDefault: true, run: () => { opts.onSave?.(); return true; } }])),
        keymap.of([...completionKeymap, ...searchKeymap, ...historyKeymap, ...defaultKeymap]),
        opts.placeholder ? placeholderExt(opts.placeholder) : [],
        deco.of(decorations(opts.citeTitle)),
        problemField,
        EditorView.contentAttributes.of({ "aria-label": opts.label || "正文", "aria-multiline": "true", spellcheck: "false" }),
        theme, listener, drops,
      ],
    }),
  });

  const api = {
    view,
    dom: parent,
    get value() { return view.state.doc.toString(); },
    /** Replace everything without counting it as an edit (keeps the caret where it can). */
    setValue(text) {
      if (text === api.value) return;
      const head = Math.min(view.state.selection.main.head, text.length);
      view.dispatch({ changes: { from: 0, to: view.state.doc.length, insert: text }, selection: { anchor: head }, annotations: External.of(true) });
    },
    focus() { view.focus(); },
    hasFocus: () => view.hasFocus,
    selection() { const r = view.state.selection.main; return { from: r.from, to: r.to, text: view.state.sliceDoc(r.from, r.to) }; },
    /** Insert text at `pos` (default: the caret), select or place the caret after it, and focus. */
    insert(text, pos = view.state.selection.main.head, { to = pos, select = false } = {}) {
      view.dispatch({ changes: { from: pos, to, insert: text },
        selection: select ? { anchor: pos, head: pos + text.length } : { anchor: pos + text.length }, userEvent: "input", scrollIntoView: true });
      view.focus();
    },
    /** Type "[@" at the caret and open the citation list. */
    startCite() {
      const r = view.state.selection.main;
      view.dispatch({ changes: { from: r.from, to: r.to, insert: "[@" }, selection: { anchor: r.from + 2 }, userEvent: "input" });
      view.focus();
      startCompletion(view);
    },
    /** Wrap the selection in "[[" so the claim list opens on it. */
    startClaim() {
      const r = view.state.selection.main;
      if (r.empty) return false;
      const chosen = view.state.sliceDoc(r.from, r.to).replace(/[[\]]/g, "");
      view.dispatch({ changes: { from: r.from, to: r.to, insert: `[[${chosen}` }, selection: { anchor: r.from + chosen.length + 2 }, userEvent: "input" });
      view.focus();
      startCompletion(view);
      return true;
    },
    async insertPassage(passage, pos = view.state.selection.main.head) {
      const text = await opts.onPassage(passage, pos);
      if (!text) return;
      const at = Math.min(pos, view.state.doc.length);
      // Keep words apart: a space before, unless the drop lands at a line start or after a space.
      const before = view.state.sliceDoc(Math.max(0, at - 1), at);
      api.insert((before && !/\s/.test(before) ? " " : "") + text, at);
    },
    /** Recompute citation hover text after the references changed. */
    redecorate(citeTitle = opts.citeTitle) { opts.citeTitle = citeTitle; view.dispatch({ effects: deco.reconfigure(decorations(citeTitle)) }); },
    /** Mark problems: [{from, to, level, message}]. Replaces the previous marks. */
    setProblems(list) {
      const len = view.state.doc.length;
      const ranges = list.filter((p) => p.from != null && p.to > p.from && p.to <= len)
        .sort((a, b) => a.from - b.from || a.to - b.to)
        .map((p) => Decoration.mark({ class: `cm-problem-${p.level}`, attributes: { title: p.message } }).range(p.from, p.to));
      view.dispatch({ effects: setProblems.of(Decoration.set(ranges, true)) });
    },
    /** Select a range, scroll it to the middle and flash it. */
    reveal(from, to = from) {
      const len = view.state.doc.length;
      from = Math.max(0, Math.min(from, len)); to = Math.max(from, Math.min(to, len));
      view.dispatch({ selection: EditorSelection.range(from, to), effects: EditorView.scrollIntoView(from, { y: "center" }) });
      view.focus();
    },
    destroy() { hideBar(); bar?.remove(); removeEventListener("scroll", onScroll, true); view.destroy(); },
  };
  return api;
}
