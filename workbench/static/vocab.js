/* Shared vocabulary for the workbench and the project list: type colours and link labels. */

export const TYPES = {
  Asset: { label: "资产", color: "#f7931a" },
  Narrative: { label: "叙事", color: "#ff5c7a" },
  Concept: { label: "概念", color: "#b69cff" },
  Event: { label: "事件", color: "#4cc9f0" },
  Entity: { label: "主体", color: "#57e39b" },
  MacroIndicator: { label: "宏观指标", color: "#7d8cff" },
  Metric: { label: "指标", color: "#d6c25a" },
  Department: { label: "部门", color: "#a3acbd" },
  Agent: { label: "智能体", color: "#e98bff" },
  Signal: { label: "信号", color: "#ffb08a" },
  // research pack
  Question: { label: "问题", color: "#ffd166" },
  Hypothesis: { label: "假设", color: "#ff7eb6" },
  Claim: { label: "论断", color: "#c3f584" },
  Observation: { label: "观测", color: "#7ee8d6" },
  Method: { label: "方法", color: "#f4a259" },
};
export const LINKS = {
  affects: "影响", drives: "驱动", underpins: "支撑", part_of: "属于", competes_with: "竞争",
  measures: "衡量", tracks: "跟踪", issues: "发行", holds: "持有", sets: "决定", involves: "涉及",
  belongs_to: "隶属", watches: "关注", signal_on: "针对", based_on: "依据", authored: "产出",
  addresses: "回应", supports: "支持", contradicts: "反驳", qualifies: "限定", derives_from: "推论自",
  refines: "细化", about: "涉及", influences: "影响", uses_method: "使用方法",
};
/* Layers: the upper band holds what is studied (world, subject); the lower band holds the organisation's own
   layer or the reasoning about it (company, reasoning), drawn as crystals. */
export const LAYERS = { world: "世界层", company: "公司层", subject: "研究对象", reasoning: "推理" };
export const lowerLayer = (layer) => layer === "company" || layer === "reasoning";
export const PROVENANCE = { user: "你提供的", llm_prior: "模型常识（待验证）", data_source: "数据" };
export const FALLBACK = { label: "其他", color: "#9ca3af" };
export const typeOf = (t) => TYPES[t] || { ...FALLBACK, label: t };

/* Library: kinds of source, and how a source bears on a node or link (a grounding's stance). */
export const KINDS = {
  article: "期刊论文", preprint: "预印本", book: "图书", chapter: "章节", thesis: "学位论文", conference: "会议论文",
  report: "报告", webpage: "网页", dataset: "数据集", software: "软件", other: "其他",
};
export const STANCES = {
  states: { label: "陈述", tone: "ok", hint: "文献直接提出这一论断" },
  supports: { label: "支持", tone: "ok", hint: "文献的发现支持它" },
  contradicts: { label: "反驳", tone: "bad", hint: "文献的发现与它相反" },
  qualifies: { label: "限定", tone: "warn", hint: "文献说明它只在某些条件下成立" },
};
/* Reasoning types that should rest on sources; the canvas marks them while they have none. */
export const NEEDS_GROUNDING = new Set(["Claim", "Hypothesis", "Observation"]);
