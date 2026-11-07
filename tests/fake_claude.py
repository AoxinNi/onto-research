"""A scripted stand-in for the Claude API, so the workbench can be exercised without a key.

It plays the part of an agent asked to build a small BTC concept graph: create nodes, read the ids
back from the tool results, link them, submit the changeset, then summarise.
"""

from __future__ import annotations

import itertools
import json
from types import SimpleNamespace as NS

LLM = {"kind": "llm_prior"}

NODES = [
    ("Asset", "比特币", ["BTC", "Bitcoin"], {"symbol": "BTC", "asset_class": "spot"}),
    ("Concept", "稀缺性", ["Scarcity"], {"category": "economics"}),
    ("Concept", "减半", ["Halving"], {"category": "protocol"}),
    ("Narrative", "数字黄金", ["Digital Gold"], {"stance": "bullish", "lifecycle": "consensus"}),
    ("Narrative", "ETF 机构化", ["ETF institutionalization"], {"stance": "bullish", "lifecycle": "spreading"}),
    ("Narrative", "流动性宽松", ["Liquidity easing"], {"stance": "bullish"}),
    ("Event", "现货 ETF 获批", ["Spot ETF approval"], {"category": "etf", "status": "digested"}),
    ("Entity", "美联储", ["Fed", "Federal Reserve"], {"kind": "central_bank", "country": "US"}),
    ("Entity", "贝莱德", ["BlackRock"], {"kind": "etf_issuer"}),
    ("Asset", "IBIT", ["iShares Bitcoin Trust"], {"symbol": "IBIT", "asset_class": "etf"}),
    ("MacroIndicator", "联邦基金利率", ["Fed funds rate"], {"unit": "%"}),
    ("Metric", "ETF 净流入", ["ETF net flow"], {"domain": "fund_flow", "unit": "USD"}),
]

LINKS = [
    ("underpins", "稀缺性", "数字黄金", "固定的 2100 万上限是数字黄金叙事的基础"),
    ("part_of", "减半", "稀缺性", "减半让新增供给每四年减少一半"),
    ("underpins", "稀缺性", "比特币", None),
    ("affects", "现货 ETF 获批", "ETF 机构化", "获批打开了传统资金的通道"),
    ("affects", "现货 ETF 获批", "比特币", "带来持续的现货买盘"),
    ("issues", "贝莱德", "IBIT", "贝莱德是 IBIT 的发行方"),
    ("tracks", "IBIT", "比特币", "IBIT 持有现货 BTC"),
    ("sets", "美联储", "联邦基金利率", "联邦基金利率由 FOMC 决定"),
    ("drives", "联邦基金利率", "流动性宽松", "降息预期推动流动性宽松叙事"),
    ("drives", "联邦基金利率", "比特币", "利率下降通常提升风险资产估值"),
    ("measures", "ETF 净流入", "ETF 机构化", "净流入是机构化叙事最直接的度量"),
    ("competes_with", "数字黄金", "流动性宽松", "避险叙事和风险资产叙事此消彼长"),
]


def _tool_use(ids, name, args):
    return NS(type="tool_use", id=f"toolu_{next(ids)}", name=name, input=args)


def _response(stop_reason, *content):
    return NS(stop_reason=stop_reason, content=list(content))


class FakeMessages:
    def __init__(self):
        self.calls = 0
        self._ids = itertools.count()

    async def create(self, **kwargs):
        self.calls += 1
        messages = kwargs["messages"]
        step = sum(1 for m in messages if m["role"] == "assistant")
        if step == 0:
            return _response("tool_use", NS(type="text", text="先建核心节点。"), *[
                _tool_use(self._ids, "upsert_node", {"type": t, "name": n, "aliases": a, "props": p,
                                                       "provenance": LLM, "rationale": "BTC 核心概念"})
                for t, n, a, p in NODES
            ])
        if step == 1:
            ids = {}
            for block in messages[-1]["content"]:
                data = json.loads(block["content"])
                node = data.get("node") or data.get("result", {}).get("node")
                ids[node["name"]] = node["id"]
            return _response("tool_use", *[
                _tool_use(self._ids, "create_link", {
                    "type": t, "from_id": ids[a], "to_id": ids[b], "rationale": why or "常识",
                    "provenance": LLM if why else {"kind": "user"}, "confidence": 0.7,
                })
                for t, a, b, why in LINKS
            ])
        if step == 2:
            return _response("tool_use", _tool_use(self._ids, "submit_changeset", {"summary": "以比特币为核心的概念图"}))
        return _response("end_turn", NS(type="text", text="搭好了 12 个节点、12 条关系，等你在上方审核。"))


class FakeClaude:
    def __init__(self):
        self.beta = NS(messages=FakeMessages())
