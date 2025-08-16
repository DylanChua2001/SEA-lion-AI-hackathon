# app/subgraphs/immunisation/immunisations.py
from __future__ import annotations
import json
import logging
from typing import Annotated, Dict, Any, List, Optional, TypedDict

from langgraph.graph import StateGraph, START, END
from langgraph.prebuilt import ToolNode
from langgraph.graph.message import add_messages
from langchain_core.messages import AnyMessage, AIMessage, ToolMessage, SystemMessage, HumanMessage

from app.llm import make_llm  # align with appointments planner

log = logging.getLogger(__name__)
log.propagate = True
if not log.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("%(levelname)s %(process)d %(name)s: %(message)s"))
    log.addHandler(_h)
log.setLevel(logging.INFO)

TARGET_HOST   = "eservices.healthhub.sg"
IMM_URL_TOKEN = "/immunisation"  # case-insensitive matching via .lower()

# ───────────────────────── tool-calling helpers ─────────────────────────
def _ai_tool_call(name: str, args: Dict[str, Any]) -> AIMessage:
    return AIMessage(content="", tool_calls=[{
        "id": f"call_{name}", "type": "tool_call", "name": name, "args": args or {}
    }])

def _last_payload(msg: ToolMessage) -> Dict[str, Any]:
    try:
        payload = json.loads(msg.content or "{}")
    except Exception:
        payload = {}
    return payload.get("data", payload) or {}

def _goal_from_msgs(msgs: List[AnyMessage]) -> str:
    for m in msgs or []:
        if isinstance(m, HumanMessage) and isinstance(m.content, str) and "GOAL:" in m.content:
            try:
                return m.content.split("GOAL:", 1)[1].strip()
            except Exception:
                pass
    return "Get the user into the Immunisation Records workflow."

# ───────────────────────── LLM: choose ONE selector ─────────────────────────
def _pick_selector_with_llm(goal: str, page_url: str, matches: List[Dict[str, Any]]) -> Optional[str]:
    """Return a single CSS selector to click (or None)."""
    if not matches:
        return None

    # compact candidate list
    candidates = []
    for m in matches[:10]:
        candidates.append({
            "text": (m or {}).get("text") or "",
            "href": (m or {}).get("href") or "",
            "selector": (m or {}).get("selector") or "",
        })

    llm = make_llm(temperature=0)
    sys = SystemMessage(content=(
        "You are a precise web agent. Choose exactly ONE clickable CSS selector that most likely opens "
        "the Immunisation Records page.\n"
        "Return ONLY JSON, no prose. Example: {\"selector\": \"a[href*='immunisation']\"}\n"
        "If none are relevant, return {}."
    ))
    usr = HumanMessage(content=json.dumps({
        "goal": goal,
        "page_url": page_url,
        "candidates": candidates,
        "hint": (
            # Strong, explicit prioritization:
            "Priority 1: Pick a candidate whose href EXACTLY contains the full URL "
            "'https://eservices.healthhub.sg/immunisation' (case-insensitive substring match is OK).\n"
            "Priority 2: If none, pick a candidate whose href or visible text contains the word "
            "'immunisation' (case-insensitive).\n"
            "Priority 3: If still none, consider related words such as 'immunization', 'vaccination', "
            "'records', 'certificate', 'booster', 'jab', 'shot'.\n"
            "If none of the candidates match these rules, return {}. Do not pick unrelated links."
        ),
    }, ensure_ascii=False))

    resp = llm.invoke([sys, usr])
    raw = (getattr(resp, "content", None) or "").strip()
    try:
        obj = json.loads(raw)
        sel = (obj.get("selector") or "").strip()
        return sel or None
    except Exception:
        log.warning("[imm_plan] LLM selector parse failed: %r", raw)
        return None

# ───────────────────────── State ─────────────────────────
class ImmPlanState(TypedDict):
    messages: Annotated[List[AnyMessage], add_messages]
    goal: str
    planned: bool        # once we've planned, we just execute & finish
    page_url: str        # last seen url (for LLM context)

# ───────────────────────── Subgraph ─────────────────────────
def build_immunisations_subgraph(page: Dict[str, Any], tools: Optional[List] = None):
    """
    Plan-once, execute-once subgraph (navigation only), mirroring appointments planner:
      1) get_page_state
      2) find("immuni|immuniz|vaccin|record|cert|booster|jab|shot")
      3) LLM picks ONE selector (with strong nudge toward https://eservices.healthhub.sg/immunisation)
      4) click
      5) done
    """
    if tools is None:
        from ...tools import build_tools  # lazy import to avoid circulars
        tools = build_tools(page)

    def node(state: ImmPlanState) -> ImmPlanState:
        state.setdefault("messages", [])
        state.setdefault("goal", _goal_from_msgs(state["messages"]))
        state.setdefault("planned", False)
        state.setdefault("page_url", "")

        # If we just got a tool result, react; otherwise start
        if state["messages"] and isinstance(state["messages"][-1], ToolMessage):
            last = state["messages"][-1]
            name = (getattr(last, "name", "") or "").lower()
            data = _last_payload(last)

            # Track latest page URL for LLM context
            if name == "get_page_state" and isinstance(data, dict):
                state["page_url"] = data.get("url") or state["page_url"]

            # After initial snapshot → enumerate likely immunisation targets
            if (not state["planned"]) and name == "get_page_state":
                return {**state, "messages": [_ai_tool_call("find", {
                    "query": "immuni|immuniz|vaccin|record|cert|booster|jab|shot"
                })]}

            # After find → pick selector, then navigation-only tail
            if (not state["planned"]) and name == "find":
                matches = (data or {}).get("matches", []) if isinstance(data, dict) else []
                sel = _pick_selector_with_llm(state["goal"], state["page_url"], matches)
                if not sel:
                    # End gracefully to avoid loops
                    return {**state, "messages": [_ai_tool_call("done", {"reason": "No selector chosen by planner"})]}
                state["planned"] = True
                log.info("[imm_plan] chosen selector: %s", sel)
                return {**state, "messages": [
                    _ai_tool_call("click", {"selector": sel}),
                    _ai_tool_call("done", {"reason": "Clicked immunisation link"}),
                ]}

            if name == "done":
                return state

        # First entry: take one snapshot so we can plan from real context
        return {**state, "messages": [_ai_tool_call("get_page_state", {})]}

    # Graph wiring (identical shape to appointments planner)
    g = StateGraph(ImmPlanState)
    g.add_node("planner", node)
    g.add_node("tools", ToolNode(tools))
    g.add_edge(START, "planner")
    g.add_edge("planner", "tools")

    def router_after_tools(state: ImmPlanState):
        last = state["messages"][-1]
        if isinstance(last, ToolMessage) and (getattr(last, "name", "").lower() == "done"):
            return END
        return "planner"

    g.add_conditional_edges("tools", router_after_tools, {"planner": "planner", END: END})
    return g.compile()
