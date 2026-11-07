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
};
export const LINKS = {
  affects: "影响", drives: "驱动", underpins: "支撑", part_of: "属于", competes_with: "竞争",
  measures: "衡量", tracks: "跟踪", issues: "发行", holds: "持有", sets: "决定", involves: "涉及",
  belongs_to: "隶属", watches: "关注", signal_on: "针对", based_on: "依据", authored: "产出",
};
export const PROVENANCE = { user: "你提供的", llm_prior: "模型常识（待验证）", data_source: "数据" };
export const FALLBACK = { label: "其他", color: "#9ca3af" };
export const typeOf = (t) => TYPES[t] || { ...FALLBACK, label: t };
