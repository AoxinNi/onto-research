/* Project list and MCP page. Each card draws a small constellation of its project's busiest nodes. */
import { typeOf } from "/static/vocab.js";

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
  const res = await fetch(url, { headers: { "Content-Type": "application/json" }, ...opts });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.detail || `请求失败（${res.status}）`);
  return body;
}

// ------------------------------------------------------------------ routing

function show(view, push = false) {
  for (const v of document.querySelectorAll(".view")) v.hidden = v.id !== `view-${view}`;
  for (const t of document.querySelectorAll(".tab")) t.setAttribute("aria-selected", String(t.dataset.view === view));
  document.title = `${{ mcp: "MCP 接入", sources: "数据源" }[view] || "本体项目"} · 本体工作台`;
  if (push) history.pushState({ view }, "", view === "projects" ? "/" : `/${view}`);
  if (view === "mcp") loadMcp();
  if (view === "sources") loadSources();
}
for (const t of document.querySelectorAll(".tab")) {
  t.addEventListener("click", (e) => { e.preventDefault(); show(t.dataset.view, true); });
}
const viewFromPath = () => ({ "/mcp": "mcp", "/sources": "sources" })[location.pathname] || "projects";
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
  const open = () => { location.href = `/p/${encodeURIComponent(p.id)}`; };
  const menuBtn = el("button", { type: "button", class: "menu-btn", "aria-label": "更多操作", "aria-haspopup": "menu", "aria-expanded": "false",
    onclick: (e) => { e.stopPropagation(); toggleMenu(p, menuBtn); } },
    svg('<svg viewBox="0 0 16 16"><circle cx="3.5" cy="8" r="1.3" fill="currentColor"/><circle cx="8" cy="8" r="1.3" fill="currentColor"/><circle cx="12.5" cy="8" r="1.3" fill="currentColor"/></svg>'));
  const top = el("div", { class: "badges-top" },
    st.pending > 0 && el("span", { class: "pill review" }, `${st.pending} 项待审核`),
    st.feed_errors > 0 && el("span", { class: "pill bad" }, `${st.feed_errors} 条采集出错`),
  );
  const chips = [["节点", st.nodes], ["关系", st.links], ["数据流", st.feeds], ["新闻 7d", st.news_7d]];
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
    el("button", { type: "button", role: "menuitem", onclick: act(() => { location.href = `/api/projects/${encodeURIComponent(p.id)}/export`; }) }, "导出 JSON"),
    el("button", { type: "button", role: "menuitem", onclick: act(() => copyMcp(p)) }, "复制 MCP 配置"),
    el("button", { type: "button", role: "menuitem", class: p.status === "active" ? "danger" : "",
      onclick: act(() => setStatus(p, p.status === "active" ? "archived" : "active")) }, p.status === "active" ? "归档" : "恢复"),
  );
  btn.parentElement.append(menu);
  btn.setAttribute("aria-expanded", "true");
  openMenu = { menu, btn };
}

async function setStatus(p, status) {
  if (status === "archived" && !confirm(`归档「${p.name}」？归档后停止采集，数据保留，可以随时恢复。`)) return;
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
  $("#f-name").value = p?.name || "";
  $("#f-desc").value = p?.description || "";
  dialog.showModal();
  $("#f-name").focus();
}
$("#create-btn").addEventListener("click", () => openDialog());
$("#dialog-cancel").addEventListener("click", () => dialog.close());
$("#project-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const body = JSON.stringify({ name: $("#f-name").value.trim(), description: $("#f-desc").value.trim() });
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
      setTimeout(() => { location.href = `/p/${encodeURIComponent(p.id)}`; }, 700);
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
  list_sources: "可绑定的数据源及参数（Binance、FRED、mempool、DefiLlama…）。",
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

// ------------------------------------------------------------------ data sources

const CATEGORY_ORDER = ["a-share", "crypto", "onchain", "macro", "news", "social", "internal"];

async function loadSources() {
  try {
    const { providers, categories } = await api("/api/sources");
    const order = [...CATEGORY_ORDER, ...Object.keys(categories).filter((c) => !CATEGORY_ORDER.includes(c))];  // new ones still show
    const groups = order.map((c) => [c, providers.filter((p) => p.category === c)]).filter(([, ps]) => ps.length);
    $("#source-groups").replaceChildren(...groups.map(([c, ps]) => el("section", {},
      el("h2", { class: "group-title" }, categories[c] || c, el("small", {}, String(ps.length))),
      el("div", { class: "grid sources" }, ...ps.map(sourceCard)))));
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
      el("button", { type: "button", class: "link danger", onclick: () => { if (confirm(`清除 ${p.name} 的 API Key？`)) save(""); } }, "清除"))), msg];
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
    p.auth_env && keyRow(p),
    p.sources.length > 0 && el("ul", { class: "src-list" }, ...p.sources.map((s) => el("li", {},
      el("span", {}, s.title), el("code", {}, s.source)))),
    el("div", { class: "src-foot" },
      p.docs ? el("a", { href: p.docs, target: "_blank", rel: "noopener noreferrer" }, "接口文档") : el("span"),
      p.category !== "internal" && btn),
    result,
  );
}

// ------------------------------------------------------------------ start

show(viewFromPath());
loadProjects();
setInterval(() => { if (!document.hidden && !$("#view-projects").hidden && !openMenu && !dialog.open) loadProjects(); }, 30000);
if (REDUCED) document.documentElement.classList.add("reduced");
