# app/subgraphs/payments.py
from __future__ import annotations
import json
from typing import Annotated, Dict, Any, List, Optional, TypedDict
from langgraph.graph import StateGraph, START, END
from langgraph.prebuilt import ToolNode
from langgraph.graph.message import add_messages
from langchain_core.messages import AIMessage, ToolMessage

MAX_TRIES = 8

def _ai(name: str, args: dict) -> AIMessage:
    return AIMessage(content="", tool_calls=[{"id": f"call_{name}", "type": "tool_call", "name": name, "args": args or {}}])

class PayState(TypedDict):
    messages: Annotated[List, add_messages]
    tries: int

def _is_pay_url(u: Optional[str]) -> bool:
    return isinstance(u, str) and ("payment" in u.lower() or "billing" in u.lower())

def build_payments_subgraph(page: dict, tools: List):
    def node(state: PayState) -> PayState:
        state.setdefault("tries", 0)
        if state["messages"] and isinstance(state["messages"][-1], ToolMessage):
            last = state["messages"][-1]
            name = getattr(last, "name", "")
            try:
                payload = json.loads(last.content or "{}")
            except Exception:
                payload = {}
            data = payload.get("data", payload) or {}
            if name == "get_page_state":
                if _is_pay_url((data or {}).get("url")):
                    return {**state, "messages": [_ai("done", {"reason": "Arrived at Payments"})]}
                return {**state, "messages": [_ai("find", {"query": "Payments"})]}
            if name == "find":
                matches = (data or {}).get("matches", [])
                if matches:
                    sel = matches[0].get("selector") or ""
                    if sel:
                        return {**state, "messages": [
                            _ai("click", {"selector": sel}),
                            _ai("wait_for_idle", {"quietMs": 900, "timeout": 9000}),
                            _ai("get_page_state", {}),
                        ]}
                t = state.get("tries", 0) + 1
                if t >= MAX_TRIES:
                    return {**state, "messages": [_ai("done", {"reason": "Payments link not found"})]}
                state["tries"] = t
                return {**state, "messages": [_ai("wait", {"ms": 600}), _ai("get_page_state", {})]}
            if name in ("click", "wait_for_idle"):
                return {**state, "messages": [_ai("get_page_state", {})]}
        return {**state, "messages": [_ai("get_page_state", {})]}

    g = StateGraph(PayState)
    g.add_node("nav", node)
    g.add_node("tools", ToolNode(tools))
    g.add_edge(START, "nav")
    g.add_edge("nav", "tools")
    g.add_conditional_edges("tools", lambda s: END if (s.get("messages") and isinstance(s["messages"][-1], ToolMessage) and getattr(s["messages"][-1], "name", "") == "done") else "nav", {
        END: END, "nav": "nav",
    })
    return g.compile()
