# codemirror.js

Single ESM bundle of CodeMirror 6 (the manuscript editor), vendored so the workbench works offline and the
front end keeps no build step. Rebuild:

    npm install @codemirror/state@6.5.2 @codemirror/view@6.38.1 @codemirror/commands@6.8.1 \
      @codemirror/language@6.11.2 @codemirror/lang-markdown@6.3.3 @codemirror/autocomplete@6.18.6 \
      @codemirror/search@6.5.11 @lezer/highlight@1.2.1 esbuild@0.25.8
    cat > entry.js <<'JS'
export { EditorState, EditorSelection, StateEffect, StateField, Compartment, Prec, RangeSetBuilder, Annotation } from "@codemirror/state";
export { EditorView, keymap, placeholder, drawSelection, highlightActiveLine, dropCursor, Decoration, ViewPlugin, MatchDecorator, WidgetType } from "@codemirror/view";
export { defaultKeymap, history, historyKeymap, indentWithTab } from "@codemirror/commands";
export { syntaxHighlighting, HighlightStyle, bracketMatching } from "@codemirror/language";
export { markdown, markdownLanguage } from "@codemirror/lang-markdown";
export { autocompletion, completionKeymap, startCompletion, closeCompletion, acceptCompletion, completionStatus } from "@codemirror/autocomplete";
export { search, searchKeymap, highlightSelectionMatches } from "@codemirror/search";
export { tags } from "@lezer/highlight";
JS
    npx esbuild entry.js --bundle --minify --format=esm --legal-comments=none --outfile=codemirror.js

lang-markdown pulls in the HTML, CSS and JavaScript grammars (for fenced code), which is most of the size.
