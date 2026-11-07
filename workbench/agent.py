"""The workbench's chat agent: Claude with the ontology MCP tools, run in-process.

The tool list and the tool calls go through the same MCPServer that external clients use, so the
workbench agent cannot do anything an MCP client could not.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Awaitable, Callable
from typing import Any

import anthropic
from mcp.server.mcpserver.exceptions import ToolError

from ontology import mcp_server

MODEL = os.environ.get("WORKBENCH_MODEL", "claude-opus-5-5")
EFFORT = os.environ.get("WORKBENCH_EFFORT", "medium")
MAX_TURNS = 60
BROKEN_RETRIES = (2, 6)  # seconds to wait before retrying a reply that arrived cut off

log = logging.getLogger("workbench")


class BrokenReply(Exception):
    """The gateway sent a reply that could not be read (cut off, empty), even after retrying."""

WRITE_TOOLS = {
    "upsert_node", "update_props", "create_link", "merge_nodes", "archive",
    "submit_changeset", "revert_changeset", "propose_type", "propose_binding", "add_evidence_note",
}

SYSTEM = mcp_server.INSTRUCTIONS + """
You are working inside the ontology workbench: the user sees the graph on a canvas next to this chat, and every
write you make appears there immediately as a pending (ghost) element until they accept the changeset.
- Answer in the user's language. Node names follow the user's language too; put the other language in aliases
  (e.g. name "比特币" with aliases ["BTC", "Bitcoin"]).
- Prefer a readable graph over an exhaustive one: for a "build a graph around X" request, 20-60 nodes, each linked
  to something, is usually right. Ask before going much bigger.
- Work in batches: search for what already exists, then create nodes, then links. Parallel tool calls are fine.
- When you finish a request that changed the graph, call submit_changeset, then tell the user in two or three
  sentences what you added and anything you were unsure about. Do not list every node; the canvas shows them.
"""

Emit = Callable[[dict[str, Any]], Awaitable[None]]


def tool_definitions(tools: list[Any]) -> list[dict[str, Any]]:
    return [{"name": t.name, "description": t.description or "", "input_schema": t.input_schema} for t in tools]


def describe_call(name: str, args: dict[str, Any]) -> str:
    """A short label for the chat log, e.g. 'upsert_node Asset 比特币'."""
    for keys in (("type", "name"), ("type",), ("query",), ("summary",), ("node_id",), ("target_id",)):
        if all(k in args for k in keys):
            return " ".join([name, *(str(args[k]) for k in keys)])
    return name


async def call_mcp_tool(name: str, args: dict[str, Any]) -> tuple[str, bool]:
    """Run one ontology tool; returns (text for Claude, is_error)."""
    try:
        result = await mcp_server.server.call_tool(name, args)
    except ToolError as e:
        return str(e), True
    if getattr(result, "structured_content", None) is not None:
        text = json.dumps(result.structured_content, ensure_ascii=False, default=str)
    else:
        text = "\n".join(getattr(c, "text", "") for c in result.content)
    return text, bool(getattr(result, "is_error", False))


class WorkbenchAgent:
    """One conversation. History is append-only so thinking blocks and the prompt cache stay valid."""

    def __init__(self, client: anthropic.AsyncAnthropic | None = None, model: str = MODEL, effort: str = EFFORT):
        self.client = client or anthropic.AsyncAnthropic()
        self.model = model
        self.effort = effort
        self.messages: list[dict[str, Any]] = []
        self._tools: list[dict[str, Any]] | None = None

    def export_messages(self) -> list[dict[str, Any]]:
        """The history as plain JSON (SDK content blocks become dicts), to store and later hand back to the API."""
        def block(b: Any) -> Any:
            if isinstance(b, dict):
                return b
            return b.model_dump(mode="json", exclude_none=True) if hasattr(b, "model_dump") else dict(vars(b))

        return [{**m, "content": [block(b) for b in m["content"]]} if isinstance(m["content"], list) else m
                for m in self.messages]

    async def tools(self) -> list[dict[str, Any]]:
        if self._tools is None:
            # The page fixes the project, so the in-page agent does not get the switch.
            self._tools = tool_definitions([t for t in await mcp_server.server.list_tools() if t.name != "use_project"])
        return self._tools

    async def _create(self, **kw: Any) -> Any:
        """One model call. A gateway such as magpie can drop the upstream mid-reply and hand back a body that is
        not JSON; the SDK does not retry that, so retry here a couple of times before giving up."""
        for wait in (*BROKEN_RETRIES, None):
            try:
                return await self.client.beta.messages.create(**kw)
            except (json.JSONDecodeError, anthropic.APIResponseValidationError) as e:
                if wait is None:
                    raise BrokenReply(str(e)) from e
                log.warning("unreadable model reply (%s); retrying in %ss", e, wait)
                await asyncio.sleep(wait)

    async def run(self, text: str, emit: Emit) -> None:
        start = len(self.messages)
        self.messages.append({"role": "user", "content": text})
        tools = await self.tools()
        for _ in range(MAX_TURNS):
            response = await self._create(
                model=self.model,
                max_tokens=16000,
                system=SYSTEM,
                tools=tools,
                messages=self.messages,
                output_config={"effort": self.effort},
                cache_control={"type": "ephemeral"},
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            )
            if response.stop_reason == "refusal":
                # Nothing usable came back: drop this request so the conversation can continue.
                del self.messages[start:]
                await emit({"type": "error", "text": "模型拒绝了这个请求，请换个说法再试。"})
                return
            self.messages.append({"role": "assistant", "content": response.content})
            for block in response.content:
                if block.type == "text" and block.text.strip():
                    await emit({"type": "assistant", "text": block.text})
            if response.stop_reason != "tool_use":
                if response.stop_reason == "max_tokens":
                    await emit({"type": "error", "text": "回复太长被截断了，可以让我继续。"})
                return
            results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                label = describe_call(block.name, block.input)
                out, is_error = await call_mcp_tool(block.name, block.input)
                await emit({"type": "tool", "name": block.name, "label": label, "ok": not is_error,
                            "error": out if is_error else None, "writes": block.name in WRITE_TOOLS})
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": out, "is_error": is_error})
            self.messages.append({"role": "user", "content": results})
        await emit({"type": "error", "text": f"已经连续调用了 {MAX_TURNS} 轮工具，先停下来。可以让我继续。"})
