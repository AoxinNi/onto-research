# Research workbench: a VS Code–style shell for professional writing

One shell that stays the same (sidebar, editor tabs and splits, problems, command palette), with domain packs
plugged into it: academic research first, then financial reports, play scripts and the like. What sets it apart
is the chain behind each sentence: **draft sentence → claim → evidence excerpt → exact place in the source**,
which the agent and the checks work along.

This builds on what the project already has: the library, MinerU 精读 and 原文, the claim and evidence graph,
groundings with review, manuscripts, the MCP agent and pandoc export. See `research-writing.md` for those.

## 1. Workbench layout

```
┌──┬──────────────┬───────────────────────────────┬──────────────┐
│◉ │ [paper tab] [manuscript tab] …                │ AGENT        │
│▤ │ ┌───────────────────┬───────────────────────┐ │  chat        │
│✎ │ │ 原文 / 精读        │ draft section          │ │  suggestions │
│◫ │ │ (reading)          │ (writing)              │ │  (as diffs)  │
│⌕ │ └───────────────────┴───────────────────────┘ │              │
├──┴──────────────────────────────────────────────┴──────────────┤
│ ● connected · view · 24 nodes · 3 to review   《paper》 words · style · model │
└─────────────────────────────────────────────────────────────────┘
```

| VS Code | Here |
| --- | --- |
| Workspace folder | A project (one paper, one report, one script) |
| Activity bar | 图谱 · 文献 · 写作 · 对照 · quick open · commands · agent · projects · settings |
| Editors, tabs, splits | Papers, notes and manuscripts as tabs; the 对照 view puts the paper next to the draft |
| Problems panel | Results from the checks (citations, claims without a source, quotes and numbers that don't match the source) |
| Command palette / quick open | `⇧⌘P` / `⌘P` |
| Language server | The pack's checker |
| Extensions | Packs |
| Source control | Snapshots and history, optional git |
| Copilot | The agent; edits arrive as suggestions, a person accepts each one |

## 2. The academic pack along the workflow

| Stage | Built on | New |
| --- | --- | --- |
| Collect | library, resolve, bibio, xsearch | Zotero sync, browser "send to app" |
| Read | MinerU, paper_layout, excerpts | highlight layer, figure and table cards |
| Think | graph, groundings | literature matrix (papers × questions) |
| Write | manuscripts, cite, KaTeX | CodeMirror 6 editor, drag an excerpt into the draft |
| Check | citation check | continuous checker and a Problems panel |
| Ask | agent, MCP tools | selection commands, suggestion queue |
| Submit | pandoc export | journal templates, CSL styles, pre-submission checklist |

## 3. Decisions

| # | Question | Recommendation | Status |
| --- | --- | --- | --- |
| W1 | Where the user's work lives | An embedded PostgreSQL 16 (pgserver) in the app's data folder is the working copy; snapshots write the project to a folder as Markdown, BibTeX, CSL-JSON and a re-importable JSON, optionally committed with git. SQLite was dropped: the server has ~330 SQL statements and 14 migrations built on jsonb, ON CONFLICT, row locks, advisory locks and per-project schemas | done in P3 |
| W2 | Front-end stack | CodeMirror 6 as a vendored ESM bundle (`vendor/codemirror.js`, like `graph3d.js`); the app itself keeps plain ES modules and no build step. Revisit Vite + TypeScript when the front end outgrows that | done in P1 |
| W3 | Editor style | Markdown with rich inline rendering, not WYSIWYG | proposed |
| W4 | Local or cloud | Local-first, cloud optional | the direction of `feature/local-first` |
| W5 | Pack API | Declarative manifests in v1, code packs later | proposed |

## 4. Phases

| Phase | Ships | Done when |
| --- | --- | --- |
| **P0 Shell** | Activity bar, tabs, 对照 split, command palette, quick open, status bar; existing panes rehosted | One window for the whole paper, no reloads |
| P1 Write with sources | CodeMirror manuscript editor, citation completion, drag an excerpt from the reader into the draft | A section written from 3 papers, every citation linked to a page |
| P2 Checks + agent | Problems panel, agent suggestions as diffs, selection commands | A planted wrong citation and a wrong number are caught |
| P3 Local projects | Embedded Postgres, project folder snapshots, bundled Python sidecar | Runs offline on a clean Mac, no Docker |
| P4 Matrix + submit | Literature matrix, journal templates, CSL styles, checklist | An export a journal would accept |
| P5 Second pack | Finance report pack | The pack design holds for a second domain |

## 5. Risks

- Scope: copy VS Code's structure, not its features (no extension host, terminal or debugger).
- Habits: DOCX and LaTeX round-trips and Zotero import decide whether people switch.
- PDF fidelity: MinerU is good but imperfect and needs a key; 原文 always keeps the plain text layer.
- Trust: one wrong check undermines all of them. Ship checks conservatively and say what each could not verify.

## P0 implementation record

- `workbench/static/shell.js`: `createShell()` gives commands (with ⌘ chords handled in one capture listener),
  the palette (quick open; `>` switches to commands; fuzzy matching with highlighted hits; recent commands
  first), open-editor tabs (persisted per project in `localStorage`, at most 12, closed when their target is
  archived), the activity bar and the status bar.
- `app.js` registers about 30 commands, the rail, four quick-open sources (papers via `/api/library?q=`, notes,
  manuscripts plus the open manuscript's sections, graph nodes) and the status entries. The status bar reads the
  existing DOM (`#conn`, `#stat-*`, `#review-*`, `#model-name`) through a `MutationObserver`, so the older code
  did not need to change.
- A fourth view, `split` (key `4`, rail 对照): the library on the left and writing on the right, each without its
  side list; tabs and quick open choose what each side shows. On screens ≤ 820px it falls back to the library.
  In the split view the agent gets both contexts (the paper and the section).
- `library.js` and `writing.js` report what they open (`onOpen`) and what disappears (`onGone`), and expose
  `openNote`, `start`, `openManuscript`, `newManuscript`, `export` and `current` for commands and tabs.
- Layout: new CSS variables `--left`, `--bottom` and `--right` replace the hard-coded gutters, so the rail and the
  status bar get room. Phones hide the rail, the tabs and the status bar and keep the old 图谱 / 文献 / 写作 switch.
- Kept as before: the single keys 1–3 and `R` `L` `T` `U` `\` `/`, `⌘K` (now a command that also leaves other views
  for the graph), the review pill and the agent panel.
- Not in P0: splitting arbitrary editors, dragging tabs, a Problems panel (P2), and dragging excerpts into the
  draft (P1).

## P1 implementation record

- `vendor/codemirror.js`: CodeMirror 6 (state, view, commands, language, lang-markdown, autocomplete, search) as
  one 549 KB ESM bundle; recipe in `vendor/build-codemirror.md`, licenses in `vendor/LICENSES.txt`. Importing it
  cold takes about 140 ms; a 200-line editor builds in about 70 ms.
- `editor.js`: `createEditor()` sets CodeMirror up for Pandoc Markdown: soft wrap, undo history, `⌘F` search,
  `⌘S` save, Markdown highlighting, citations (`[@key, p. 3]`, in-text `@key`) and claim anchors
  (`[text]{.claim node=…}`) marked inline with the reference as hover text. Completion: `[@` (or `; @`) asks the
  library, matching cite keys, titles and authors, with the abstract alongside; `[[` asks the graph. Dropping a
  passage (`application/x-onto-passage`) inserts it at the drop point.
- `writing.js`: each section's textarea is now an editor; the old hand-made completion box is gone. Saving,
  conflicts, reloads, suggestions and the preview work as before. `insertPassage()` puts a passage where the
  person last typed: a quote with `[@key, p. N]` (a block quote past 220 characters), and the passage is saved as
  an excerpt so the quote can be traced. Clicking a citation with a page opens 原文 at that page (`showPage`, with
  a flash); in the split view the paper opens beside the draft.
- `library.js`: the selection bar in 原文 leads with 引用到草稿; a selection can be dragged straight into a
  section; excerpts can be cited from the 摘录 tab or dragged. Citing from the library view opens the split view
  (the writing view on phones) so the insertion is visible.
- Kept: `[[` anchoring through the 锚定论断 button, the 引用 button (now types `[@` and opens the list), the
  preview, suggestion diffs, and save on idle (1.5 s) or `⌘S`.
- Not in P1: highlights in the reader for passages already cited, and dragging from 精读.

## P2 implementation record

- `ontology/cite.py`: `quotes()` finds quoted passages followed by a citation (`“…”[@key, p. 3]`, `"…" [@key]`,
  `「…」`, and block quotes whose last line or the line after is a citation); `numbers()` finds numbers in sentences
  that cite something, outside the citation, links, headings, list markers and quotes. Years and integers below 10
  are left out on purpose.
- `Manuscripts.check()` now also checks against the cited sources' extracted text (`_check_sources`):
  `quote_mismatch` (warning, the quote is in none of the cited sources), `quote_page` (warning, it is there but
  on another page than the citation says), `number_mismatch` (warning, no cited source has that value; 23.5% also
  matches 0.235, and 23% matches 23.4%), and `unchecked_source` (info, a cited source has no text, so nothing in
  that section could be checked against it). Every issue now carries `start`, `end` and `snippet`.
- `editor.js`: problems are wavy underlines with the message as hover text (a StateField of decorations that move
  with edits until the next check, so the vendored bundle did not need `@codemirror/lint`); `reveal()` selects and
  centres a range; a selection bar (改写 · 找支持 · 找反例 · 核对 · 问 Agent…) appears over text the person selects,
  but not over text a jump to a problem selects.
- `problems.js`: the Problems panel at the bottom of the work area. Issues grouped by section in reading order,
  filters per level, ↻ re-check. The panel's open state and filters are kept per project. `⇧⌘M` toggles it,
  `F8` / `⇧F8` walk the problems, and the status bar shows `⊗ errors ⚠ warnings`. The shell now also handles
  function-key commands.
- Selection commands are palette commands too (`选中文字` group). 改写 asks the agent for a `suggest_edit`, so
  the rewrite arrives as a suggestion with a diff that the person accepts or rejects (the P1 flow); the others
  answer in the chat. 问 Agent… only fills the chat box.
- Not in P2: checking a quote against a page range (`pp. 3–5` uses the first page), numbers written out as words,
  and paraphrase detection. Each would add false alarms, and those cost trust (see Risks).

## P3 implementation record

- `ontology/localdb.py`: when `DATABASE_URL` is not set, `db.database_url()` starts an embedded PostgreSQL 16 from
  the `pgserver` wheel (`pip install onto-research[local]`, about 33 MB) with its data in the app's data folder
  (`~/Library/Application Support/onto-research/pg` on macOS; `ONTO_DATA_DIR` moves it, `ONTO_LOCAL_DB=0` turns it
  off). It listens on a Unix socket and is shared by every local process. The last process to let go stops it;
  `localdb.stop()` also runs on SIGTERM/SIGINT (uvicorn re-raises the signal, which skips exit handlers) and first
  drops handles of processes that died. The first start (initdb) takes about 15 s, later ones about 0.6 s. The
  whole test suite (201 tests) passes on it as well as on the Docker Postgres.
- `ontology/snapshot.py` with `GET/POST /api/projects/{id}/snapshot`: writes `project.onto.json` (re-importable),
  `references.bib`, `references.json`, `manuscripts/*.md` (Pandoc Markdown with YAML, `bibliography:
  ../references.bib`), `notes/*.md` and `snapshot.json`. Unchanged files are not rewritten; manuscripts and notes
  that are gone are removed, and other files in the folder are left alone. With git (on request, or by default
  once the folder is a repository) it commits, as `onto-research` only when no git identity is set up. The folder
  comes from `ONTO_PROJECTS_DIR`, never from a request. Snapshots are on only for local runs.
- Front end: the palette commands 保存快照到项目文件夹 and 保存快照并用 git 提交, and a status entry
  (`快照 3 分钟前 · 41b24ad`).
- Desktop: `src-tauri/src/sidecar.rs` starts the bundled server when the app points at the default local
  address, nothing answers there, and `onto-workbench` sits next to the app's executable. `session_boot` waits
  for it (up to 90 s), and quitting the app stops it with SIGTERM. `desktop/sidecar/` freezes the server and
  Postgres into one 39 MB executable with PyInstaller (`npm run sidecar`; `npm run build:local` bundles it through
  `tauri.local.conf.json`, so ordinary builds and `cargo test` do not need it). The spec points PyInstaller's single
  `libpq.5.dylib` at psycopg's copy: pgserver ships an older one, and the bundled app failed until this was fixed.
- Verified on this Mac (arm64): the frozen binary with an empty environment and an empty data folder starts,
  migrates, serves, keeps its data across restarts, takes git snapshots, and stops Postgres on SIGTERM. Also
  `cargo test` (24 tests), clippy and rustfmt.
- **Not yet verified:** a full `tauri build` with the sidecar inside the .app; a Mac with nothing installed; code
  signing and notarization of the nested Postgres binaries (they will need signing with the app); Intel Macs,
  Windows and Linux (pgserver has wheels for them, and the libpq workaround is macOS-specific). Attached PDFs are
  not copied into snapshots.
