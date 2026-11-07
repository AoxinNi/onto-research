/* 本体工作台：三维星图 + 对话 + 审核。图数据来自服务端快照，前端只做增量合并、布局和视觉状态。 */
import { ForceGraph3D, THREE, CSS2DRenderer, CSS2DObject, UnrealBloomPass } from "/static/vendor/graph3d.js";
import { TYPES, LINKS, PROVENANCE, typeOf } from "/static/vocab.js";

// Each workbench page is served at /p/<id>.
const PROJECT = decodeURIComponent((location.pathname.match(/^\/p\/([^/]+)/) || [])[1]);
const q = (url) => `${url}${url.includes("?") ? "&" : "?"}project=${encodeURIComponent(PROJECT)}`;

// ------------------------------------------------------------------ vocabulary


const REDUCED = matchMedia("(prefers-reduced-motion: reduce)").matches;
const HUB_DEGREE = 5;
const DIM = 0.1;

// ------------------------------------------------------------------ dom helpers

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
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const typing = (e) => /^(INPUT|TEXTAREA)$/.test(e.target.tagName) || e.target.isContentEditable;
const clamp = (v, a, b) => Math.min(b, Math.max(a, v));
const smooth = (a, b, x) => { const t = clamp((x - a) / (b - a), 0, 1); return t * t * (3 - 2 * t); };
const easeOutBack = (t) => { const c = 1.6; return 1 + (c + 1) * (t - 1) ** 3 + c * (t - 1) ** 2; };

// Numbers read the way a Chinese market terminal shows them: 亿 for the very large, thousands separators otherwise.
function fmtValue(v, unit) {
  if (v == null || !Number.isFinite(v)) return "—";
  const a = Math.abs(v);
  let s;
  if (a >= 1e8) s = `${(v / 1e8).toLocaleString("zh-CN", { maximumFractionDigits: 2 })} 亿`;
  else if (a >= 1000) s = Math.round(v).toLocaleString("en-US");
  else if (a >= 1) s = v.toFixed(2);
  else s = v.toPrecision(3);
  return unit === "%" ? `${s}%` : s;
}
function fmtChange(c) {
  if (!c) return null;
  const pct = c.pct;
  if (pct == null || !Number.isFinite(pct)) return null;
  const dir = pct > 0.005 ? "up" : pct < -0.005 ? "down" : "flat";
  return { dir, text: `${dir === "up" ? "▲" : dir === "down" ? "▼" : "■"} ${Math.abs(pct) < 10 ? Math.abs(pct).toFixed(2) : Math.abs(pct).toFixed(1)}%` };
}
function ago(iso) {
  if (!iso) return "从未";
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  if (s < 90) return "刚刚";
  if (s < 3600) return `${Math.round(s / 60)} 分钟前`;
  if (s < 86400 * 2) return `${Math.round(s / 3600)} 小时前`;
  return `${Math.round(s / 86400)} 天前`;
}
// Under a node's label: its market data if it has any, otherwise its news heat. Sentiment stays in the detail card.
const primaryData = (n) => {
  const ds = (n.__data || []).filter((d) => d.latest && d.role !== "sentiment");
  return ds.find((d) => d.role !== "heat") || ds[0] || null;
};

const ICON = {
  close: '<svg viewBox="0 0 12 12"><path d="m2.5 2.5 7 7m0-7-7 7" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>',
  go: '<svg class="go" viewBox="0 0 12 12"><path d="M2 6h8M6.5 2.5 10 6 6.5 9.5" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"/></svg>',
  send: '<svg viewBox="0 0 20 20"><path d="M10 15.5v-11M5.5 9 10 4.5 14.5 9" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>',
  busy: '<svg viewBox="0 0 20 20"><circle cx="10" cy="10" r="6" fill="none" stroke="currentColor" stroke-width="1.8" stroke-dasharray="26 12" stroke-linecap="round"/></svg>',
};

// ------------------------------------------------------------------ state

let graph = { nodes: [], links: [] };
const nodes = new Map(); // id -> force-graph node (kept across snapshots so positions survive)
const links = new Map();
let hovered = null;
let selected = null; // { kind: "node" | "link", id }
const typeFilter = new Set();
let showAllLabels = true;
let autoRotate = !REDUCED;
let interacting = false;
let resumeTimer = null;
let firstGraph = true;
let fitOnStop = false;
let fitTimer = null;
function cancelAutoFit() { fitOnStop = false; clearTimeout(fitTimer); }
let busy = false;

// ------------------------------------------------------------------ textures

function canvasTexture(size, draw) {
  const c = document.createElement("canvas");
  c.width = c.height = size;
  draw(c.getContext("2d"), size);
  const tex = new THREE.CanvasTexture(c);
  tex.colorSpace = THREE.SRGBColorSpace;
  return tex;
}
const glowTex = canvasTexture(128, (g, s) => {
  const r = g.createRadialGradient(s / 2, s / 2, 0, s / 2, s / 2, s / 2);
  r.addColorStop(0, "rgba(255,255,255,1)");
  r.addColorStop(0.18, "rgba(255,255,255,.55)");
  r.addColorStop(0.45, "rgba(255,255,255,.12)");
  r.addColorStop(1, "rgba(255,255,255,0)");
  g.fillStyle = r;
  g.fillRect(0, 0, s, s);
});
const ringTex = (dashed) => canvasTexture(256, (g, s) => {
  g.strokeStyle = "#fff";
  g.lineWidth = dashed ? 5 : 4;
  if (dashed) g.setLineDash([14, 12]);
  g.beginPath();
  g.arc(s / 2, s / 2, s / 2 - 8, 0, Math.PI * 2);
  g.stroke();
});
const dashedRing = ringTex(true);
const solidRing = ringTex(false);

const sphereGeo = new THREE.SphereGeometry(1, 32, 24);

// Glassy orb: saturated body, luminous rim, a faint specular point. Honours scene fog for depth.
function orbMaterial() {
  return new THREE.ShaderMaterial({
    uniforms: THREE.UniformsUtils.merge([THREE.UniformsLib.fog, { color: { value: new THREE.Color() }, opacity: { value: 1 } }]),
    vertexShader: `
      #include <fog_pars_vertex>
      varying vec3 vN; varying vec3 vV;
      void main() {
        vec4 mvPosition = modelViewMatrix * vec4(position, 1.0);
        vN = normalize(normalMatrix * normal); vV = normalize(-mvPosition.xyz);
        gl_Position = projectionMatrix * mvPosition;
        #include <fog_vertex>
      }`,
    fragmentShader: `
      uniform vec3 color; uniform float opacity;
      #include <fog_pars_fragment>
      varying vec3 vN; varying vec3 vV;
      void main() {
        vec3 n = normalize(vN), v = normalize(vV);
        float rim = 1.0 - max(dot(n, v), 0.0);
        vec3 l = normalize(vec3(-0.4, 0.6, 0.7));
        float spec = pow(max(dot(reflect(-l, n), v), 0.0), 24.0);
        vec3 c = color * (0.42 + 0.5 * pow(1.0 - rim, 0.6)) + color * pow(rim, 2.2) * 1.1 + vec3(spec * 0.35);
        gl_FragColor = vec4(c, opacity);
        #include <fog_fragment>
      }`,
    transparent: true,
    fog: true,
  });
}
const crystalGeo = new THREE.IcosahedronGeometry(1.25, 0);
const crystalEdges = new THREE.EdgesGeometry(crystalGeo);

// ------------------------------------------------------------------ scene

const labelRenderer = new CSS2DRenderer();
const stage = $("#scene");
const G = new ForceGraph3D(stage, { extraRenderers: [labelRenderer], controlType: "orbit" })
  .width(innerWidth)
  .height(innerHeight)
  .backgroundColor("#05060a")
  .showNavInfo(false)
  .nodeId("id")
  .nodeThreeObject((n) => visual(n).group)
  .nodeThreeObjectExtend(false)
  .nodeLabel(() => "")
  .linkThreeObject((l) => linkVisual(l).line)
  .linkPositionUpdate((obj, { start, end }) => {
    const p = obj.geometry.attributes.position;
    p.setXYZ(0, start.x, start.y, start.z || 0);
    p.setXYZ(1, end.x, end.y, end.z || 0);
    p.needsUpdate = true;
    obj.geometry.computeBoundingSphere();
    return true;
  })
  .linkLabel(linkTooltip)
  .linkHoverPrecision(1.5)
  .linkDirectionalParticles(particleCount)
  .linkDirectionalParticleWidth(1.3)
  .linkDirectionalParticleSpeed(() => 0.0035 + Math.random() * 0.002)
  .linkDirectionalParticleColor((l) => typeOf(nodes.get(srcId(l))?.type).color)
  .linkDirectionalParticleResolution(6)
  .enableNodeDrag(true)
  .cooldownTime(5500)
  .onEngineStop(() => { if (fitOnStop) { fitOnStop = false; fitView(REDUCED ? 0 : 1600, 70); } })
  .d3VelocityDecay(0.32)
  .onNodeHover((n) => { hovered = n ? n.id : null; stage.classList.toggle("hovering", !!n); refreshFocus(); })
  .onLinkHover((l) => { stage.classList.toggle("hovering", !!l); })
  .onNodeClick((n) => select({ kind: "node", id: n.id }, true))
  .onLinkClick((l) => select({ kind: "link", id: l.id }, false))
  .onBackgroundClick(() => select(null))
  .onNodeDrag(() => cancelAutoFit())
  .onNodeDragEnd((n) => { n.fx = n.x; n.fy = n.y; n.fz = n.z; });

window.workbenchGraph = G; // handy from the devtools console

// zoomToFit, but never closer than MIN_VIEW: a graph of two or three nodes should not fill the screen.
const MIN_VIEW = 340;
function fitView(ms = 1000, padding = 70) {
  G.zoomToFit(ms, padding);
  setTimeout(() => {
    const target = controls.target.clone();
    const d = camera.position.distanceTo(target);
    if (d >= MIN_VIEW) return;
    const dir = camera.position.clone().sub(target).normalize();
    G.cameraPosition(target.clone().add(dir.multiplyScalar(MIN_VIEW)), target, REDUCED ? 0 : 700);
  }, ms + 30);
}

const renderer = G.renderer();
renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
labelRenderer.domElement.style.pointerEvents = "none";
const scene = G.scene();
const camera = G.camera();
const controls = G.controls();
scene.fog = new THREE.FogExp2(0x05060a, 0.002);
controls.enableDamping = true;
controls.dampingFactor = 0.08;
controls.rotateSpeed = 0.6;
controls.zoomSpeed = 0.8;
controls.autoRotateSpeed = 0.35;
controls.addEventListener("start", () => {
  interacting = true;
  cancelAutoFit(); // never yank the camera away from someone who is steering it
  clearTimeout(resumeTimer);
  $("#hint").classList.add("gone");
});
controls.addEventListener("end", () => {
  clearTimeout(resumeTimer);
  resumeTimer = setTimeout(() => { interacting = false; }, 5000);
});
camera.position.set(0, 120, 1100);

const bloom = new UnrealBloomPass(new THREE.Vector2(innerWidth, innerHeight), 0.55, 0.5, 0.32);
G.postProcessingComposer().addPass(bloom);

G.d3Force("charge").strength((n) => -120 - 40 * Math.sqrt(n.__deg || 0)).distanceMax(700);
G.d3Force("link").distance((l) => 52 + 9 * Math.sqrt((nodes.get(srcId(l))?.__deg || 1) + (nodes.get(dstId(l))?.__deg || 1)));
// World layer floats above, the company's own layer settles below it.
let forceNodes = [];
const layerForce = Object.assign((alpha) => {
  for (const n of forceNodes) {
    n.vy += ((n.layer === "company" ? -80 : 18) - n.y) * alpha * 0.05;
    // Gentle gravity keeps unlinked nodes from drifting off to the edge of the charge range.
    const g = (n.__deg ? 0.012 : 0.05) * alpha;
    n.vx -= n.x * g;
    if (n.vz !== undefined) n.vz -= (n.z || 0) * g;
    if (!n.__deg) n.vy -= n.y * g;
  }
}, { initialize: (ns) => { forceNodes = ns; } });
G.d3Force("layer", layerForce);

// Distant stars give the camera something to move against.
(() => {
  const count = 2200;
  const pos = new Float32Array(count * 3);
  const col = new Float32Array(count * 3);
  const warm = new THREE.Color("#ffcf9a"), cool = new THREE.Color("#a9b8ff"), white = new THREE.Color("#ffffff");
  for (let i = 0; i < count; i++) {
    const r = 1400 + Math.random() * 1800;
    const th = Math.random() * Math.PI * 2, ph = Math.acos(2 * Math.random() - 1);
    pos.set([r * Math.sin(ph) * Math.cos(th), r * Math.cos(ph), r * Math.sin(ph) * Math.sin(th)], i * 3);
    const c = Math.random() < 0.15 ? warm : Math.random() < 0.3 ? cool : white;
    const k = 0.35 + Math.random() * 0.65;
    col.set([c.r * k, c.g * k, c.b * k], i * 3);
  }
  const geo = new THREE.BufferGeometry();
  geo.setAttribute("position", new THREE.BufferAttribute(pos, 3));
  geo.setAttribute("color", new THREE.BufferAttribute(col, 3));
  const stars = new THREE.Points(geo, new THREE.PointsMaterial({ size: 1.6, sizeAttenuation: false, vertexColors: true, transparent: true, opacity: 0.8, fog: false, depthWrite: false }));
  stars.name = "stars";
  scene.add(stars);
})();

// ------------------------------------------------------------------ node + link visuals

function radius(n) { return 3.2 + Math.sqrt(n.__deg || 0) * 2.1; }

function visual(n) {
  if (n.__v) return n.__v;
  const group = new THREE.Group();
  const coreMat = orbMaterial();
  // The company's own layer reads as wireframe crystals; the market world as solid orbs.
  let core = new THREE.Mesh(sphereGeo, coreMat);
  if (n.layer === "company") {
    const heart = core;
    heart.scale.setScalar(0.42);
    core = new THREE.Group().add(
      new THREE.Mesh(crystalGeo, new THREE.MeshBasicMaterial({ transparent: true })),
      new THREE.LineSegments(crystalEdges, new THREE.LineBasicMaterial({ transparent: true })),
      heart,
    );
  }
  const halo = new THREE.Sprite(new THREE.SpriteMaterial({ map: glowTex, blending: THREE.AdditiveBlending, transparent: true, depthWrite: false }));
  const ring = new THREE.Sprite(new THREE.SpriteMaterial({ map: dashedRing, transparent: true, depthWrite: false }));
  ring.visible = false;
  const elLabel = el("div", { class: "node-label" }, el("span", { class: "n" }), el("span", { class: "t" }), el("span", { class: "v" }));
  const label = new CSS2DObject(elLabel);
  label.center.set(0.5, 0);
  group.add(core, halo, ring, label);
  n.__v = { group, core, coreMat, halo, ring, label, el: elLabel, alpha: 0, born: performance.now(), spin: Math.random() * Math.PI };
  styleNode(n);
  return n.__v;
}

function styleNode(n) {
  const v = n.__v;
  if (!v) return;
  const t = typeOf(n.type);
  const color = new THREE.Color(t.color);
  const r = radius(n);
  v.r = r;
  v.coreMat.uniforms.color.value.copy(color);
  v.core.traverse((o) => { if (o.material && o.material !== v.coreMat) o.material.color.copy(color); });
  v.core.scale.setScalar(r);
  v.halo.material.color.copy(color);
  v.halo.scale.setScalar(r * (n.layer === "company" ? 4.5 : 5.5));
  v.label.position.set(0, -r * 1.05, 0);
  v.el.style.setProperty("--c", t.color);
  v.el.querySelector(".n").textContent = n.name;
  v.el.querySelector(".t").textContent = t.label;
  v.el.classList.toggle("hub", (n.__deg || 0) >= HUB_DEGREE);
  v.el.classList.toggle("pending", !!n.pending);
  const d = primaryData(n);
  const val = v.el.querySelector(".v");
  val.replaceChildren();
  if (d) {
    const ch = fmtChange(d.change);
    if (d.role === "heat") val.append(el("span", { class: "k" }, "热度"));
    val.append(fmtValue(d.latest.value, d.unit));
    if (ch) val.append(el("em", { class: ch.dir }, ch.text));
  }
  v.el.classList.toggle("has-data", !!d);
  v.el.classList.toggle("stale", !!d?.stale);
}

function linkVisual(l) {
  if (l.__v) return l.__v;
  const geo = new THREE.BufferGeometry();
  geo.setAttribute("position", new THREE.BufferAttribute(new Float32Array(6), 3));
  geo.setAttribute("color", new THREE.BufferAttribute(new Float32Array(6), 3));
  const mat = new THREE.LineBasicMaterial({ vertexColors: true, transparent: true, opacity: 0, depthWrite: false, blending: THREE.AdditiveBlending });
  const line = new THREE.Line(geo, mat);
  l.__v = { line, mat, alpha: 0 };
  styleLink(l);
  return l.__v;
}

function styleLink(l) {
  const v = l.__v;
  if (!v) return;
  const a = new THREE.Color(typeOf(nodes.get(srcId(l))?.type).color);
  const b = new THREE.Color(typeOf(nodes.get(dstId(l))?.type).color);
  const c = v.line.geometry.attributes.color;
  c.setXYZ(0, a.r, a.g, a.b);
  c.setXYZ(1, b.r, b.g, b.b);
  c.needsUpdate = true;
}

const srcId = (l) => (typeof l.source === "object" ? l.source.id : l.source);
const dstId = (l) => (typeof l.target === "object" ? l.target.id : l.target);
// Links glow with their evidence: the more the data and the news back a link, the brighter it is and the more
// signal flows along it. A link that rests only on the model's background knowledge stays dim and still.
const strengthOf = (l) => l.__ev?.strength || 0;
const baseLinkAlpha = (l) => {
  if (l.pending) return 0.28;
  const s = strengthOf(l);
  if (s > 0) return 0.18 + 0.55 * Math.min(1, s * 1.4);
  return l.dashed ? 0.16 : 0.32;
};

function particleCount(l) {
  if (REDUCED) return 0;
  const f = focusSet();
  if (f && !(f.has(srcId(l)) && f.has(dstId(l)))) return 0;
  const s = strengthOf(l);
  if (s >= 0.12) return 1 + Math.round(Math.min(1, s * 1.4) * 2);
  return l.dashed || l.pending ? 0 : 1;
}

function linkTooltip(l) {
  const a = nodes.get(srcId(l)), b = nodes.get(dstId(l));
  const ev = l.__ev?.items?.find((e) => e.verdict !== "insufficient");
  return `${esc(a?.name)}<span class="rel">${esc(LINKS[l.type] || l.type)} →</span>${esc(b?.name)}`
    + (l.rationale ? `<small>${esc(l.rationale)}</small>` : "")
    + (ev ? `<small class="ev-line">证据 ${strengthOf(l).toFixed(2)} · ${esc(ev.summary)}</small>` : "");
}

// ------------------------------------------------------------------ data merge

function spawnPosition(id, rawLinks) {
  const anchors = [];
  for (const l of rawLinks) {
    const other = l.from_id === id ? l.to_id : l.to_id === id ? l.from_id : null;
    const n = other && nodes.get(other);
    if (n && n.x != null) anchors.push(n);
  }
  const j = () => (Math.random() - 0.5) * 40;
  if (!anchors.length) return { x: j() * 4, y: j() * 4, z: j() * 4 };
  const avg = (k) => anchors.reduce((s, n) => s + (n[k] || 0), 0) / anchors.length;
  return { x: avg("x") + j(), y: avg("y") + j(), z: avg("z") + j() };
}

function applyGraph(next) {
  graph = next;
  const deg = new Map();
  for (const l of next.links) { deg.set(l.from_id, (deg.get(l.from_id) || 0) + 1); deg.set(l.to_id, (deg.get(l.to_id) || 0) + 1); }

  const keepNodes = new Set();
  let stagger = 0;
  for (const raw of next.nodes) {
    keepNodes.add(raw.id);
    let n = nodes.get(raw.id);
    if (!n) {
      n = { id: raw.id, ...spawnPosition(raw.id, next.links) };
      nodes.set(raw.id, n);
      if (n.__v) n.__v.born = performance.now();
      n.__bornAt = performance.now() + (firstGraph ? stagger++ * 18 : 0);
    }
    const layerChanged = n.layer && n.layer !== raw.layer;
    const data = (next.data || {})[raw.id] || [];
    const newest = data.map((d) => d.latest?.ts).filter(Boolean).sort().pop();
    if (newest && n.__newest && newest !== n.__newest) n.__pulseAt = performance.now(); // fresh data: the node breathes once
    n.__newest = newest;
    Object.assign(n, { name: raw.name, type: raw.type, layer: raw.layer, pending: raw.pending, raw, __deg: deg.get(raw.id) || 0, __data: data });
    if (layerChanged && n.__v) { n.__v.el.remove(); n.__v = null; }
    if (n.__v) styleNode(n);
  }
  for (const [id, n] of nodes) if (!keepNodes.has(id)) { n.__v?.el.remove(); nodes.delete(id); }

  const keepLinks = new Set();
  for (const raw of next.links) {
    keepLinks.add(raw.id);
    let l = links.get(raw.id);
    if (l && (srcId(l) !== raw.from_id || dstId(l) !== raw.to_id)) { links.delete(raw.id); l = null; }
    if (!l) { l = { id: raw.id, source: raw.from_id, target: raw.to_id }; links.set(raw.id, l); }
    Object.assign(l, { type: raw.type, pending: raw.pending, dashed: (raw.provenance || {}).kind === "llm_prior", rationale: raw.rationale, raw,
                       __ev: (next.evidence || {})[raw.id] || null });
    if (l.__v) styleLink(l);
  }
  for (const id of [...links.keys()]) if (!keepLinks.has(id)) links.delete(id);

  G.graphData({ nodes: [...nodes.values()], links: [...links.values()] });

  const empty = next.nodes.length === 0;
  $("#empty").hidden = !empty;
  for (const s of ["#legend", "#dock", "#search", "#hint"]) $(s).hidden = empty;
  countTo($("#stat-nodes"), next.nodes.length);
  countTo($("#stat-links"), next.links.length);
  countTo($("#stat-feeds"), Object.values(next.data || {}).reduce((s, ds) => s + ds.length, 0));
  renderLegend();
  if (selected && !(selected.kind === "node" ? nodes.has(selected.id) : links.has(selected.id))) selected = null;
  renderDetail();
  refreshFocus();

  if (!empty && firstGraph) {
    firstGraph = false;
    // A first sweep once the layout has opened up, then a settle when the simulation cools.
    fitOnStop = true;
    fitTimer = setTimeout(() => fitView(REDUCED ? 0 : 2400, 70), 1400);
  } else if (empty) {
    firstGraph = true;
  }
}

// ------------------------------------------------------------------ focus + selection

function focusSet() {
  if (reportFocus) return reportFocus.nodes;
  const focal = hovered || (selected?.kind === "node" ? selected.id : null);
  if (focal) {
    const s = new Set([focal]);
    for (const l of links.values()) {
      if (srcId(l) === focal) s.add(dstId(l));
      if (dstId(l) === focal) s.add(srcId(l));
    }
    return s;
  }
  if (selected?.kind === "link") {
    const l = links.get(selected.id);
    return l ? new Set([srcId(l), dstId(l)]) : null;
  }
  if (typeFilter.size) return new Set([...nodes.values()].filter((n) => typeFilter.has(n.type)).map((n) => n.id));
  return null;
}

let lastFocusKey = "";
function refreshFocus() {
  markCitingSentences();
  const f = focusSet();
  const key = f ? [...f].sort().join(",") + (selected?.id || "") + (reportFocus ? [...reportFocus.links].join(",") : "") : "";
  if (key === lastFocusKey) return;
  lastFocusKey = key;
  G.linkDirectionalParticles(particleCount);
}

function select(sel, fly) {
  selected = sel;
  renderDetail();
  refreshFocus();
  if (sel?.kind === "node" && fly) flyTo(nodes.get(sel.id));
}

function flyTo(n, ms = 1300) {
  if (!n || n.x == null) return;
  const dist = 130 + radius(n) * 8;
  const z = n.z || 0;
  const dir = new THREE.Vector3().subVectors(camera.position, new THREE.Vector3(n.x, n.y, z)).normalize();
  if (G.numDimensions() === 2) dir.set(0, 0, 1);
  G.cameraPosition({ x: n.x + dir.x * dist, y: n.y + dir.y * dist, z: z + dir.z * dist }, { x: n.x, y: n.y, z }, REDUCED ? 0 : ms);
}

// ------------------------------------------------------------------ frame loop

// Keep the visual centre of the scene in the space the floating panel leaves free.
const viewShift = { x: 0, y: 0 };
const panelWidth = () => parseFloat(getComputedStyle(document.documentElement).getPropertyValue("--panel-w")) || 0;
function targetShift() {
  const collapsed = document.body.classList.contains("panel-collapsed");
  if (innerWidth <= 820) {
    // Phone: the chat is a bottom sheet, so centre the graph in the band between the top chrome and the sheet.
    const sheet = collapsed ? 0 : $("#panel").offsetHeight + 14;
    return { x: 0, y: (sheet - 110) / 2 };
  }
  const right = collapsed ? 0 : panelWidth() + 20;
  const left = selected ? 360 : 0;
  return { x: (right - left) / 2, y: 0 };
}
const tmp = new THREE.Vector3();
let frameNo = 0;

// Greedy screen-space placement: important labels claim their space first, the rest give way.
function resolveLabelCollisions(f) {
  const focal = hovered || (selected?.kind === "node" ? selected.id : null);
  const cands = [];
  for (const n of nodes.values()) {
    const v = n.__v;
    if (!v || v.want < 0.08) { if (v) v.hiddenByCollision = false; continue; }
    tmp.set(n.x || 0, n.y || 0, n.z || 0).project(camera);
    if (tmp.z > 1) { v.hiddenByCollision = true; continue; }
    const x = (tmp.x + 1) / 2 * innerWidth, y = (1 - tmp.y) / 2 * innerHeight;
    const hub = (n.__deg || 0) >= HUB_DEGREE;
    const w = [...(n.name || "")].reduce((s, c) => s + (c.charCodeAt(0) > 255 ? (hub ? 14 : 12) : (hub ? 8 : 7)), 0) + 8;
    const prio = (n.id === focal ? 1e6 : 0) + (f && f.has(n.id) ? 1e4 : 0) + (n.__deg || 0) * 10 + v.want * 5;
    const tall = n.__data?.some((d) => d.latest) ? 16 : 0;
    cands.push({ v, prio, x0: x - w / 2, x1: x + w / 2, y0: y + 6, y1: y + 36 + tall });
  }
  cands.sort((a, b) => b.prio - a.prio);
  const placed = [];
  for (const c of cands) {
    const hit = placed.some((p) => c.x0 < p.x1 && c.x1 > p.x0 && c.y0 < p.y1 && c.y1 > p.y0);
    c.v.hiddenByCollision = hit;
    if (!hit) placed.push(c);
  }
}

function frame(now) {
  requestAnimationFrame(frame);
  const f = focusSet();
  const camDist = camera.position.distanceTo(controls.target);

  controls.autoRotate = autoRotate && !interacting && !hovered && !selected && G.numDimensions() === 3;
  scene.fog.density = 0.62 / Math.max(camDist, 120);
  const stars = scene.getObjectByName("stars");
  if (stars) stars.rotation.y = now * 0.000006;

  const shift = targetShift();
  if (Math.abs(shift.x - viewShift.x) + Math.abs(shift.y - viewShift.y) > 0.3 || camera.view == null) {
    viewShift.x += (shift.x - viewShift.x) * 0.12;
    viewShift.y += (shift.y - viewShift.y) * 0.12;
    camera.setViewOffset(innerWidth, innerHeight, viewShift.x, viewShift.y, innerWidth, innerHeight);
  }

  for (const n of nodes.values()) {
    const v = n.__v;
    if (!v) continue;
    const isSel = selected?.kind === "node" && selected.id === n.id;
    const inFocus = !f || f.has(n.id);
    tmp.set(n.x || 0, n.y || 0, n.z || 0);
    const d = camera.position.distanceTo(tmp);
    const near = smooth(v.r * 3, v.r * 12, d); // nodes brushing the lens fade instead of filling the screen
    const target = (inFocus ? 1 : DIM) * (n.pending ? 0.7 : 1) * near;
    v.alpha += (target - v.alpha) * 0.14;

    const age = now - (n.__bornAt ?? 0);
    const grow = REDUCED ? 1 : age < 0 ? 0 : age > 900 ? 1 : easeOutBack(age / 900);
    v.group.scale.setScalar(Math.max(grow, 0.0001));

    const sincePulse = n.__pulseAt ? (now - n.__pulseAt) / 1400 : 1;
    const flash = sincePulse < 1 ? 1 + 2.2 * (1 - sincePulse) ** 2 : 1;
    const pulse = (n.pending ? 0.65 + 0.35 * Math.sin(now * 0.004 + v.spin) : 1) * flash;
    v.coreMat.uniforms.opacity.value = v.alpha;
    v.core.traverse((o) => { if (o.material && o.material !== v.coreMat) o.material.opacity = v.alpha * (o.isLineSegments ? 0.95 : 0.18); });
    v.halo.material.opacity = v.alpha * 0.32 * pulse * (isSel || hovered === n.id ? 1.5 : 1);

    v.ring.visible = n.pending || isSel;
    if (v.ring.visible) {
      v.ring.material.map = isSel ? solidRing : dashedRing;
      v.ring.material.color.set(isSel ? "#f7931a" : typeOf(n.type).color);
      v.ring.material.opacity = (isSel ? 0.95 : 0.6 * pulse) * Math.max(v.alpha, 0.3);
      v.ring.material.rotation = now * (isSel ? 0.0004 : 0.0007) + v.spin;
      v.ring.scale.setScalar(v.r * (isSel ? 3.4 + 0.15 * Math.sin(now * 0.003) : 3));
    }

    // Labels fade with depth so the front of the graph reads first.
    const depth = 1 - smooth(camDist * 0.7, camDist * 1.55, d) * (f && f.has(n.id) ? 0.2 : 0.85);
    const wanted = f ? f.has(n.id) : showAllLabels || (n.__deg || 0) >= HUB_DEGREE;
    v.want = wanted ? clamp(v.alpha * depth, 0, 1) * (grow >= 0.6 ? 1 : 0) : 0;
    const op = v.hiddenByCollision ? 0 : v.want;
    if (Math.abs((v.lastOp ?? -1) - op) > 0.02) { v.el.style.opacity = op.toFixed(2); v.lastOp = op; }
  }

  if (++frameNo % 6 === 0) resolveLabelCollisions(f);

  for (const l of links.values()) {
    const v = l.__v;
    if (!v) continue;
    const s = srcId(l), t = dstId(l);
    const inFocus = !f || (reportFocus ? reportFocus.links.has(l.id) || (f.has(s) && f.has(t))
      : f.has(s) && f.has(t) && (!hovered && selected?.kind !== "node" ? true : [hovered, selected?.id].some((x) => x === s || x === t)));
    const isSel = selected?.kind === "link" && selected.id === l.id;
    const target = isSel ? 1 : inFocus ? baseLinkAlpha(l) * (f ? 1.7 : 1) : 0.03;
    v.alpha += (target - v.alpha) * 0.14;
    const ages = Math.min(now - (nodes.get(s)?.__bornAt ?? 0), now - (nodes.get(t)?.__bornAt ?? 0));
    v.mat.opacity = v.alpha * (REDUCED ? 1 : smooth(300, 1100, ages));
  }
}
requestAnimationFrame(frame);

addEventListener("resize", () => {
  G.width(innerWidth).height(innerHeight);
  bloom.setSize(innerWidth, innerHeight);
  camera.setViewOffset(innerWidth, innerHeight, viewShift.x, viewShift.y, innerWidth, innerHeight);
});

// ------------------------------------------------------------------ chrome: stats, legend, dock

function countTo(node, value) {
  const from = Number(node.dataset.v || 0);
  node.dataset.v = value;
  if (REDUCED || from === value) { node.textContent = value; return; }
  const start = performance.now();
  const step = (now) => {
    const t = Math.min(1, (now - start) / 700);
    node.textContent = Math.round(from + (value - from) * (1 - (1 - t) ** 3));
    if (t < 1) requestAnimationFrame(step);
  };
  requestAnimationFrame(step);
}

function renderLegend() {
  const legend = $("#legend");
  const counts = new Map();
  for (const n of graph.nodes) counts.set(n.type, (counts.get(n.type) || 0) + 1);
  for (const t of [...typeFilter]) if (!counts.has(t)) typeFilter.delete(t);
  const order = Object.keys(TYPES);
  const present = [...counts.keys()].sort((a, b) => (order.indexOf(a) + 1 || 99) - (order.indexOf(b) + 1 || 99));
  legend.replaceChildren(...present.map((type) => {
    const t = typeOf(type);
    const company = graph.nodes.find((n) => n.type === type)?.layer === "company";
    return el("button", {
      type: "button", class: "chip", style: `--c:${t.color}`, "aria-pressed": String(typeFilter.has(type)),
      onclick: () => { typeFilter.has(type) ? typeFilter.delete(type) : typeFilter.add(type); renderLegend(); refreshFocus(); },
    }, el("span", { class: `dot${company ? " sq" : ""}` }), t.label, el("span", { class: "n" }, String(counts.get(type))));
  }));
  if (graph.links.length) {
    legend.append(el("div", { class: "keys" },
      el("span", { class: "chip key", title: "数据联动或新闻共现支持这条关系，越亮证据越强" }, el("span", { class: "ln flow" }), "有证据"),
      el("span", { class: "chip key", title: "只来自模型常识，还没有数据或新闻证据" }, el("span", { class: "ln dim" }), "仅模型常识"),
    ));
  }
  legend.classList.toggle("filtering", typeFilter.size > 0);
}

let refreshing = false;
let lastRefresh = null;
async function refreshData() {
  if (refreshing) return;
  refreshing = true;
  const btn = $('#dock [data-action="refresh"]');
  btn.classList.add("spinning");
  btn.disabled = true;
  try {
    const res = await fetch(q("/api/refresh"), { method: "POST" });
    const r = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(r.detail || `刷新失败（${res.status}）`);
    lastRefresh = new Date();
    btn.dataset.tip = `立即刷新数据 · U（上次 ${lastRefresh.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" })}）`;
    // The updated graph arrives over the WebSocket; nodes with new points pulse on their own.
    const parts = [`${r.series} 条数据流`, r.points ? `新增或修正 ${r.points} 个数据点` : "数据已是最新"];
    toast(`已刷新 · ${parts.join("，")} · ${r.seconds}s；新闻在后台拉取`);
    if (r.errors?.length) toast(`${r.errors.length} 条采集失败：${r.errors.map((e) => e.series).join("、")}`, true);
  } catch (e) {
    toast(e.message, true);
  } finally {
    refreshing = false;
    btn.classList.remove("spinning");
    btn.disabled = false;
  }
}

const dockActions = {
  refresh: refreshData,
  timeline: () => setTimelineOpen($("#timeline").hidden),
  fit: () => { select(null); fitView(REDUCED ? 0 : 1000, 90); },
  rotate: () => { autoRotate = !autoRotate; interacting = false; syncDock(); toast(autoRotate ? "自动环绕已开启" : "自动环绕已关闭"); },
  labels: () => { showAllLabels = !showAllLabels; syncDock(); toast(showAllLabels ? "显示全部标签" : "只显示枢纽和聚焦节点的标签"); },
  dim3: () => setDimensions(3),
  dim2: () => setDimensions(2),
  panel: () => { document.body.classList.toggle("panel-collapsed"); syncDock(); },
};
function syncDock() {
  const q = (a) => $(`#dock [data-action="${a}"]`);
  q("rotate").setAttribute("aria-pressed", String(autoRotate));
  q("rotate").disabled = G.numDimensions() === 2;
  q("labels").setAttribute("aria-pressed", String(showAllLabels));
  const collapsed = document.body.classList.contains("panel-collapsed");
  q("panel").setAttribute("aria-pressed", String(collapsed));
  q("panel").dataset.tip = collapsed ? "展开对话 · \\" : "收起对话 · \\";
  q("dim3").setAttribute("aria-checked", String(G.numDimensions() === 3));
  q("dim2").setAttribute("aria-checked", String(G.numDimensions() === 2));
}
function setDimensions(d) {
  if (G.numDimensions() === d) return;
  for (const n of nodes.values()) {
    n.fz = undefined;
    if (d === 3) { n.z = (Math.random() - 0.5) * 60; n.vz = 0; }
  }
  G.numDimensions(d);
  controls.enableRotate = d === 3;
  // Flat view has nothing to orbit, so the primary drag pans the canvas instead.
  controls.mouseButtons.LEFT = d === 3 ? THREE.MOUSE.ROTATE : THREE.MOUSE.PAN;
  controls.touches.ONE = d === 3 ? THREE.TOUCH.ROTATE : THREE.TOUCH.PAN;
  controls.screenSpacePanning = true;
  $("#hint").textContent = d === 3 ? "拖拽旋转 · 右键平移 · 滚轮缩放 · 点击节点聚焦" : "拖拽平移 · 滚轮缩放 · 拖动节点可固定位置";
  $("#hint").classList.remove("gone");
  syncDock();
  const dist = Math.max(camera.position.distanceTo(controls.target), 260);
  G.cameraPosition(d === 2 ? { x: controls.target.x, y: controls.target.y, z: dist } : { x: dist * 0.5, y: dist * 0.25, z: dist * 0.8 }, controls.target.clone(), REDUCED ? 0 : 1100);
  fitOnStop = true;
  clearTimeout(fitTimer);
  fitTimer = setTimeout(() => fitView(REDUCED ? 0 : 900, 70), 1600);
}
$("#reopen").addEventListener("click", () => dockActions.panel());
$("#dock").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-action]");
  if (b) dockActions[b.dataset.action]();
});
syncDock();

// Tooltips for icon buttons.
const tip = $("#tip");
document.addEventListener("pointerover", (e) => {
  const t = e.target.closest?.("[data-tip]");
  if (!t) return;
  tip.textContent = t.dataset.tip;
  tip.hidden = false;
  const r = t.getBoundingClientRect(), w = tip.offsetWidth;
  const above = r.top > 60;
  tip.style.left = `${clamp(r.left + r.width / 2 - w / 2, 8, innerWidth - w - 8)}px`;
  tip.style.top = `${above ? r.top - tip.offsetHeight - 8 : r.bottom + 8}px`;
});
document.addEventListener("pointerout", (e) => { if (e.target.closest?.("[data-tip]")) tip.hidden = true; });
document.addEventListener("pointerdown", () => { tip.hidden = true; });

function toast(text, bad = false) {
  const t = el("div", { class: `toast${bad ? " bad" : ""}` }, text);
  $("#toasts").append(t);
  setTimeout(() => { t.classList.add("out"); t.addEventListener("animationend", () => t.remove()); }, bad ? 4200 : 2200);
}

// ------------------------------------------------------------------ search

const searchInput = $("#search-input"), results = $("#search-results");
let hits = [], cursor = 0;
function runSearch() {
  const q = searchInput.value.trim().toLowerCase();
  if (!q) { results.hidden = true; hits = []; return; }
  hits = graph.nodes
    .map((n) => {
      const names = [n.name, ...(n.aliases || [])].map((s) => s.toLowerCase());
      const score = names.some((s) => s === q) ? 0 : names.some((s) => s.startsWith(q)) ? 1 : names.some((s) => s.includes(q)) ? 2 : 9;
      return { n, score };
    })
    .filter((h) => h.score < 9)
    .sort((a, b) => a.score - b.score || (nodes.get(b.n.id)?.__deg || 0) - (nodes.get(a.n.id)?.__deg || 0))
    .slice(0, 8)
    .map((h) => h.n);
  cursor = 0;
  renderHits();
}
function renderHits() {
  results.hidden = false;
  if (!hits.length) { results.replaceChildren(el("li", { class: "none" }, "没有匹配的节点")); return; }
  results.replaceChildren(...hits.map((n, i) => {
    const t = typeOf(n.type);
    return el("li", {
      role: "option", "aria-selected": String(i === cursor),
      onmouseenter: () => { cursor = i; renderHits(); },
      onmousedown: (e) => { e.preventDefault(); pick(n); },
    }, el("span", { class: "dot", style: `background:${t.color};color:${t.color}` }), n.name, el("span", { class: "ty" }, t.label));
  }));
}
function pick(n) {
  searchInput.value = "";
  results.hidden = true;
  searchInput.blur();
  select({ kind: "node", id: n.id }, true);
}
searchInput.addEventListener("input", runSearch);
searchInput.addEventListener("keydown", (e) => {
  if (e.key === "ArrowDown" || e.key === "ArrowUp") {
    e.preventDefault();
    if (hits.length) { cursor = (cursor + (e.key === "ArrowDown" ? 1 : -1) + hits.length) % hits.length; renderHits(); }
  } else if (e.key === "Enter" && hits[cursor]) { e.preventDefault(); pick(hits[cursor]); }
  else if (e.key === "Escape") { searchInput.value = ""; results.hidden = true; searchInput.blur(); }
});
searchInput.addEventListener("blur", () => setTimeout(() => { results.hidden = true; }, 100));

// ------------------------------------------------------------------ detail

function provenanceText(p) {
  if (!p) return "未知";
  return [PROVENANCE[p.kind] || p.kind, p.ref, p.note].filter(Boolean).join(" · ");
}

function renderDetail() {
  const box = $("#detail");
  document.body.classList.toggle("inspecting", !!selected);
  if (!selected) { box.hidden = true; return; }
  const raw = selected.kind === "node" ? nodes.get(selected.id)?.raw : links.get(selected.id)?.raw;
  if (!raw) { box.hidden = true; return; }
  const wasHidden = box.hidden;
  box.replaceChildren(el("button", { type: "button", class: "close", "aria-label": "关闭", onclick: () => select(null) }, svg(ICON.close)));
  if (selected.kind === "node") renderNode(box, raw); else renderLink(box, raw);
  box.hidden = false;
  if (!wasHidden) { box.style.animation = "none"; void box.offsetWidth; box.style.animation = ""; }
  box.scrollTop = 0;
}

function renderNode(box, n) {
  const t = typeOf(n.type);
  box.style.setProperty("--c", t.color);
  box.append(el("div", { class: "kind" }, el("span", { class: "dot" }), `${t.label} · ${n.type}`));
  box.append(el("h2", {}, n.name));
  if (n.aliases?.length) box.append(el("div", { class: "aliases" }, n.aliases.join(" / ")));
  box.append(el("div", { class: "badges" },
    el("span", { class: "badge" }, n.layer === "company" ? "公司层" : "世界层"),
    n.pending && el("span", { class: "badge warn" }, "待审核"),
    el("span", { class: "badge" }, provenanceText(n.provenance)),
  ));
  if (n.description) box.append(el("p", { class: "desc" }, n.description));
  const data = nodes.get(n.id)?.__data || [];
  if (data.length) {
    box.append(el("h3", {}, el("span", {}, "数据"), el("span", {}, String(data.length))));
    for (const d of data) box.append(dataCard(d));
  }
  box.append(newsSection(n.id));
  const props = Object.entries(n.props || {});
  if (props.length) {
    box.append(el("h3", {}, "属性"));
    const dl = el("dl");
    for (const [k, v] of props) dl.append(el("dt", {}, k), el("dd", {}, Array.isArray(v) ? v.join("、") : String(v)));
    box.append(dl);
  }
  const rels = graph.links.filter((l) => l.from_id === n.id || l.to_id === n.id);
  if (rels.length) {
    box.append(el("h3", {}, el("span", {}, "关系"), el("span", {}, String(rels.length))));
    const ul = el("ul", { class: "rels" });
    for (const l of rels) {
      const out = l.from_id === n.id;
      const other = nodes.get(out ? l.to_id : l.from_id);
      ul.append(el("li", {}, el("button", {
        type: "button",
        onclick: () => select({ kind: "node", id: other.id }, true),
        onmouseenter: () => { hovered = other?.id; refreshFocus(); },
        onmouseleave: () => { hovered = null; refreshFocus(); },
      },
        el("span", { class: "dir" }, out ? "→" : "←"),
        el("span", { class: "who" }, el("em", {}, LINKS[l.type] || l.type), other?.name ?? "?",
          evidencePill(links.get(l.id))),
        svg(ICON.go),
        l.rationale && el("span", { class: "why" }, l.rationale),
      )));
    }
    box.append(ul);
  }
}

function sparkline(values, color) {
  const w = 296, h = 56, pad = 3;
  if (values.length < 2) return el("div", { class: "spark empty" }, "数据积累中");
  const min = Math.min(...values), max = Math.max(...values), span = max - min || 1;
  const pts = values.map((v, i) => [pad + (i / (values.length - 1)) * (w - pad * 2), pad + (1 - (v - min) / span) * (h - pad * 2)]);
  const line = pts.map(([x, y], i) => `${i ? "L" : "M"}${x.toFixed(1)},${y.toFixed(1)}`).join("");
  const id = `g${Math.random().toString(36).slice(2, 8)}`;
  const [lx, ly] = pts[pts.length - 1];
  return svg(`<svg class="spark" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" role="img" aria-label="走势">
    <defs><linearGradient id="${id}" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="${color}" stop-opacity=".28"/><stop offset="1" stop-color="${color}" stop-opacity="0"/></linearGradient></defs>
    <path d="${line}L${(w - pad).toFixed(1)},${h}L${pad},${h}Z" fill="url(#${id})"/>
    <path d="${line}" fill="none" stroke="${color}" stroke-width="1.5" vector-effect="non-scaling-stroke" stroke-linejoin="round"/>
    <circle cx="${lx.toFixed(1)}" cy="${ly.toFixed(1)}" r="2.6" fill="${color}"/></svg>`);
}

function dataCard(d) {
  const ch = fmtChange(d.change);
  const color = ch?.dir === "down" ? "#ff6b6b" : ch?.dir === "up" ? "#57e39b" : "#a3acbd";
  const health = d.status === "error" ? ["bad", "采集出错"] : d.stale ? ["warn", "数据陈旧"] : d.latest ? ["ok", "正常"] : ["warn", "等待首次采集"];
  return el("div", { class: "data-card" },
    el("div", { class: "dc-head" },
      el("span", { class: "dc-label" }, d.label),
      el("span", { class: "dc-role" }, d.role),
      d.pending && el("span", { class: "badge warn" }, "待审核"),
    ),
    el("div", { class: "dc-value" },
      el("strong", {}, d.latest ? fmtValue(d.latest.value, d.unit) : "—"),
      d.unit && d.unit !== "%" && el("span", { class: "unit" }, d.unit),
      ch && el("em", { class: ch.dir }, `${ch.text} · ${d.change.window}`),
    ),
    sparkline(d.spark || [], color),
    el("div", { class: "dc-foot" },
      el("span", { class: `health ${health[0]}` }, health[1]),
      el("span", {}, !d.latest ? "" : d.latest.live ? `实时 · 本根 K 线未收盘`
        : `数据 ${new Date(d.latest.ts).toLocaleString("zh-CN", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" })}`),
      el("span", {}, `采集 ${ago(d.last_fetch_at)}`),
    ),
    d.status === "error" && d.last_error && el("div", { class: "dc-error" }, d.last_error),
  );
}

// News is fetched when a node is opened, not pushed with every snapshot; a short cache keeps re-renders cheap.
const newsCache = new Map();
async function loadNews(nodeId) {
  const hit = newsCache.get(nodeId);
  if (hit && Date.now() - hit.at < 60000) return hit.items;
  const res = await fetch(q(`/api/nodes/${encodeURIComponent(nodeId)}/news`));
  const items = res.ok ? await res.json() : [];
  newsCache.set(nodeId, { at: Date.now(), items });
  return items;
}

function newsSection(nodeId) {
  const wrap = el("section", { class: "news" });
  const head = el("h3", {}, el("span", {}, "新闻"), el("span", {}, "…"));
  const list = el("ul", { class: "news-list" }, el("li", { class: "news-empty" }, "读取中…"));
  wrap.append(head, list);
  loadNews(nodeId).then((items) => {
    head.lastChild.textContent = String(items.length);
    if (!items.length) { list.replaceChildren(el("li", { class: "news-empty" }, "最近 14 天没有相关报道")); return; }
    list.replaceChildren(...items.slice(0, 8).map((it) => {
      const tone = it.sentiment > 0.15 ? "up" : it.sentiment < -0.15 ? "down" : "flat";
      return el("li", {},
        el("a", { href: it.url, target: "_blank", rel: "noopener noreferrer" },
          el("span", { class: `tone ${tone}`, title: `情绪 ${it.sentiment.toFixed(2)}` }),
          el("span", { class: "news-title" }, it.title)),
        el("div", { class: "news-meta" }, `${it.feed} · ${ago(it.published_at)} · 相关度 ${Math.round(it.relevance * 100)}%`),
        it.quote && el("div", { class: "news-quote" }, `“${it.quote}”`),
      );
    }));
  }).catch(() => list.replaceChildren(el("li", { class: "news-empty" }, "新闻读取失败")));
  return wrap;
}

function renderLink(box, l) {
  const from = nodes.get(l.from_id), to = nodes.get(l.to_id);
  box.style.setProperty("--c", "#f7931a");
  box.append(el("div", { class: "kind" }, el("span", { class: "dot" }), `关系 · ${l.type}`));
  box.append(el("h2", {}, `${from?.name ?? "?"} → ${to?.name ?? "?"}`));
  box.append(el("div", { class: "badges" },
    el("span", { class: "badge" }, LINKS[l.type] || l.type),
    l.pending && el("span", { class: "badge warn" }, "待审核"),
    el("span", { class: "badge" }, provenanceText(l.provenance)),
  ));
  if (l.rationale) box.append(el("p", { class: "desc" }, l.rationale));
  const dl = el("dl");
  if (l.confidence != null) dl.append(el("dt", {}, "置信度"), el("dd", {}, `${Math.round(l.confidence * 100)}%`));
  for (const [k, v] of Object.entries(l.props || {})) dl.append(el("dt", {}, k), el("dd", {}, String(v)));
  if (l.valid_from || l.valid_to) dl.append(el("dt", {}, "有效期"), el("dd", {}, `${l.valid_from ?? "…"} 至 ${l.valid_to ?? "…"}`));
  if (dl.childElementCount) { box.append(el("h3", {}, "属性")); box.append(dl); }
  const ends = el("ul", { class: "rels" });
  for (const [dir, n] of [["←", from], ["→", to]]) {
    if (n) ends.append(el("li", {}, el("button", { type: "button", onclick: () => select({ kind: "node", id: n.id }, true) },
      el("span", { class: "dir" }, dir), el("span", { class: "who" }, el("em", {}, typeOf(n.type).label), n.name), svg(ICON.go))));
  }
  box.append(el("h3", {}, "两端"), ends);
  box.append(evidenceSection(l.id));
}

const VERDICT = { supports: ["支持", "ok"], reverse: ["方向相反", "warn"], contradicts: ["反驳", "bad"], weak: ["弱", "dim"], insufficient: ["数据不足", "dim"] };
const EV_KIND = { series: "数据联动", news: "新闻共现", note: "分析笔记" };

function evidencePill(l) {
  const s = l ? strengthOf(l) : 0;
  if (!s) return null;
  return el("span", { class: "ev-pill", title: "证据强度" }, el("b", {}, el("i", { style: `width:${Math.round(Math.min(1, s) * 100)}%` })), s.toFixed(2));
}

function lagChart(stat) {
  const entries = Object.entries(stat.lags || {}).map(([k, r]) => [Number(k), r]).sort((a, b) => a[0] - b[0]);
  if (!entries.length) return null;
  const w = 296, h = 64, mid = h / 2, bw = w / entries.length;
  const unit = { hour: "小时", day: "天", week: "周", month: "月" }[stat.bucket] || "";
  const bars = entries.map(([k, r], i) => {
    const bh = Math.abs(r) * (mid - 4);
    const best = k === stat.best_lag;
    return `<rect x="${(i * bw + bw * 0.18).toFixed(1)}" y="${(r >= 0 ? mid - bh : mid).toFixed(1)}" width="${(bw * 0.64).toFixed(1)}" height="${Math.max(1, bh).toFixed(1)}" rx="1.5" fill="${best ? "#f7931a" : "rgba(238,235,228,.28)"}"/>`;
  }).join("");
  const wrap = el("div", { class: "lag-chart" });
  wrap.append(svg(`<svg viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" role="img" aria-label="不同领先期的相关系数">
    <line x1="0" x2="${w}" y1="${mid}" y2="${mid}" stroke="rgba(255,255,255,.12)"/>${bars}</svg>`));
  wrap.append(el("div", { class: "lag-axis" }, el("span", {}, `乙领先 ${-entries[0][0]}${unit}`), el("span", {}, "同步"), el("span", {}, `甲领先 ${entries.at(-1)[0]}${unit}`)));
  return wrap;
}

function evidenceSection(linkId) {
  const wrap = el("section", { class: "evidence" });
  const head = el("h3", {}, el("span", {}, "证据"), el("span", {}, "…"));
  const body = el("div", {}, el("p", { class: "news-empty" }, "读取中…"));
  wrap.append(head, body);
  fetch(q(`/api/links/${encodeURIComponent(linkId)}/evidence`)).then((r) => r.json()).then((ev) => {
    head.lastChild.textContent = ev.evidence.length ? `强度 ${ev.strength.toFixed(2)}` : "无";
    if (!ev.evidence.length) {
      body.replaceChildren(el("p", { class: "news-empty" }, "还没有证据：两端都绑定数据后会计算联动，新闻同时提到两端时会记为共现。"));
      return;
    }
    body.replaceChildren(...ev.evidence.map((e) => {
      const [vText, vClass] = VERDICT[e.verdict] || [e.verdict, "dim"];
      const card = el("div", { class: "ev-card" },
        el("div", { class: "ev-head" },
          el("span", { class: "ev-kind" }, EV_KIND[e.kind] || e.kind),
          el("span", { class: `ev-verdict ${vClass}` }, vText),
          el("span", { class: "ev-strength" }, el("i", { style: `width:${Math.round(e.strength * 100)}%` })),
        ),
        el("p", { class: "ev-summary" }, e.summary),
      );
      if (e.kind === "series" && e.stat?.lags) {
        card.append(el("div", { class: "ev-series" }, `甲 = ${e.stat.a}`, el("br"), `乙 = ${e.stat.b}`));
        const chart = lagChart(e.stat);
        if (chart) card.append(chart);
      }
      const docs = e.stat?.docs || [];
      if (docs.length) card.append(el("ul", { class: "news-list" }, ...docs.map((d) => el("li", {},
        el("a", { href: d.url, target: "_blank", rel: "noopener noreferrer" }, el("span", { class: "tone flat" }), el("span", { class: "news-title" }, d.title)),
        el("div", { class: "news-meta" }, `${d.feed} · ${ago(d.published_at)}`)))));
      const refs = e.stat?.refs || [];
      if (refs.length) card.append(el("div", { class: "ev-refs" }, ...refs.map((r) => /^https?:/.test(r) ? el("a", { href: r, target: "_blank", rel: "noopener noreferrer" }, r) : el("span", {}, r))));
      if (e.kind === "note") card.append(el("div", { class: "news-meta" }, `${e.author} · ${ago(e.computed_at)}`));
      return card;
    }));
    body.append(el("p", { class: "ev-caveat" }, "联动只说明相关，不等于因果。"));
  }).catch(() => body.replaceChildren(el("p", { class: "news-empty" }, "证据读取失败")));
  return wrap;
}

// ------------------------------------------------------------------ review

async function post(url, okText) {
  const res = await fetch(q(url), { method: "POST" });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    toast(body.detail || `操作失败（${res.status}）`, true);
    return false;
  }
  if (okText) toast(okText);
  return true;
}

let reviewOpen = false;
function renderReview(review, proposed) {
  const total = review.length + proposed.length;
  $("#review-wrap").hidden = total === 0;
  if (!total) { setReviewOpen(false); return; }
  $("#review-count").textContent = `${total} 项待审核`;
  const pop = $("#review-pop");
  const row = (title, sub, live, onAccept, onReject) => {
    const acts = el("div", { class: "acts" });
    const go = (fn) => async () => { acts.querySelectorAll("button").forEach((b) => (b.disabled = true)); if (!(await fn())) acts.querySelectorAll("button").forEach((b) => (b.disabled = false)); };
    acts.append(el("button", { type: "button", class: "btn", onclick: go(onReject) }, "拒绝"), el("button", { type: "button", class: "btn primary", onclick: go(onAccept) }, "接受"));
    return el("div", { class: "review-item" }, el("div", { class: "what" }, title), acts, el("small", { class: live ? "live" : "" }, sub));
  };
  pop.replaceChildren(
    ...review.map((cs) => row(
      cs.summary || (cs.status === "open" ? "正在进行的改动" : "待审核的改动"),
      `${cs.operations} 项操作 · ${cs.status === "open" ? "agent 还在改" : "等你审核"}`,
      cs.status === "open",
      () => post(`/api/changesets/${cs.id}/accept`, "已接受，改动已生效"),
      () => post(`/api/changesets/${cs.id}/reject`, "已拒绝，改动已撤回"),
    )),
    ...proposed.map((t) => row(
      `新类型提案：${t.name}（${t.kind === "object" ? "对象" : "关系"}）`,
      t.justification || "agent 提议的新类型",
      false,
      () => post(`/api/types/${t.kind}/${t.name}/accept`, `已接受类型 ${t.name}`),
      () => post(`/api/types/${t.kind}/${t.name}/reject`, `已拒绝类型 ${t.name}`),
    )),
  );
}
function setReviewOpen(open) {
  reviewOpen = open;
  $("#review-pop").hidden = !open;
  $("#review-pill").setAttribute("aria-expanded", String(open));
}
$("#review-pill").addEventListener("click", () => setReviewOpen(!reviewOpen));
document.addEventListener("pointerdown", (e) => { if (reviewOpen && !e.target.closest("#review-wrap")) setReviewOpen(false); });

$("#undo").addEventListener("click", () => post("/api/undo", "已撤销上一步"));

// ------------------------------------------------------------------ chat

const logBox = $("#log");
const nearBottom = () => logBox.scrollHeight - logBox.scrollTop - logBox.clientHeight < 80;
function scrollLog(force) { if (force || nearBottom()) logBox.scrollTo({ top: logBox.scrollHeight, behavior: REDUCED ? "auto" : "smooth" }); }

function richText(text) {
  const span = el("div");
  span.innerHTML = esc(text).replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>").replace(/`([^`]+)`/g, "<code>$1</code>");
  return [...span.childNodes];
}

// ------------------------------------------------------------------ research report

// Hovering a sentence lights its reasoning chain (the sentence, and every earlier sentence it is inferred
// from) in the text, and the nodes and links they cite on the graph. Clicking pins it.
let reportFocus = null;      // {nodes: Set, links: Set} while a sentence is hovered or pinned
let viewingAt = null;        // ISO time when the timeline shows a past state; null = now
let report = null;
let reportState = null;
let pinned = null;           // sentence id
let panelTab = "chat";
const sentenceIndex = new Map();

function setTab(tab) {
  panelTab = tab;
  for (const b of document.querySelectorAll(".panel-tabs [data-tab]")) b.setAttribute("aria-selected", String(b.dataset.tab === tab));
  $("#report-view").hidden = tab !== "report";
  $("#log").hidden = tab === "report";
  $("#composer").hidden = tab === "report";
  if (tab === "report") { $("#report-dot").hidden = true; if (!report) loadReport(); }
  else setReportFocus(null);
}
for (const b of document.querySelectorAll(".panel-tabs [data-tab]")) b.addEventListener("click", () => setTab(b.dataset.tab));

async function loadReport() {
  try {
    const res = await fetch(q(viewingAt ? `/api/report?at=${encodeURIComponent(viewingAt)}` : "/api/report"));
    const r = await res.json();
    report = r.report;
    reportState = r.state;
    renderReport();
  } catch {
    $("#report-body").replaceChildren(el("p", { class: "rp-empty" }, "研报读取失败"));
  }
}

function chainOf(id) {
  const seen = new Set();
  const stack = [id];
  while (stack.length) {
    const cur = stack.pop();
    if (seen.has(cur)) continue;
    seen.add(cur);
    for (const up of sentenceIndex.get(cur)?.from || []) stack.push(up);
  }
  return seen;
}

function setReportFocus(sentenceId) {
  const body = $("#report-body");
  body.querySelectorAll(".rs").forEach((n) => n.classList.remove("on", "chain"));
  body.classList.toggle("focusing", !!sentenceId);
  if (!sentenceId) { reportFocus = null; refreshFocus(); return; }
  const chain = chainOf(sentenceId);
  const nodesSet = new Set(), linksSet = new Set();
  for (const id of chain) {
    const s = sentenceIndex.get(id);
    if (!s) continue;
    s.nodes.forEach((n) => nodes.has(n) && nodesSet.add(n));
    for (const l of s.links) {
      const link = links.get(l);
      if (link) { linksSet.add(l); nodesSet.add(srcId(link)); nodesSet.add(dstId(link)); }
    }
    body.querySelector(`.rs[data-id="${CSS.escape(id)}"]`)?.classList.add(id === sentenceId ? "on" : "chain");
  }
  reportFocus = nodesSet.size ? { nodes: nodesSet, links: linksSet } : null;
  refreshFocus();
}

// The other direction: a node hovered or selected on the graph marks the sentences that cite it.
function markCitingSentences() {
  if (panelTab !== "report" || reportFocus) return;
  const focal = hovered || (selected?.kind === "node" ? selected.id : null);
  for (const n of document.querySelectorAll("#report-body .rs")) {
    const s = sentenceIndex.get(n.dataset.id);
    n.classList.toggle("cites", !!focal && !!s && s.nodes.includes(focal));
  }
}

function reportStatus() {
  const st = $("#rp-status"), regen = $("#rp-regen");
  regen.hidden = !!viewingAt;
  if (viewingAt) { st.className = "rp-status hist"; st.textContent = `历史版本 · ${new Date(viewingAt).toLocaleString("zh-CN", { hour12: false })}`; return; }
  regen.disabled = !!reportState?.writing;
  regen.textContent = reportState?.writing ? "生成中…" : "重新生成";
  if (reportState?.writing) { st.className = "rp-status busy"; st.textContent = "正在根据当前图生成研报"; }
  else if (!report) { st.className = "rp-status"; st.textContent = "还没有研报"; }
  else if (reportState?.stale) {
    const m = Math.ceil((reportState.next_auto_in || 0) / 60);
    st.className = "rp-status warn";
    st.textContent = m > 0 ? `图已更新 · 约 ${m} 分钟后自动重写` : "图已更新 · 即将自动重写";
  } else { st.className = "rp-status ok"; st.textContent = "与当前图一致"; }
  if (reportState?.last_failed && !reportState.writing) { st.className = "rp-status bad"; st.textContent = `上次生成失败：${reportState.last_failed}`; }
}

function renderReport() {
  sentenceIndex.clear();
  pinned = null;
  reportFocus = null;
  reportStatus();
  const body = $("#report-body");
  if (!report && viewingAt) {
    body.replaceChildren(el("div", { class: "rp-empty" }, el("p", {}, "这个时间点还没有研报。"),
      el("p", {}, "研报生成后，在时间轴上以蓝色菱形标出。")));
    return;
  }
  if (!report) {
    // Reports are written from the accepted graph only, so a graph that is still all pending has nothing to go on.
    const settled = graph.nodes.filter((n) => !n.pending).length;
    const pending = graph.nodes.length - settled;
    body.replaceChildren(el("div", { class: "rp-empty" }, ...(!graph.nodes.length
      ? [el("p", {}, "图是空的，先让 agent 搭一些节点。")]
      : !settled
        ? [el("p", {}, `图里的 ${pending} 个节点还在待审核。`),
           el("p", {}, "研报只根据已接受的内容来写：接受变更集后会自动生成。"),
           el("button", { type: "button", class: "btn sm primary", onclick: () => setReviewOpen(true) }, "去审核")]
        : [el("p", {}, "这张图还没有研报。"), el("p", {}, "点“重新生成”，或等图有变化后自动生成。")])));
    $("#rp-regen").disabled = !settled;
    return;
  }
  const meta = [`v${report.version}`, new Date(report.created_at).toLocaleString("zh-CN", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit", hour12: false }),
    (report.model || "").split("/").pop()];
  const parts = [el("h3", { class: "rp-title" }, report.title), el("div", { class: "rp-meta" }, meta.filter(Boolean).join(" · ")),
    report.summary && el("p", { class: "rp-summary" }, report.summary)];
  for (const sec of report.body.sections || []) {
    const para = el("p", { class: "rp-para" });
    for (const sn of sec.sentences) {
      sentenceIndex.set(sn.id, sn);
      const cited = sn.nodes.length + sn.links.length;
      const span = el("span", {
        class: `rs${sn.from.length ? " inferred" : ""}${cited ? " cited" : ""}`, "data-id": sn.id, tabindex: "0",
        title: sn.from.length ? `由 ${sn.from.length} 句推出 · 引用 ${cited} 处` : `引用 ${cited} 处`,
        onmouseenter: () => { if (!pinned) setReportFocus(sn.id); },
        onmouseleave: () => { if (!pinned) setReportFocus(null); },
        onfocus: () => { if (!pinned) setReportFocus(sn.id); },
        onblur: () => { if (!pinned) setReportFocus(null); },
        onclick: () => {
          pinned = pinned === sn.id ? null : sn.id;
          body.querySelectorAll(".rs.pinned").forEach((n) => n.classList.remove("pinned"));
          setReportFocus(pinned);
          if (pinned) {
            body.querySelector(`.rs[data-id="${CSS.escape(sn.id)}"]`).classList.add("pinned");
            const ids = reportFocus?.nodes;
            if (ids?.size) G.zoomToFit(REDUCED ? 0 : 900, 120, (n) => ids.has(n.id));
          }
        },
      }, sn.text);
      para.append(span, " ");
    }
    parts.push(el("h4", { class: "rp-h" }, sec.heading), para);
  }
  parts.push(el("p", { class: "rp-foot" }, "悬停句子查看依据和推理链，点击固定；在图上悬停节点会标出引用它的句子。"));
  body.replaceChildren(...parts.filter(Boolean));
}

$("#rp-regen").addEventListener("click", async () => {
  reportState = { ...(reportState || {}), writing: true };
  reportStatus();
  try {
    const res = await fetch(q("/api/report/regenerate"), { method: "POST" });
    if (!res.ok) { const b = await res.json().catch(() => ({})); toast(b.detail || "研报生成失败", true); }
  } catch { toast("研报生成失败", true); }
  loadReport();
});

// ------------------------------------------------------------------ timeline

// Ticks are commits of the accepted graph (evenly spaced, so close-together changes stay pickable) plus the
// report versions between them. The rightmost stop is "now". Dragging shows each past state read-only.
let liveSnapshot = null;
let commits = [];
let reportVersions = [];
let tlIndex = -1;            // index into commits, or commits.length for "now"
const pastCache = new Map();

async function loadHistory() {
  try {
    const h = await (await fetch(q("/api/history"))).json();
    commits = h.commits || [];
    reportVersions = h.reports || [];
  } catch { return; }
  if (!viewingAt) tlIndex = commits.length;
  renderTimeline();
}

const fmtTime = (iso) => new Date(iso).toLocaleString("zh-CN", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit", hour12: false });
const stopX = (i) => (commits.length ? i / commits.length : 1) * 100;

function renderTimeline() {
  const ticks = $("#tl-ticks");
  const stops = commits.map((c, i) => el("button", {
    type: "button", class: "tl-tick c", style: `left:${stopX(i)}%`, "data-tip": `${fmtTime(c.created_at)} · ${c.message}`,
    "aria-label": `${fmtTime(c.created_at)} ${c.message}`, onclick: () => goTo(i),
  }));
  // A report sits between the commit before it and the next one, in proportion to time.
  const times = commits.map((c) => +new Date(c.created_at));
  const rs = reportVersions.filter((r) => times.length && +new Date(r.created_at) >= times[0]).map((r) => {
    const t = +new Date(r.created_at);
    const i = times.findLastIndex((x) => x <= t);
    const next = i + 1 < times.length ? times[i + 1] : Date.now();
    const frac = Math.min(0.92, Math.max(0.08, (t - times[i]) / Math.max(1, next - times[i])));
    return el("span", { class: "tl-tick r", style: `left:${stopX(i + frac)}%`, "data-tip": `${fmtTime(r.created_at)} · 研报 v${r.version}：${r.title || ""}` });
  });
  ticks.replaceChildren(...stops, ...rs, el("button", { type: "button", class: "tl-tick now", style: "left:100%", "data-tip": "现在", onclick: () => goTo(commits.length) }));
  const track = $("#tl-track");
  track.setAttribute("aria-valuemax", String(commits.length));
  track.setAttribute("aria-valuenow", String(tlIndex));
  const x = stopX(tlIndex < 0 ? commits.length : tlIndex);
  $("#tl-handle").style.left = `${x}%`;
  $("#tl-fill").style.width = `${x}%`;
  const c = commits[tlIndex];
  $("#tl-caption").textContent = c ? `${fmtTime(c.created_at)} · ${c.message}` : `现在 · 共 ${commits.length} 次提交`;
}

function setTimelineOpen(open) {
  $("#timeline").hidden = !open;
  $("#hint").classList.toggle("gone", open);
  document.body.classList.toggle("timeline-open", open);
  $('#dock [data-action="timeline"]').setAttribute("aria-pressed", String(open));
  if (open) loadHistory();
  else if (viewingAt) goTo(commits.length);
}

async function goTo(i) {
  i = Math.max(0, Math.min(commits.length, i));
  tlIndex = i;
  renderTimeline();
  if (i === commits.length) { leaveHistory(); return; }
  const c = commits[i];
  let past = pastCache.get(c.id);
  if (!past) {
    try { past = await (await fetch(q(`/api/graph?at=${encodeURIComponent(c.id)}`))).json(); }
    catch { toast("读取历史状态失败", true); return; }
    pastCache.set(c.id, past);
  }
  if (tlIndex !== i) return;  // the handle moved on while this loaded
  viewingAt = c.created_at;
  document.body.classList.add("history");
  $("#history-banner").hidden = false;
  $("#hb-when").textContent = fmtTime(c.created_at);
  $("#hb-msg").textContent = c.message;
  $("#review-wrap").hidden = true;
  input.disabled = true;
  input.placeholder = "正在查看历史状态，回到现在后再对话";
  applyGraph(past.graph);
  if (panelTab === "report") loadReport(); else report = null;
}

function leaveHistory() {
  if (!viewingAt) return;
  viewingAt = null;
  document.body.classList.remove("history");
  $("#history-banner").hidden = true;
  input.disabled = false;
  input.placeholder = "描述你想要的图…（Enter 发送，Shift+Enter 换行）";
  tlIndex = commits.length;
  renderTimeline();
  if (liveSnapshot) { applyGraph(liveSnapshot.graph); renderReview(liveSnapshot.review, liveSnapshot.proposed_types); }
  report = null;
  if (panelTab === "report") loadReport();
}
$("#hb-now").addEventListener("click", () => goTo(commits.length));

// Drag along the track; the handle snaps to the nearest stop.
(() => {
  const track = $("#tl-track");
  const indexAt = (clientX) => {
    const r = track.getBoundingClientRect();
    return Math.round(Math.max(0, Math.min(1, (clientX - r.left) / r.width)) * commits.length);
  };
  let dragging = false;
  track.addEventListener("pointerdown", (e) => {
    if (e.target.closest(".tl-tick")) return;
    dragging = true; track.setPointerCapture(e.pointerId); goTo(indexAt(e.clientX));
  });
  track.addEventListener("pointermove", (e) => { if (dragging) { const i = indexAt(e.clientX); if (i !== tlIndex) goTo(i); } });
  track.addEventListener("pointerup", () => { dragging = false; });
  track.addEventListener("keydown", (e) => {
    if (e.key === "ArrowLeft" || e.key === "ArrowRight") { e.preventDefault(); goTo(tlIndex + (e.key === "ArrowLeft" ? -1 : 1)); }
    if (e.key === "End") goTo(commits.length);
    if (e.key === "Home") goTo(0);
  });
})();

// ------------------------------------------------------------------ conversations

let currentSession = null;
let sessionList = [];

// A saved conversation comes back as the events the page showed; replay them without the entrance animation.
function showSession(msg) {
  currentSession = msg.session.id;
  sessionModel = msg.session.model || null;
  renderModel();
  sessionList = msg.sessions || [];
  $("#session-title").textContent = msg.session.title || "新对话";
  closeToolGroup();
  for (const n of [...logBox.children]) if (!n.classList.contains("log-intro") && n !== thinking) n.remove();
  logBox.classList.add("replay");
  for (const e of msg.events) {
    if (e.type === "tool") logTool(e);
    else if (e.type === "user" || e.type === "assistant" || e.type === "error" || e.type === "note") log(e.type, e.text);
  }
  closeToolGroup();
  logBox.scrollTop = logBox.scrollHeight;
  requestAnimationFrame(() => logBox.classList.remove("replay"));
  renderSessions();
}

// ------------------------------------------------------------------ model picker

let sessionModel = null;   // this conversation's choice; null means the server default
let models = null;         // what the model endpoint (magpie) offers
const PROVIDERS = { claude: "Claude · 经 Claude Code 订阅", codex: "Codex · 经 Codex 订阅", anthropic: "Anthropic API" };

async function loadModels() {
  try { models = await (await fetch("/api/models")).json(); } catch { models = null; }
  renderModel();
}

const shortName = (id) => {
  const m = models?.models.find((x) => x.id === id);
  return m ? m.name : id.split("/").pop();
};

function renderModel() {
  const active = sessionModel || models?.default;
  $("#model-name").textContent = active ? shortName(active) : "模型";
  const provider = active?.split("/")[0];
  $("#model-btn").dataset.provider = provider || "";
  $("#model-btn").dataset.tip = active ? `${active}${models?.via ? " · 经 magpie" : ""}` : "选择模型";
  const menu = $("#model-menu");
  if (!models) { menu.replaceChildren(el("p", { class: "sess-empty" }, "读不到模型列表")); return; }
  const groups = new Map();
  for (const m of models.models) {
    if (!groups.has(m.provider)) groups.set(m.provider, []);
    groups.get(m.provider).push(m);
  }
  menu.replaceChildren(
    ...[...groups].map(([p, ms]) => el("div", { class: "model-group" },
      el("div", { class: "model-group-title" }, PROVIDERS[p] || p),
      ...ms.map((m) => el("button", {
        type: "button", role: "menuitemradio", "aria-checked": String(m.id === active), class: m.id === active ? "current" : "",
        onclick: () => chooseModel(m.id),
      }, el("span", { class: "model-label" }, m.name), m.id === models.default && el("span", { class: "model-tag" }, "默认"),
        m.context && el("span", { class: "model-ctx" }, `${Math.round(m.context / 1000)}K`))))),
    el("p", { class: "model-foot" }, models.via ? `经 magpie 路由 · ${models.base_url}` : models.base_url, models.error ? ` · ${models.error}` : ""),
  );
}

function chooseModel(id) {
  setModelOpen(false);
  if (busy) { toast("agent 正在回复，结束后再切换模型", true); return; }
  const next = id === models?.default ? null : id;
  if (ws?.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: "set_model", model: next }));
  toast(`本对话改用 ${shortName(id)}，上下文保留`);
}

function setModelOpen(open) {
  $("#model-menu").hidden = !open;
  $("#model-btn").setAttribute("aria-expanded", String(open));
  if (open && !models) loadModels();
  if (open) $("#model-menu .current")?.scrollIntoView({ block: "center" });
}
$("#model-btn").addEventListener("click", () => setModelOpen($("#model-menu").hidden));
document.addEventListener("pointerdown", (e) => { if (!e.target.closest(".model-wrap")) setModelOpen(false); });
loadModels();

function renderSessions() {
  const menu = $("#sessions-menu");
  if (!sessionList.length) { menu.replaceChildren(el("p", { class: "sess-empty" }, "还没有历史对话")); return; }
  menu.replaceChildren(...sessionList.map((x) => el("button", {
    type: "button", role: "menuitem", class: x.id === currentSession ? "current" : "",
    onclick: () => { setSessionsOpen(false); if (x.id !== currentSession) openSession(x.id); },
  }, el("span", { class: "sess-name" }, x.title), el("span", { class: "sess-when" }, ago(x.updated_at)))));
}

function setSessionsOpen(open) {
  $("#sessions-menu").hidden = !open;
  $("#sessions-btn").setAttribute("aria-expanded", String(open));
}

function openSession(id) {
  if (busy) { toast("agent 正在回复，结束后再切换对话", true); return; }
  ws?.readyState === WebSocket.OPEN && ws.send(JSON.stringify({ type: "open_session", id }));
}

$("#sessions-btn").addEventListener("click", () => setSessionsOpen($("#sessions-menu").hidden));
document.addEventListener("pointerdown", (e) => { if (!e.target.closest(".sess-wrap")) setSessionsOpen(false); });
$("#new-session").addEventListener("click", () => {
  if (busy) { toast("agent 正在回复，结束后再开新对话", true); return; }
  if (ws?.readyState === WebSocket.OPEN) { ws.send(JSON.stringify({ type: "new_session" })); input.focus(); }
});

function log(kind, text) {
  const stick = nearBottom();
  const line = el("div", { class: `msg ${kind}` }, ...(kind === "assistant" ? richText(text) : [text]));
  logBox.insertBefore(line, thinking?.isConnected ? thinking : null);
  scrollLog(stick || kind === "user");
  if (kind !== "tool") closeToolGroup();
  return line;
}

// Consecutive tool calls collapse into one expandable line so the conversation stays readable.
let toolGroup = null;
function closeToolGroup() { if (toolGroup) { toolGroup.box.classList.add("done"); toolGroup = null; } }
function logTool(msg) {
  const stick = nearBottom();
  if (!toolGroup) {
    const label = el("span");
    const summary = el("summary", {}, label, el("span", { class: "bar" }, el("i")));
    const box = el("details", { class: "msg tool" }, summary);
    toolGroup = { box, label, ok: 0, failed: 0 };
    logBox.insertBefore(box, thinking?.isConnected ? thinking : null);
  }
  const g = toolGroup;
  if (msg.ok) g.ok += 1; else g.failed += 1;
  g.label.textContent = `调用工具 ${g.ok + g.failed} 次` + (g.failed ? ` · ${g.failed} 次失败` : "");
  g.box.classList.toggle("failed", g.failed > 0);
  if (!msg.ok) g.box.open = true;
  g.box.append(el("div", { class: `row${msg.ok ? "" : " bad"}` }, el("b", {}, msg.ok ? "✓" : "✗"), msg.label + (msg.ok ? "" : `：${(msg.error || "").slice(0, 200)}`)));
  scrollLog(stick);
}

let thinking = null;
function setBusy(value) {
  busy = value;
  const send = $("#send");
  send.classList.toggle("busy", value);
  send.replaceChildren(svg(value ? ICON.busy : ICON.send));
  send.setAttribute("aria-label", value ? "处理中" : "发送");
  syncSend();
  if (value && !thinking) {
    thinking = el("div", { class: "thinking msg" }, el("i", {}, el("b"), el("b"), el("b")), el("span", {}, "正在思考"));
    logBox.append(thinking);
    scrollLog();
  }
  if (!value) {
    thinking?.remove();
    thinking = null;
    closeToolGroup();
  }
}

const input = $("#input");
function syncSend() { $("#send").disabled = busy || !input.value.trim(); if (busy) $("#send").disabled = false; }
function autosize() { input.style.height = "auto"; input.style.height = `${Math.min(input.scrollHeight, 180)}px`; }

function showProject(p) {
  if (!p) return;
  $("#project-name").textContent = p.name;
  document.title = `${p.name} · 本体工作台`;
}

let ws;
function setConn(state) {
  const c = $("#conn");
  c.className = `conn ${state}`;
  c.title = state === "on" ? "已连接" : state === "off" ? "连接断开，正在重连" : "连接中";
}
function connect() {
  const sess = currentSession ? `?session=${encodeURIComponent(currentSession)}` : "";
  ws = new WebSocket(q(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws${sess}`));
  ws.onopen = () => setConn("on");
  ws.onmessage = (evt) => {
    const msg = JSON.parse(evt.data);
    if (msg.type === "graph") {
      liveSnapshot = msg;
      if (viewingAt) return;  // keep showing the past state; "回到现在" brings the live one back
      showProject(msg.project); applyGraph(msg.graph); renderReview(msg.review, msg.proposed_types);
      if (msg.news_refreshed != null && lastRefresh) toast(msg.news_refreshed ? `新闻已更新：${msg.news_refreshed} 篇新报道` : "新闻已是最新");
    }
    else if (msg.type === "history") loadHistory();
    else if (msg.type === "session") showSession(msg);
    else if (msg.type === "report") {
      if (viewingAt) return;
      report = msg.report; reportState = msg.state;
      if (panelTab === "report") { renderReport(); toast("研报已根据最新的图更新"); } else $("#report-dot").hidden = false;
    }
    else if (msg.type === "report_status") { reportState = { ...(reportState || {}), writing: msg.writing }; if (panelTab === "report") reportStatus(); }
    else if (msg.type === "model") { sessionModel = msg.model; renderModel(); }
    else if (msg.type === "assistant") log("assistant", msg.text);
    else if (msg.type === "tool") logTool(msg);
    else if (msg.type === "error") log("error", msg.text);
    else if (msg.type === "status") { if (msg.text) log("note", msg.text); setBusy(msg.busy); }
  };
  ws.onclose = async () => {
    setConn("off");
    setBusy(false);
    const session = await fetch("/auth/session").catch(() => null);
    if (session?.status === 401) location.replace("/login");
    else setTimeout(connect, 1500);
  };
}
connect();
addEventListener("pageshow", (event) => { if (event.persisted) location.reload(); });

function send(text = input.value.trim()) {
  if (!text || busy || ws.readyState !== WebSocket.OPEN) return;
  log("user", text);
  ws.send(JSON.stringify({ type: "chat", text }));
  input.value = "";
  autosize();
  syncSend();
}
$("#composer").addEventListener("submit", (e) => { e.preventDefault(); send(); });
input.addEventListener("input", () => { autosize(); syncSend(); });
input.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); send(); }
});
for (const chip of document.querySelectorAll(".prompt-chip")) chip.addEventListener("click", () => send(chip.textContent));
syncSend();

// ------------------------------------------------------------------ keyboard

addEventListener("keydown", (e) => {
  if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
    e.preventDefault();
    if (!$("#search").hidden) { searchInput.focus(); searchInput.select(); }
    return;
  }
  if (e.key === "Escape") {
    if (viewingAt && !pinned) { goTo(commits.length); return; }
    if (pinned) { pinned = null; document.querySelectorAll("#report-body .rs.pinned").forEach((n) => n.classList.remove("pinned")); setReportFocus(null); return; }
    if (reviewOpen) setReviewOpen(false);
    else if (selected) select(null);
    else if (typeFilter.size) { typeFilter.clear(); renderLegend(); refreshFocus(); }
    return;
  }
  if (typing(e) || e.metaKey || e.ctrlKey || e.altKey) return;
  const map = { r: "fit", " ": "rotate", l: "labels", "\\": "panel", u: "refresh", t: "timeline" };
  if (e.key === "/") { e.preventDefault(); if (document.body.classList.contains("panel-collapsed")) dockActions.panel(); input.focus(); }
  else if (map[e.key.toLowerCase()] && !(map[e.key.toLowerCase()] !== "panel" && graph.nodes.length === 0)) { e.preventDefault(); dockActions[map[e.key.toLowerCase()]](); }
});
