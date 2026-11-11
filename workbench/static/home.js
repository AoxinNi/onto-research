/* Project list and MCP page. Each card draws a small constellation of its project's busiest nodes. */
import { typeOf } from "/static/vocab.js";
import { confirmAction, currentView, download, go, request, routes } from "/static/net.js";

const $ = (sel) => document.querySelector(sel);
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
const svg = (markup) => { const t = document.createElement("template"); t.innerHTML = markup.trim(); return t.content.firstChild; };
const REDUCED = matchMedia("(prefers-reduced-motion: reduce)").matches;
let cloudWorkspace = false;

function ago(iso) {
  if (!iso) return "—";
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  if (s < 90) return "刚刚";
  if (s < 3600) return `${Math.round(s / 60)} 分钟前`;
  if (s < 86400 * 2) return `${Math.round(s / 3600)} 小时前`;
  return `${Math.round(s / 86400)} 天前`;
}

function toast(text, bad = false) {
  const t = el("div", { class: `toast${bad ? " bad" : ""}` }, text);
  $("#toasts").append(t);
  setTimeout(() => { t.classList.add("out"); t.addEventListener("animationend", () => t.remove()); }, bad ? 4200 : 2400);
}

async function api(url, opts = {}) {
  const res = await request(url, { headers: { "Content-Type": "application/json" }, ...opts });
  if (res.status === 401) go(routes.login, { replace: true });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.detail || `请求失败（${res.status}）`);
  return body;
}

// ------------------------------------------------------------------ routing

function show(view, push = false) {
  for (const v of document.querySelectorAll(".view")) v.hidden = v.id !== `view-${view}`;
  for (const t of document.querySelectorAll(".tab")) t.setAttribute("aria-selected", String(t.dataset.view === view));
  document.title = `${{ mcp: "MCP 接入", sources: "数据源", settings: "平台设置" }[view] || "本体项目"} · 本体工作台`;
  if (push) history.pushState({ view }, "", routes.view(view));
  if (view === "mcp") loadMcp();
  if (view === "sources") loadSources();
  if (view === "settings") loadModelSettings();
}
for (const t of document.querySelectorAll(".tab")) {
  t.addEventListener("click", (e) => { e.preventDefault(); show(t.dataset.view, true); });
}
const viewFromPath = currentView;
addEventListener("popstate", () => show(viewFromPath()));

// ------------------------------------------------------------------ constellation

// A tiny deterministic force layout: the same project always draws the same sky.
function layout(sample, w, h) {
  const n = sample.nodes.length;
  const pts = sample.nodes.map((_, i) => {
    const a = i * 2.39996, r = 0.18 + 0.32 * Math.sqrt((i + 0.5) / Math.max(n, 1));
    return { x: 0.5 + r * Math.cos(a), y: 0.5 + r * Math.sin(a) * 0.62, vx: 0, vy: 0 };
  });
  const k = 0.55 / Math.sqrt(Math.max(n, 1));
  for (let it = 0; it < 140; it++) {
    for (let i = 0; i < n; i++) for (let j = i + 1; j < n; j++) {
      const a = pts[i], b = pts[j];
      let dx = a.x - b.x, dy = (a.y - b.y) * 1.6;
      const d2 = dx * dx + dy * dy + 1e-4, f = (k * k) / d2 * 0.012;
      a.vx += dx * f; a.vy += dy * f; b.vx -= dx * f; b.vy -= dy * f;
    }
    for (const [s, t] of sample.links) {
      const a = pts[s], b = pts[t];
      const dx = b.x - a.x, dy = b.y - a.y, d = Math.hypot(dx, dy) + 1e-4, f = (d - k * 1.4) * 0.04;
      a.vx += dx / d * f; a.vy += dy / d * f; b.vx -= dx / d * f; b.vy -= dy / d * f;
    }
    for (const p of pts) {
      p.vx += (0.5 - p.x) * 0.004; p.vy += (0.5 - p.y) * 0.006;
      p.x += p.vx; p.y += p.vy; p.vx *= 0.6; p.vy *= 0.6;
    }
  }
  const xs = pts.map((p) => p.x), ys = pts.map((p) => p.y);
  const [x0, x1, y0, y1] = [Math.min(...xs), Math.max(...xs), Math.min(...ys), Math.max(...ys)];
  const pad = 22, sx = (w - pad * 2) / Math.max(x1 - x0, 0.08), sy = (h - pad * 2) / Math.max(y1 - y0, 0.08), s = Math.min(sx, sy);
  const ox = (w - (x1 - x0) * s) / 2, oy = (h - (y1 - y0) * s) / 2;
  return pts.map((p) => ({ x: ox + (p.x - x0) * s, y: oy + (p.y - y0) * s }));
}

function constellation(sample, id) {
  const w = 360, h = 148;
  if (!sample.nodes.length) return el("div", { class: "empty" }, "EMPTY · 等你来搭");
  const pts = layout(sample, w, h);
  const maxDeg = Math.max(1, ...sample.nodes.map((n) => n[1]));
  const lines = sample.links.map(([s, t]) => {
    const c = typeOf(sample.nodes[s][0]).color;
    return `<line x1="${pts[s].x.toFixed(1)}" y1="${pts[s].y.toFixed(1)}" x2="${pts[t].x.toFixed(1)}" y2="${pts[t].y.toFixed(1)}" stroke="${c}" stroke-opacity=".28" stroke-width=".8"/>`;
  }).join("");
  const orbs = sample.nodes.map(([type, deg], i) => {
    const c = typeOf(type).color, r = 1.8 + 3.4 * Math.sqrt(deg / maxDeg);
    return `<circle cx="${pts[i].x.toFixed(1)}" cy="${pts[i].y.toFixed(1)}" r="${(r * 3.2).toFixed(1)}" fill="url(#halo-${id})" style="color:${c}" opacity=".5"/>`
      + `<circle class="orb" cx="${pts[i].x.toFixed(1)}" cy="${pts[i].y.toFixed(1)}" r="${r.toFixed(1)}" fill="${c}"/>`;
  }).join("");
  return svg(`<svg viewBox="0 0 ${w} ${h}" preserveAspectRatio="xMidYMid meet" aria-hidden="true">
    <defs><radialGradient id="halo-${id}"><stop offset="0" stop-color="currentColor" stop-opacity=".55"/><stop offset="1" stop-color="currentColor" stop-opacity="0"/></radialGradient></defs>
    ${lines}${orbs}</svg>`);
}

// ------------------------------------------------------------------ projects

let projects = [];
let openMenu = null;
function closeMenu() { openMenu?.menu.remove(); openMenu?.btn.setAttribute("aria-expanded", "false"); openMenu = null; }
document.addEventListener("pointerdown", (e) => { if (openMenu && !e.target.closest(".menu, .menu-btn")) closeMenu(); });
addEventListener("keydown", (e) => { if (e.key === "Escape") closeMenu(); });

async function loadProjects() {
  try {
    projects = await api("/api/projects");
    renderProjects();
    $("#nav-status").textContent = `${projects.filter((p) => p.status === "active").length} 个项目 · 数据采集运行中`;
  } catch (e) {
    $("#project-groups").replaceChildren(el("p", { class: "lede" }, `项目读取失败：${e.message}`));
  }
}

function renderTotals(active) {
  const sum = (k) => active.reduce((s, p) => s + (p.stats?.[k] || 0), 0);
  const items = [["项目", active.length], ["节点", sum("nodes")], ["关系", sum("links")], ["数据流", sum("feeds")],
    ["近 7 天新闻", sum("news_7d")], ["待审核", sum("pending"), sum("pending") > 0]];
  $("#totals").replaceChildren(...items.map(([k, v, warn]) =>
    el("div", { class: `total${warn ? " warn" : ""}` }, el("b", {}, v.toLocaleString("en-US")), el("span", {}, k))));
}

function renderProjects() {
  const active = projects.filter((p) => p.status === "active");
  const archived = projects.filter((p) => p.status === "archived");
  renderTotals(active);
  const groups = [el("h2", { class: "group-title" }, "进行中", el("small", {}, String(active.length))),
    el("div", { class: "grid" }, ...active.map(card))];
  if (archived.length) {
    groups.push(el("details", { class: "archived" },
      el("summary", {}, el("h2", { class: "group-title" }, "已归档", el("small", {}, String(archived.length)))),
      el("div", { class: "grid" }, ...archived.map(card))));
  }
  $("#project-groups").replaceChildren(...groups);
}

function card(p, i) {
  const st = p.stats || {};
  const open = () => go(routes.project(p.id));
  const menuBtn = el("button", { type: "button", class: "menu-btn", "aria-label": "更多操作", "aria-haspopup": "menu", "aria-expanded": "false",
    onclick: (e) => { e.stopPropagation(); toggleMenu(p, menuBtn); } },
    svg('<svg viewBox="0 0 16 16"><circle cx="3.5" cy="8" r="1.3" fill="currentColor"/><circle cx="8" cy="8" r="1.3" fill="currentColor"/><circle cx="12.5" cy="8" r="1.3" fill="currentColor"/></svg>'));
  const top = el("div", { class: "badges-top" },
    st.pending > 0 && el("span", { class: "pill review" }, `${st.pending} 项待审核`),
    st.feed_errors > 0 && el("span", { class: "pill bad" }, `${st.feed_errors} 条采集出错`),
  );
  const chips = (p.packs || []).includes("research")
    ? [["节点", st.nodes], ["关系", st.links], ["文献", st.sources ?? 0], ["论文", st.manuscripts ?? 0]]
    : [["节点", st.nodes], ["关系", st.links], ["数据流", st.feeds], ["新闻 7d", st.news_7d]];
  return el("article", {
    class: `card${p.status === "archived" ? " archived" : ""}`, style: `--i:${i}`, tabindex: "0", role: "link",
    "aria-label": `打开 ${p.name}`, onclick: open, onkeydown: (e) => { if (e.key === "Enter") open(); },
  },
    el("div", { class: "card-sky" }, constellation(p.sample || { nodes: [], links: [] }, p.id), top, menuBtn),
    el("div", { class: "card-body" },
      el("h3", {}, p.name),
      el("p", { class: `desc${p.description ? "" : " none"}` }, p.description || "还没有描述"),
      el("div", { class: "chips" }, ...chips.map(([k, v]) => el("span", { class: "chip-stat" }, k, el("b", {}, String(v ?? 0))))),
      el("div", { class: "card-foot" },
        el("span", { class: "when" }, `更新于 ${ago(p.last_activity)}`),
        el("span", { class: "open" }, p.status === "archived" ? "查看" : "打开工作台",
          svg('<svg viewBox="0 0 12 12"><path d="M2 6h8M6.5 2.5 10 6 6.5 9.5" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"/></svg>')),
      ),
    ),
  );
}

function toggleMenu(p, btn) {
  if (openMenu?.btn === btn) { closeMenu(); return; }
  closeMenu();
  const act = (fn) => (e) => { e.stopPropagation(); closeMenu(); fn(); };
  const menu = el("div", { class: "menu", role: "menu", onclick: (e) => e.stopPropagation() },
    el("button", { type: "button", role: "menuitem", onclick: act(() => openDialog(p)) }, "编辑名称和描述"),
    el("button", { type: "button", role: "menuitem", onclick: act(() => exportProject(p)) }, "导出 JSON"),
    p.status === "active" && !(p.packs || []).includes("research") &&
      el("button", { type: "button", role: "menuitem", onclick: act(() => addResearchTypes(p)) }, "加入研究类型"),
    !cloudWorkspace && el("button", { type: "button", role: "menuitem", onclick: act(() => copyMcp(p)) }, "复制 MCP 配置"),
    el("button", { type: "button", role: "menuitem", class: p.status === "active" ? "danger" : "",
      onclick: act(() => setStatus(p, p.status === "active" ? "archived" : "active")) }, p.status === "active" ? "归档" : "恢复"),
  );
  btn.parentElement.append(menu);
  btn.setAttribute("aria-expanded", "true");
  openMenu = { menu, btn };
}

async function exportProject(p) {
  try {
    if (await download(`/api/projects/${encodeURIComponent(p.id)}/export`, `${p.id}.json`)) toast("已导出");
  } catch (e) { toast(e?.message || String(e), true); }
}

async function addResearchTypes(p) {
  if (!await confirmAction(`给「${p.name}」加入研究类型（问题、假设、论断、观测、方法）？已有的节点和类型保留，同名类型会合并。`)) return;
  try {
    const r = await api(`/api/projects/${encodeURIComponent(p.id)}/packs/research`, { method: "POST" });
    toast(`已加入 ${r.added.length} 个类型`);
    loadProjects();
  } catch (e) { toast(e.message, true); }
}

async function setStatus(p, status) {
  if (status === "archived" && !await confirmAction(`归档「${p.name}」？归档后停止采集，数据保留，可以随时恢复。`)) return;
  try {
    await api(`/api/projects/${encodeURIComponent(p.id)}`, { method: "PATCH", body: JSON.stringify({ status }) });
    toast(status === "archived" ? "已归档" : "已恢复");
    loadProjects();
  } catch (e) { toast(e.message, true); }
}

// ------------------------------------------------------------------ dialog

const dialog = $("#project-dialog");
let editing = null;
function openDialog(p = null) {
  editing = p;
  $("#dialog-title").textContent = p ? "编辑本体项目" : "创建本体项目";
  $("#dialog-ok").textContent = p ? "保存" : "创建";
  $("#dialog-hint").hidden = !!p;
  $("#f-pack-field").hidden = !!p;
  $("#f-pack").value = "research";
  $("#f-name").value = p?.name || "";
  $("#f-desc").value = p?.description || "";
  dialog.showModal();
  $("#f-name").focus();
}
$("#create-btn").addEventListener("click", () => openDialog());
$("#dialog-cancel").addEventListener("click", () => dialog.close());
$("#project-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const fields = { name: $("#f-name").value.trim(), description: $("#f-desc").value.trim() };
  const body = JSON.stringify(editing ? fields : { ...fields, pack: $("#f-pack").value });
  $("#dialog-ok").disabled = true;
  try {
    if (editing) {
      await api(`/api/projects/${encodeURIComponent(editing.id)}`, { method: "PATCH", body });
      dialog.close();
      toast("已保存");
      loadProjects();
    } else {
      const p = await api("/api/projects", { method: "POST", body });
      dialog.close();
      toast(`已创建「${p.name}」，正在打开工作台`);
      setTimeout(() => go(routes.project(p.id)), 700);
    }
  } catch (err) { toast(err.message, true); }
  finally { $("#dialog-ok").disabled = false; }
});

// ------------------------------------------------------------------ import

$("#import-btn").addEventListener("click", () => $("#import-file").click());
$("#import-file").addEventListener("change", async (e) => {
  const file = e.target.files[0];
  e.target.value = "";
  if (!file) return;
  try {
    const text = await file.text();
    JSON.parse(text);
    const p = await api("/api/projects/import", { method: "POST", body: text });
    toast(`已导入「${p.name}」：${p.stats.nodes} 个节点、${p.stats.links} 条关系`);
    loadProjects();
  } catch (err) { toast(err instanceof SyntaxError ? "文件不是有效的 JSON" : err.message, true); }
});

// ------------------------------------------------------------------ mcp

let mcpInfo = null;
const GROUPS = [
  ["项目", ["list_projects", "use_project"]],
  ["读图", ["get_schema", "search_nodes", "get_node", "get_neighbors", "find_paths", "validate_graph"]],
  ["改图", ["upsert_node", "update_props", "create_link", "merge_nodes", "archive", "propose_type"]],
  ["审核", ["submit_changeset", "list_changesets", "revert_changeset"]],
  ["数据", ["list_sources", "list_bindings", "get_latest", "get_series", "propose_binding"]],
  ["新闻、社交与证据", ["search_news", "search_x", "get_link_evidence", "add_evidence_note"]],
  ["A 股（同花顺）", ["a_share_search", "a_share_quote", "a_share_financials", "a_share_index_members"]],
];
const ZH = {
  list_projects: "列出全部本体项目和当前正在操作的项目。",
  use_project: "切换后续工具调用作用的项目（工作台里由页面决定，不能切换）。",
  get_schema: "列出全部对象类型和关系类型（属性、允许的两端），以及待审核的类型提案。",
  search_nodes: "按名称、key 或别名搜索节点。建节点前必须先搜，避免重复。",
  get_node: "读取一个节点：属性、来源和它的所有关系。",
  get_neighbors: "读取节点周围 1–3 跳的子图。",
  find_paths: "找两个节点之间最短的关系链，比如降息如何传导到 BTC。",
  validate_graph: "检查孤立节点、疑似重复和不符合当前类型定义的数据。",
  upsert_node: "新建节点，或更新同类型同 key 的节点；疑似重复时拒绝。",
  update_props: "修改节点的属性、描述或别名。",
  create_link: "在两个节点间建立有类型的关系，必须写理由和来源。",
  merge_nodes: "把重复节点并入另一个，关系随之转移，名称变为别名。",
  archive: "软删除节点（连同关系）、关系或数据绑定，不做物理删除。",
  propose_type: "提议新的对象或关系类型，人接受后才能使用。",
  submit_changeset: "把本轮改动提交给人审核。",
  list_changesets: "查看最近的变更集和状态。",
  revert_changeset: "整批撤销一个变更集（默认最近一个）。",
  list_sources: "可绑定的数据源及参数（FRED、同花顺、手动导入…）。",
  list_bindings: "节点绑定了哪些数据序列，以及最新值、涨跌和健康状态。",
  get_latest: "读取节点数据的最新值、变化和新鲜度。",
  get_series: "读取节点数据的时间序列。",
  propose_binding: "提议把节点绑定到一条数据序列，审核通过后开始采集。",
  search_news: "检索最近的 BTC 相关新闻，附带每篇提到的节点和情绪。",
  search_x: "经 Grok 实时搜索 X（推特）上的帖子，返回作者、原文、时间和链接；找到的帖子进入新闻管道。",
  a_share_search: "按名称或代码检索 A 股、指数、同花顺板块、基金，返回 thscode。",
  a_share_quote: "最多 50 只 A 股或指数的最新价、涨跌幅，股票另附 PE/PB/PS/PCF。",
  a_share_financials: "A 股某报告期的成长、盈利、偿债、现金流指标，默认最新一期。",
  a_share_index_members: "指数或同花顺板块的当前成分股。",
  get_link_evidence: "查看关系的证据：数据联动、新闻共现、分析笔记和综合强度。",
  add_evidence_note: "为关系记录自己的证据（支持或反驳），注明依据。",
};
const WRITES = new Set(["upsert_node", "update_props", "create_link", "merge_nodes", "archive", "propose_type", "submit_changeset",
  "revert_changeset", "propose_binding", "add_evidence_note"]);

let mcpFormat = "cli";
const shq = (v) => (/^[\w@%+=:,./-]+$/.test(v) ? v : `'${v.replace(/'/g, "'\\''")}'`);

// Secrets are not in the config: the server reads DATABASE_URL, THS_API_KEY and the model gateway from the project's
// .env. PYTHONPATH (not cwd) points at the code, because some clients ignore cwd. A project is optional.
function mcpConfig(pid, format = mcpFormat) {
  if (!mcpInfo) return "";
  const name = `onto-research${pid ? `-${pid}` : ""}`;
  const env = { PYTHONPATH: mcpInfo.cwd, ...(pid ? { ONTOLOGY_PROJECT: pid } : {}) };
  if (format === "cli") {
    return ["claude mcp add --scope user", ...Object.entries(env).map(([k, v]) => `--env ${k}=${shq(v)}`), name,
      "--", shq(mcpInfo.python), "-m ontology.mcp_server"].join(" ");
  }
  return JSON.stringify({ mcpServers: { [name]: { command: mcpInfo.python, args: ["-m", "ontology.mcp_server"], env } } }, null, 2);
}

function showMcpConfig() {
  const pid = $("#mcp-project").value;
  const p = projects.find((x) => x.id === pid);
  const pre = $("#mcp-config");
  pre.textContent = mcpConfig(pid);
  pre.classList.toggle("wrap", mcpFormat === "cli");
  $("#mcp-project-hint").textContent = pid
    ? `每次启动都从「${p?.name || pid}」开始。适合只给某个项目用的客户端；对话中仍可切换。`
    : "一条配置访问全部项目：默认从 BTC 开始，对话里说「切到半导体项目」，agent 会用 list_projects / use_project 切换。";
  $("#mcp-format-hint").replaceChildren(...(mcpFormat === "cli"
    ? ["在终端运行一次即可，", el("code", {}, "--scope user"), " 让所有目录下的 Claude Code 都能用；改成 ",
       el("code", {}, "--scope project"), " 则只写进当前目录的 .mcp.json。"]
    : ["Claude Desktop：设置 › 开发者 › 编辑配置；Cursor 等写进各自的 MCP 配置文件。密钥不在配置里，服务启动时从 ",
       el("code", {}, mcpInfo.env_file), " 读取。"]));
}

function renderNeeds() {
  const g = mcpInfo.gateway;
  const grok = g.grok.length ? ["ok", "可用"] : g.base_url ? ["warn", "缺 Grok"] : ["idle", "未配置"];
  const items = [
    ["读图、改图、审核、数据、新闻、证据", ["ok", "不需要"], `其余 ${mcpInfo.tools.length - 5} 个工具直接读写本体数据库，和用什么模型无关。`],
    ["A 股工具（同花顺）", mcpInfo.ths ? ["ok", "已配置"] : ["warn", "缺密钥"],
      mcpInfo.ths ? "用 .env 里的 THS_API_KEY，不需要 magpie。" : "需要 THS_API_KEY：在「数据源」页的同花顺卡片上填写，不需要 magpie。"],
    ["search_x（搜索 X / 推特）", grok,
      g.grok.length ? `经 magpie 调用 ${g.grok[0]}。没有 magpie 时这个工具会报错，其他工具不受影响。`
        : "唯一需要 magpie 的工具：magpie 里配好 Grok，并在 .env 里设 ANTHROPIC_BASE_URL 指向它。"],
    ["新闻抽取、研报生成", ["idle", "工作台负责"],
      g.magpie ? "在工作台后台运行，用工作台的模型（现在经 magpie）。MCP 客户端读到的是结果。" : "在工作台后台运行，用工作台配置的模型。MCP 客户端读到的是结果。"],
  ];
  $("#mcp-needs").replaceChildren(...items.map(([title, [cls, label], text]) =>
    el("li", {}, el("b", {}, title), el("span", { class: `pill ${cls}` }, label), el("p", {}, text))));
}

async function loadMcp() {
  try {
    mcpInfo = mcpInfo || await api("/api/mcp");
    if (!projects.length) projects = await api("/api/projects");
  } catch (e) { toast(e.message, true); return; }
  const sel = $("#mcp-project");
  const keep = sel.value;
  sel.replaceChildren(el("option", { value: "" }, "不固定（推荐）· 对话里切换"),
    ...projects.filter((p) => p.status === "active").map((p) => el("option", { value: p.id }, `固定为 ${p.name}`)));
  sel.value = keep || new URLSearchParams(location.search).get("project") || "";
  showMcpConfig();
  renderNeeds();
  loadExtMcp();
  const byName = new Map(mcpInfo.tools.map((t) => [t.name, t]));
  const seen = new Set(GROUPS.flatMap(([, ns]) => ns));
  const groups = [...GROUPS, ["其他", mcpInfo.tools.map((t) => t.name).filter((n) => !seen.has(n))]];
  $("#tool-count").textContent = `${mcpInfo.tools.length} 个`;
  $("#tool-groups").replaceChildren(...groups.filter(([, ns]) => ns.some((n) => byName.has(n))).map(([title, names]) =>
    el("div", { class: "tool-group" }, el("h3", {}, title),
      ...names.filter((n) => byName.has(n)).map((n) => {
        const t = byName.get(n);
        return el("div", { class: "tool" },
          el("code", { class: WRITES.has(n) ? "write" : "", title: WRITES.has(n) ? "会写入本体" : "只读" }, n),
          el("p", {}, ZH[n] || t.description.split(/(?<=[.。])\s/)[0]),
          t.params.length > 0 && el("div", { class: "params" }, ...t.params.map((x) => el("span", {}, x))),
        );
      }))));
}
$("#mcp-project").addEventListener("change", showMcpConfig);
for (const b of document.querySelectorAll(".seg [data-fmt]")) {
  b.addEventListener("click", () => {
    mcpFormat = b.dataset.fmt;
    for (const x of document.querySelectorAll(".seg [data-fmt]")) x.setAttribute("aria-selected", String(x === b));
    showMcpConfig();
  });
}
$("#copy-config").addEventListener("click", async () => {
  try { await navigator.clipboard.writeText($("#mcp-config").textContent); toast("已复制 MCP 配置"); }
  catch { toast("复制失败，请手动选择文本", true); }
});
async function copyMcp(p) {
  try {
    mcpInfo = mcpInfo || await api("/api/mcp");
    await navigator.clipboard.writeText(mcpConfig(p.id));
    toast(`已复制「${p.name}」的 Claude Code 接入命令`);
  } catch { toast("复制失败，可在「MCP 接入」页手动复制", true); }
}

// ------------------------------------------------------------------ external MCP servers

// Servers the user connected for the agent and the collector. Header values stay on this machine; the page only
// ever gets their last 4 characters, and sends `keep` for a header it leaves alone.
async function loadExtMcp() {
  let data;
  try { data = await api("/api/mcp/servers"); }
  catch { $("#ext-mcp").hidden = true; return; }  // a cloud workspace, or not this machine
  $("#ext-mcp").hidden = false;
  $("#ext-mcp-file").textContent = data.file;
  $("#ext-mcp-count").textContent = String(data.servers.length);
  $("#ext-mcp-list").replaceChildren(...(data.servers.length ? data.servers.map(extMcpCard)
    : [el("p", { class: "lede" }, "还没有接入外部 MCP 服务。点「添加 MCP 服务」，填上名称、URL 和需要的请求头（例如 Authorization 或 X-Api-Key）。")]));
}

function savedDefinition(s) {
  return { name: s.name, transport: s.transport, url: s.url, enabled: s.enabled,
           headers: s.headers.map((h) => ({ key: h.key, keep: true })) };
}

function showMcpTest(box, r) {
  const head = el("p", { class: `test-result ${r.ok ? "ok" : "bad"}` },
    r.ok ? `连接正常 · ${r.tools.length} 个工具 · ${r.seconds}s` : `连接失败 · ${r.message}`);
  box.replaceChildren(...[head, r.ok && r.tools.length > 0 && el("ul", {}, ...r.tools.map((t) =>
    el("li", {}, el("span", {}, t.name), el("span", { class: "tool-desc", title: t.description }, t.description))))].filter(Boolean));
  box.hidden = false;
}

async function testMcp(body, box, btn) {
  btn.disabled = true; const label = btn.textContent; btn.textContent = "连接中…";
  try { showMcpTest(box, await api("/api/mcp/servers/test", { method: "POST", body: JSON.stringify(body) })); }
  catch (e) { showMcpTest(box, { ok: false, message: e.message }); }
  btn.disabled = false; btn.textContent = label;
}

function extMcpCard(s, i) {
  const box = el("div", { class: "source-test", hidden: true });
  const test = el("button", { type: "button", class: "btn sm", onclick: () => testMcp({ name: s.name, definition: savedDefinition(s) }, box, test) }, "测试连接");
  const remove = async () => {
    const used = s.used_by.length ? `\n数据源 ${s.used_by.map((t) => `「${t}」`).join("、")} 用到了它，删除后这些数据源会采集失败。` : "";
    if (!await confirmAction(`删除 MCP 服务「${s.name}」？保存的请求头也会一并删除。${used}`)) return;
    try {
      await api(`/api/mcp/servers/${encodeURIComponent(s.name)}`, { method: "DELETE" });
      toast(`已删除「${s.name}」`);
      loadExtMcp();
    } catch (e) { toast(e.message, true); }
  };
  return el("article", { class: "src-card", style: `--i:${i}` },
    el("div", { class: "src-head" },
      el("h3", {}, s.name),
      el("span", { class: `src-state ${s.enabled ? "ok" : "idle"}` }, s.enabled ? "已启用" : "已停用")),
    el("div", { class: "src-actions" }, el("span", { class: "src-kind" }, "Streamable HTTP")),
    el("p", { class: "src-url" }, s.url),
    s.headers.length > 0 && el("ul", { class: "src-list" }, ...s.headers.map((h) => el("li", {},
      el("span", {}, h.key), el("code", {}, h.hint)))),
    el("p", { class: "src-note" }, s.enabled ? `agent 里的工具名：mcp__${s.name}__…` : "停用后 agent 看不到它的工具，数据源也不会调用它。"),
    s.used_by.length > 0 && el("p", { class: "src-note" }, "数据源：", s.used_by.join("、")),
    el("div", { class: "src-foot" },
      el("span", { class: "src-actions" },
        el("button", { type: "button", class: "link", onclick: () => openMcpDialog(s) }, "编辑"),
        el("button", { type: "button", class: "link danger", onclick: remove }, "删除")),
      test),
    box,
  );
}

const mcpDialog = $("#mcp-dialog");
const mcpForm = $("#mcp-form");
const mcpField = (name) => mcpForm.elements.namedItem(name);
let editingMcp = null;

function headerRow(h = null) {
  const key = el("input", { class: "h-key", value: h?.key || "", placeholder: "名称，例如 Authorization", maxlength: "100",
    autocomplete: "off", spellcheck: "false", "aria-label": "请求头名称" });
  const value = el("input", { class: "h-value", type: "password", maxlength: "4000", autocomplete: "off", spellcheck: "false",
    placeholder: h ? `已保存 ${h.hint}，留空则不变` : "值，例如 Bearer …", "aria-label": "请求头的值" });
  const row = el("div", { class: "header-row" }, key, value,
    el("button", { type: "button", class: "link danger", "aria-label": "删除这个请求头", onclick: () => row.remove() }, "删除"));
  row.dataset.saved = h ? h.key : "";
  return row;
}

function mcpDefinition() {
  const headers = [...$("#mcp-headers").children].map((row) => {
    const key = row.querySelector(".h-key").value.trim();
    const value = row.querySelector(".h-value").value;
    return { key, value, keep: !value && !!row.dataset.saved && row.dataset.saved.toLowerCase() === key.toLowerCase() };
  });
  return { name: mcpField("name").value.trim(), transport: mcpField("transport").value, url: mcpField("url").value.trim(),
           enabled: mcpField("enabled").checked, headers };
}

function openMcpDialog(s = null) {
  editingMcp = s;
  mcpForm.reset();
  $("#mcp-dialog-title").textContent = s ? `编辑「${s.name}」` : "添加 MCP 服务";
  mcpField("name").value = s?.name || "";
  mcpField("name").disabled = !!s;
  mcpField("transport").value = s?.transport || "streamable_http";
  mcpField("url").value = s?.url || "";
  mcpField("enabled").checked = s ? s.enabled : true;
  $("#mcp-headers").replaceChildren(...(s?.headers.length ? s.headers.map(headerRow) : [headerRow()]));
  $("#mcp-test-box").hidden = true;
  mcpDialog.showModal();
}

$("#add-mcp-btn").addEventListener("click", () => openMcpDialog());
$("#mcp-add-header").addEventListener("click", () => {
  const row = headerRow();
  $("#mcp-headers").append(row);
  row.querySelector(".h-key").focus();
});
$("#mcp-cancel").addEventListener("click", () => mcpDialog.close());
$("#mcp-test-btn").addEventListener("click", () => {
  if (!mcpField("url").checkValidity()) { mcpField("url").reportValidity(); return; }
  testMcp({ name: editingMcp?.name, definition: mcpDefinition() }, $("#mcp-test-box"), $("#mcp-test-btn"));
});
mcpForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  const btn = $("#mcp-save");
  btn.disabled = true;
  try {
    const d = mcpDefinition();
    await api(editingMcp ? `/api/mcp/servers/${encodeURIComponent(editingMcp.name)}` : "/api/mcp/servers",
      { method: editingMcp ? "PUT" : "POST", body: JSON.stringify(d) });
    mcpDialog.close();
    toast(editingMcp ? `已保存「${d.name || editingMcp.name}」` : `已添加「${d.name}」，agent 下次对话时就能用它的工具`);
    loadExtMcp();
  } catch (err) { showMcpTest($("#mcp-test-box"), { ok: false, message: err.message }); }
  btn.disabled = false;
});

// ------------------------------------------------------------------ data sources

const CATEGORY_ORDER = ["a-share", "macro", "news", "social", "internal"];

async function loadSources() {
  try {
    const { providers, categories, custom = [], can_manage: canManage } = await api("/api/sources");
    $("#add-source-btn").hidden = !canManage;
    const order = [...CATEGORY_ORDER, ...Object.keys(categories).filter((c) => !CATEGORY_ORDER.includes(c))];  // new ones still show
    const groups = order.map((c) => [c, providers.filter((p) => p.category === c)]).filter(([, ps]) => ps.length);
    const mine = canManage && el("section", {},
      el("h2", { class: "group-title" }, "我添加的", el("small", {}, String(custom.length))),
      custom.length
        ? el("div", { class: "grid sources" }, ...custom.map(customCard))
        : el("p", { class: "lede" }, "还没有自己添加的数据源。点右上角「添加数据源」，接入 RSS 新闻或任意返回 JSON / CSV 的数据接口。"));
    $("#source-groups").replaceChildren(...[mine, ...groups.map(([c, ps]) => el("section", {},
      el("h2", { class: "group-title" }, categories[c] || c, el("small", {}, String(ps.length))),
      el("div", { class: "grid sources" }, ...ps.map(sourceCard))))].filter(Boolean));
  } catch (e) {
    $("#source-groups").replaceChildren(el("p", { class: "lede" }, `数据源读取失败：${e.message}`));
  }
}

// The key is written to .env on this machine and takes effect at once; the page only gets its last 4 characters.
function keyRow(p) {
  const row = el("div", { class: "src-key" });
  const save = async (value) => {
    try {
      await api(`/api/sources/${encodeURIComponent(p.id)}/key`, { method: "PUT", body: JSON.stringify({ key: value }) });
      loadSources();
    } catch (e) { msg.textContent = e.message; msg.hidden = false; }
  };
  const msg = el("p", { class: "test-result bad", hidden: true });
  const editor = () => {
    const input = el("input", { type: "password", class: "key-input", placeholder: `粘贴 ${p.auth_env}`, autocomplete: "off",
      spellcheck: "false", "aria-label": `${p.name} 的 API Key` });
    const ok = el("button", { type: "submit", class: "btn sm primary" }, "保存");
    const form = el("form", { class: "key-form", onsubmit: (e) => {
      e.preventDefault();
      if (!input.value.trim()) { input.focus(); return; }
      ok.disabled = true; ok.textContent = "保存中…"; save(input.value);
    } }, input, ok,
      p.configured && el("button", { type: "button", class: "btn sm", onclick: () => row.replaceChildren(...view()) }, "取消"));
    setTimeout(() => input.focus());
    return [form, el("p", { class: "key-note" }, "保存到本机的 ", el("code", {}, ".env"), "，立即生效，不会上传到别处。"), msg];
  };
  const view = () => [el("div", { class: "key-line" },
    el("span", {}, "API Key ", el("code", {}, p.key_hint)),
    el("span", { class: "key-actions" },
      el("button", { type: "button", class: "link", onclick: () => row.replaceChildren(...editor()) }, "更换"),
      el("button", { type: "button", class: "link danger", onclick: async () => { if (await confirmAction(`清除 ${p.name} 的 API Key？`)) save(""); } }, "清除"))), msg];
  row.replaceChildren(...(p.configured ? view() : editor()));
  return row;
}

function sourceCard(p, i) {
  const h = p.health;
  const state = !p.configured ? ["warn", `需要 ${p.auth_env}`] : h.errors ? ["bad", `${h.errors} 条出错`]
    : h.series ? ["ok", "运行中"] : ["idle", p.auth_env ? "已配置 · 未使用" : "可用 · 未使用"];
  const result = el("p", { class: "test-result", hidden: true });
  const btn = el("button", { type: "button", class: "btn sm", onclick: async () => {
    btn.disabled = true; btn.textContent = "测试中…"; result.hidden = true;
    try {
      const r = await api(`/api/sources/${encodeURIComponent(p.id)}/test`, { method: "POST" });
      result.className = `test-result ${r.ok ? "ok" : "bad"}`;
      result.textContent = `${r.ok ? "连接正常" : "连接失败"}${r.seconds != null ? ` · ${r.seconds}s` : ""} · ${r.message}`;
    } catch (e) { result.className = "test-result bad"; result.textContent = e.message; }
    result.hidden = false; btn.disabled = false; btn.textContent = "测试连接";
  } }, "测试连接");
  return el("article", { class: "src-card", style: `--i:${i}` },
    el("div", { class: "src-head" },
      el("h3", {}, p.name),
      el("span", { class: `src-state ${state[0]}` }, state[1])),
    p.note && el("p", { class: "src-note" }, p.note),
    el("div", { class: "src-stats" },
      el("span", {}, "数据流 ", el("b", {}, String(h.series))),
      h.stale > 0 && el("span", {}, "陈旧 ", el("b", {}, String(h.stale))),
      el("span", {}, "最近成功 ", el("b", {}, h.last_ok ? ago(h.last_ok) : "—")),
      h.projects.length > 0 && el("span", {}, "项目 ", el("b", {}, h.projects.join("、")))),
    h.last_error && el("p", { class: "src-error" }, `${h.last_error.series}：${h.last_error.error}`),
    p.auth_env && !cloudWorkspace && keyRow(p),
    p.sources.length > 0 && el("ul", { class: "src-list" }, ...p.sources.map((s) => el("li", {},
      el("span", {}, s.title), el("code", {}, s.source)))),
    el("div", { class: "src-foot" },
      p.docs ? el("a", { href: p.docs, target: "_blank", rel: "noopener noreferrer" }, "接口文档") : el("span"),
      p.category !== "internal" && btn),
    result,
  );
}

// ------------------------------------------------------------------ custom data sources

const KIND_NAMES = { json: "JSON 接口", csv: "CSV 文件", rss: "RSS 新闻", mcp: "MCP 工具" };
const FREQ_NAMES = { "5m": "5 分钟", "1h": "1 小时", "4h": "4 小时", "8h": "8 小时", "1d": "1 天", "1w": "1 周", "1mo": "1 月", "1q": "1 季度" };

function customCard(c, i) {
  const h = c.health;
  const state = h.errors ? ["bad", `${h.errors} 条出错`] : h.series ? ["ok", "运行中"] : ["idle", "可用 · 未使用"];
  const cfg = c.config || {};
  const how = c.kind === "rss" ? `语言 ${cfg.lang === "en" ? "英文" : "中文"}`
    : [c.kind !== "csv" && `路径 ${cfg.path || "（根）"}`, `时间 ${cfg.time || "采集时刻"}`, `数值 ${cfg.value || "返回值本身"}`,
       FREQ_NAMES[cfg.frequency] || cfg.frequency, `每 ${cfg.poll_minutes} 分钟采集`, cfg.unit && `单位 ${cfg.unit}`].filter(Boolean).join(" · ");
  const where = c.kind === "mcp"
    ? `${cfg.server} → ${cfg.tool}${Object.keys(cfg.arguments || {}).length ? ` ${JSON.stringify(cfg.arguments)}` : ""}` : c.url;
  const remove = async () => {
    const kept = h.series ? `\n已有 ${h.series} 条数据流（${h.projects.join("、")}）会保留已采集的数据，但不再更新。` : "";
    if (!await confirmAction(`删除数据源「${c.title}」？${kept}`)) return;
    try {
      await api(`/api/sources/custom/${encodeURIComponent(c.name)}`, { method: "DELETE" });
      toast(`已删除「${c.title}」`);
      loadSources();
    } catch (e) { toast(e.message, true); }
  };
  return el("article", { class: "src-card", style: `--i:${i}` },
    el("div", { class: "src-head" },
      el("h3", {}, c.title),
      el("span", { class: `src-state ${state[0]}` }, state[1])),
    el("div", { class: "src-actions" }, el("span", { class: "src-kind" }, KIND_NAMES[c.kind] || c.kind), el("code", { class: "src-url" }, c.name)),
    el("p", { class: "src-url" }, where),
    el("p", { class: "src-note" }, how),
    c.params.length > 0 && el("p", { class: "src-note" }, "绑定时填写：", ...c.params.flatMap((p, k) => [k ? "、" : "", el("code", {}, p)])),
    el("div", { class: "src-stats" },
      el("span", {}, c.kind === "rss" ? "项目 " : "数据流 ", el("b", {}, String(h.series))),
      h.stale > 0 && el("span", {}, "陈旧 ", el("b", {}, String(h.stale))),
      el("span", {}, "最近成功 ", el("b", {}, h.last_ok ? ago(h.last_ok) : "—"))),
    h.last_error && el("p", { class: "src-error" }, `${h.last_error.series}：${h.last_error.error}`),
    el("div", { class: "src-foot" },
      el("span", { class: "src-actions" },
        el("button", { type: "button", class: "link", onclick: () => openSourceDialog(c) }, "编辑"),
        el("button", { type: "button", class: "link danger", onclick: remove }, "删除")),
      el("span")),
  );
}

const sourceDialog = $("#source-dialog");
const sourceForm = $("#source-form");
const field = (name) => sourceForm.elements.namedItem(name);
let editingSource = null;  // the custom source being edited, or null when adding

function urlParams(url) {
  return [...new Set([...url.matchAll(/\{([a-z][a-z0-9_]*)\}/g)].map((m) => m[1]))];
}

function syncSourceForm() {
  const kind = field("kind").value;
  const series = kind !== "rss";
  const mcp = kind === "mcp";
  const params = mcp ? urlParams(field("arguments").value) : urlParams(field("url").value);
  const show = { rss: !series, series, mcp, url: !mcp, "http-series": kind === "json" || kind === "csv",
                 path: kind !== "csv", params: params.length > 0 };
  for (const node of sourceForm.querySelectorAll("[data-for]")) node.hidden = !show[node.dataset.for];
  sourceForm.querySelector('[data-label="time"]').textContent = kind === "csv" ? "时间列" : "时间字段";
  sourceForm.querySelector('[data-label="value"]').textContent = kind === "csv" ? "数值列" : "数值字段";
  field("time").placeholder = kind === "csv" ? "例如 date" : "例如 date；留空则记录采集时刻的数值";
  field("value").placeholder = mcp ? "例如 price；工具只返回一个数时留空" : "例如 close";
  field("test_params").placeholder = `例如 ${params.map((p) => `${p}=…`).join(" ") || "symbol=600519"}`;
  field("url").required = !mcp;
  field("time").required = kind === "csv";
  field("value").required = kind === "json" || kind === "csv";
  field("server").required = field("tool").required = mcp;
}

// The MCP servers connected on the MCP 接入 page, for the MCP 工具 kind; and the tools of the one picked.
let sourceTools = [];
async function fillServerChoices(chosen) {
  const sel = field("server");
  let servers = [];
  try { servers = (await api("/api/mcp/servers")).servers; } catch { /* a cloud workspace has none */ }
  sel.replaceChildren(el("option", { value: "" }, servers.length ? "选择一个服务" : "还没有接入 MCP 服务"),
    ...servers.map((s) => el("option", { value: s.name }, `${s.name}${s.enabled ? "" : "（已停用）"}`)));
  if (chosen && !servers.some((s) => s.name === chosen)) sel.append(el("option", { value: chosen }, `${chosen}（已删除）`));
  sel.value = chosen || "";
}
async function loadSourceTools() {
  const name = field("server").value;
  const help = $("#mcp-tool-help");
  sourceTools = [];
  $("#mcp-tool-names").replaceChildren();
  help.hidden = !name;
  if (!name) return;
  help.textContent = "正在读取工具列表…";
  try {
    sourceTools = (await api(`/api/mcp/servers/${encodeURIComponent(name)}/tools`)).tools;
    $("#mcp-tool-names").replaceChildren(...sourceTools.map((t) => el("option", { value: t.name }, t.description.slice(0, 80))));
    showToolHelp();
  } catch (e) { help.textContent = e.message; }
}
function showToolHelp() {
  const help = $("#mcp-tool-help");
  const t = sourceTools.find((x) => x.name === field("tool").value.trim());
  if (!t) { help.textContent = sourceTools.length ? `这个服务有 ${sourceTools.length} 个工具，在「工具」里选一个。` : ""; return; }
  const props = t.input_schema?.properties || {};
  const need = new Set(t.input_schema?.required || []);
  const args = Object.keys(props).map((k) => `${k}${need.has(k) ? "（必填）" : ""}`);
  const about = t.description.length > 240 ? `${t.description.slice(0, 240)}…` : t.description;
  help.textContent = `${about}${args.length ? ` 参数：${args.join("、")}` : " 不需要参数。"}`;
  if (!field("arguments").value.trim() && need.size) {  // a starting point: the required arguments, to be filled in
    field("arguments").value = JSON.stringify(Object.fromEntries([...need].map((k) => [k, ""])));
    syncSourceForm();
  }
}

function openSourceDialog(c = null) {
  editingSource = c;
  sourceForm.reset();
  const cfg = c?.config || {};
  $("#source-dialog-title").textContent = c ? `编辑「${c.title}」` : "添加数据源";
  field("kind").value = c?.kind || "json";
  field("kind").disabled = field("name").disabled = !!c;
  for (const n of ["name", "title"]) field(n).value = c?.[n] || "";
  field("url").value = c && c.kind !== "mcp" ? c.url : "";
  for (const n of ["path", "time", "value", "unit", "tool"]) field(n).value = cfg[n] || "";
  field("arguments").value = cfg.arguments && Object.keys(cfg.arguments).length ? JSON.stringify(cfg.arguments) : "";
  field("lang").value = cfg.lang || "zh";
  field("time_format").value = cfg.time_format || "auto";
  field("frequency").value = cfg.frequency || "1d";
  field("poll_minutes").value = cfg.poll_minutes || 60;
  field("backfill_days").value = cfg.backfill_days || 365;
  sourceForm.querySelector(".source-test").hidden = true;
  syncSourceForm();
  sourceDialog.showModal();
  fillServerChoices(cfg.server).then(() => { if (field("kind").value === "mcp") loadSourceTools(); });
}

function sourceDefinition() {
  const kind = field("kind").value;
  const v = (n) => field(n).value.trim();
  const config = kind === "rss" ? { lang: v("lang") } : {
    path: v("path"), time: v("time"), value: v("value"), time_format: v("time_format"), frequency: v("frequency"),
    unit: v("unit"), poll_minutes: Number(v("poll_minutes")) || 60, backfill_days: Number(v("backfill_days")) || 365,
    ...(kind === "mcp" ? { server: v("server"), tool: v("tool"), arguments: v("arguments") } : {}),
  };
  return { kind, name: v("name"), title: v("title"), url: kind === "mcp" ? "" : v("url"), config };
}

function showTest(r) {
  const box = sourceForm.querySelector(".source-test");
  const head = el("p", { class: `test-result ${r.ok ? "ok" : "bad"}` });
  if (!r.ok) head.textContent = `读取失败 · ${r.message}`;
  else if (r.kind === "rss") head.textContent = `读到 ${r.count} 篇文章 · ${r.seconds}s`;
  else head.textContent = `读到 ${r.count} 个数据点 · ${FREQ_NAMES[r.frequency] || r.frequency} · ${r.seconds}s`;
  const rows = !r.ok ? [] : r.kind === "rss"
    ? r.samples.map((x) => el("li", {}, el("span", {}, x.title), el("span", {}, x.published_at.slice(0, 10))))
    : r.samples.map((x) => el("li", {}, el("span", {}, x.ts.replace("T", " ").replace(/:00\+00:00$|\+00:00$/, "")),
        el("span", {}, Number(x.value).toLocaleString("zh-CN", { maximumFractionDigits: 6 }))));
  box.replaceChildren(...[head, rows.length > 0 && el("ul", {}, ...rows)].filter(Boolean));
  box.hidden = false;
}

field("kind").addEventListener("change", () => {
  syncSourceForm();
  if (field("kind").value === "mcp" && !sourceTools.length) loadSourceTools();
});
field("url").addEventListener("input", syncSourceForm);
field("arguments").addEventListener("input", syncSourceForm);
field("server").addEventListener("change", () => { field("tool").value = ""; loadSourceTools(); });
field("tool").addEventListener("change", showToolHelp);
$("#add-source-btn").addEventListener("click", () => openSourceDialog());
$("#source-cancel").addEventListener("click", () => sourceDialog.close());
$("#source-test-btn").addEventListener("click", async () => {
  const btn = $("#source-test-btn");
  for (const n of field("kind").value === "mcp" ? ["server", "tool"] : ["url"]) {
    if (!field(n).checkValidity()) { field(n).reportValidity(); return; }
  }
  const params = Object.fromEntries(field("test_params").value.split(/[,，\s]+/).filter((x) => x.includes("="))
    .map((x) => x.split("=").map((y) => y.trim())));
  btn.disabled = true; btn.textContent = "测试中…";
  try {
    showTest(await api("/api/sources/custom/test", { method: "POST", body: JSON.stringify({ definition: sourceDefinition(), params }) }));
  } catch (e) { showTest({ ok: false, message: e.message }); }
  btn.disabled = false; btn.textContent = "测试";
});
sourceForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  const btn = $("#source-save");
  btn.disabled = true;
  try {
    const d = sourceDefinition();
    await api(editingSource ? `/api/sources/custom/${encodeURIComponent(editingSource.name)}` : "/api/sources/custom",
      { method: editingSource ? "PUT" : "POST", body: JSON.stringify(d) });
    sourceDialog.close();
    toast(editingSource ? `已保存「${d.title}」` : `已添加「${d.title}」`);
    loadSources();
  } catch (err) { showTest({ ok: false, message: err.message }); }
  btn.disabled = false;
});

// ------------------------------------------------------------------ model settings

const modelForm = $("#model-settings");
const modelSave = modelForm.querySelector('button[type="submit"]');
const modelTest = $("#test-model-settings");
const modelResult = $("#model-settings-result");
let savedModelSettings = null;

function modelMessage(text, error = false) {
  modelResult.hidden = false;
  modelResult.textContent = text;
  modelResult.classList.toggle("bad", error);
}

function renderModelFields() {
  const mode = modelForm.elements.mode.value;
  const fields = modelForm.elements;
  const route = savedModelSettings.local_routes.find((entry) => entry.id === mode);
  const custom = mode === "custom";
  const editing = mode !== "environment";
  $("#model-url-field").hidden = $("#model-auth-field").hidden = !custom;
  $("#model-name-field").hidden = $("#model-key-field").hidden = !editing;
  fields.base_url.disabled = fields.auth_kind.disabled = !custom;
  fields.base_url.required = custom;
  fields.model.required = editing;
  fields.api_key.placeholder = savedModelSettings.key_configured && savedModelSettings.mode === mode
    ? "已保存，留空则保留" : route ? "可选，留空使用路由默认凭据" : "输入 API Key";
  $("#model-route-hint").textContent = route
    ? `${route.label} · ${route.base_url} · ${route.reachable ? "端口可连接" : "端口未连接"}`
    : custom
      ? savedModelSettings.cloud
        ? `支持 Anthropic Messages。可用的 HTTPS 域名：${savedModelSettings.allowed_hosts.join("、") || "无"}。本机路由请在本地工作台使用。`
        : "填写支持 Anthropic Messages 的 Base URL。"
      : savedModelSettings.default_configured ? "当前运行环境已配置模型凭据。" : "当前运行环境未配置模型凭据。";
  $("#local-model-options").replaceChildren();
  if (route?.reachable) {
    api(`/api/settings/model/local/${mode}`).then((catalog) => {
      if (modelForm.elements.mode.value !== mode) return;
      $("#local-model-options").replaceChildren(...catalog.models.map((entry) =>
        el("option", { value: entry.id, label: entry.name })));
    }).catch(() => {});
  }
}

function fillModelSettings(data) {
  savedModelSettings = data;
  for (const option of modelForm.elements.mode.options) {
    if (["magpie", "ccswitch"].includes(option.value)) option.hidden = data.cloud;
  }
  const fields = modelForm.elements;
  fields.mode.disabled = false;
  fields.mode.value = data.mode;
  fields.model.value = data.model;
  fields.base_url.value = data.base_url;
  fields.auth_kind.value = data.auth_kind;
  fields.api_key.value = "";
  renderModelFields();
}

async function loadModelSettings() {
  modelSave.disabled = modelTest.disabled = true;
  modelForm.elements.mode.disabled = true;
  modelResult.hidden = true;
  try {
    fillModelSettings(await api("/api/settings/model"));
    modelSave.disabled = modelTest.disabled = false;
  } catch (error) { modelMessage(error.message, true); }
}

modelForm.elements.mode.addEventListener("change", () => {
  const mode = modelForm.elements.mode.value;
  modelForm.elements.api_key.value = "";
  modelForm.elements.model.value = mode === savedModelSettings.mode ? savedModelSettings.model
    : mode === "ccswitch" ? "claude-sonnet-5" : savedModelSettings.model;
  renderModelFields();
});

modelForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  modelSave.disabled = modelTest.disabled = true;
  try {
    const data = Object.fromEntries(new FormData(modelForm));
    fillModelSettings(await api("/api/settings/model", { method: "PUT", body: JSON.stringify(data) }));
    modelMessage("模型设置已保存。");
  } catch (error) { modelMessage(error.message, true); }
  finally { modelSave.disabled = modelTest.disabled = false; }
});

modelTest.addEventListener("click", async () => {
  modelSave.disabled = modelTest.disabled = true;
  try {
    const result = await api("/api/settings/model/test", { method: "POST" });
    modelMessage(`模型 ${result.model} 已成功响应。`);
  } catch (error) { modelMessage(error.message, true); }
  finally { modelSave.disabled = modelTest.disabled = false; }
});

// ------------------------------------------------------------------ start

$("#sign-out").addEventListener("click", async () => {
  const button = $("#sign-out");
  button.disabled = true;
  try {
    const response = await request("/auth/logout", { method: "POST" });
    if (!response.ok) throw new Error();
    go(routes.login, { replace: true });
  } catch {
    button.disabled = false;
    toast("退出失败，请重试。", true);
  }
});

async function start() {
  const session = await request("/auth/session").catch(() => null);
  if (session?.status === 401) {
    go(routes.login, { replace: true });
    return;
  }
  cloudWorkspace = session?.ok === true;
  if (cloudWorkspace) {
    $("#sign-out").hidden = false;
    $('.tab[data-view="mcp"]').hidden = true;
    $("#sources-description").textContent = "这里显示你正在进行的项目所使用的数据源。云端数据源凭据由工作台运营者管理。";
  }
  show(cloudWorkspace && viewFromPath() === "mcp" ? "projects" : viewFromPath());
  loadProjects();
}

start();
addEventListener("pageshow", (event) => { if (event.persisted && cloudWorkspace) location.reload(); });
setInterval(() => { if (!document.hidden && !$("#view-projects").hidden && !openMenu && !dialog.open) loadProjects(); }, 30000);
if (REDUCED) document.documentElement.classList.add("reduced");
