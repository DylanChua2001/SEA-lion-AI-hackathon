# app/subgraphs/appointments.py
from __future__ import annotations
import json
import logging
from typing import Annotated, Dict, Any, List, Optional, TypedDict

from langgraph.graph import StateGraph, START, END
from langgraph.prebuilt import ToolNode
from langgraph.graph.message import add_messages
from langchain_core.messages import AIMessage, ToolMessage

log = logging.getLogger(__name__)
log.propagate = True
if not log.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("%(levelname)s %(process)d %(name)s: %(message)s"))
    log.addHandler(_h)
log.setLevel(logging.INFO)

TARGET_HOST = "eservices.healthhub.sg"
APPT_URL_TOKEN = "/appointments"          # match lower-cased path
IDLE_HINT = {"quietMs": 1100, "timeout": 15000}
MAX_TRIES = 8

def _ai(name: str, args: dict) -> AIMessage:
    return AIMessage(content="", tool_calls=[{
        "id": f"call_{name}",
        "type": "tool_call",
        "name": name,
        "args": args or {}
    }])

class ApptState(TypedDict):
    messages: Annotated[List, add_messages]
    tries: int
    last_url: Optional[str]

def _is_appt_url(u: Optional[str]) -> bool:
    if not isinstance(u, str):
        return False
    ul = u.lower()
    return (TARGET_HOST in ul) and (APPT_URL_TOKEN in ul)

def _last_payload(msg: ToolMessage) -> Dict[str, Any]:
    try:
        payload = json.loads(msg.content or "{}")
    except Exception:
        payload = {}
    return payload.get("data", payload) or {}

def _choose_best_match(matches: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """
    Prefer candidates that most likely open the Appointments workflow:
      1) href contains TARGET_HOST and /appointments
      2) href contains /appointments
      3) visible text contains 'appointment'
      4) otherwise first match
    """
    if not matches:
        return None

    def _href(m): return ((m or {}).get("href") or "").lower()
    def _text(m): return ((m or {}).get("text") or "").lower()

    # 1) full host + path
    for m in matches:
        h = _href(m)
        if (TARGET_HOST in h) and (APPT_URL_TOKEN in h):
            return m
    # 2) path only
    for m in matches:
        if APPT_URL_TOKEN in _href(m):
            return m
    # 3) text mention
    for m in matches:
        if "appointment" in _text(m):
            return m
    # 4) fallback
    return matches[0]

def build_appointments_subgraph(page: dict, tools: List):
    def node(state: ApptState) -> ApptState:
        state.setdefault("tries", 0)
        state.setdefault("last_url", "")

        if state.get("messages") and isinstance(state["messages"][-1], ToolMessage):
            last = state["messages"][-1]
            name = getattr(last, "name", "")
            data = _last_payload(last)

            # After click → idle + snapshot
            if name == "click":
                return {**state, "messages": [
                    _ai("wait_for_idle", IDLE_HINT),
                    _ai("get_page_state", {}),
                ]}

            # After wait/idle → snapshot
            if name in ("wait_for_idle", "wait"):
                return {**state, "messages": [_ai("get_page_state", {})]}

            # After snapshot: if already there, done; else search for Appointments
            if name == "get_page_state":
                url = (data.get("url") or "") if isinstance(data, dict) else ""
                state["last_url"] = url
                if _is_appt_url(url):
                    return {**state, "messages": [_ai("done", {"reason": "Arrived at Appointments"})]}
                # Not there yet → enumerate likely targets
                return {**state, "messages": [_ai("find", {"query": "appointment|appointments|booking|reschedule"})]}

            # After find: pick best match and click (selector only; no nav)
            if name == "find":
                matches = (data or {}).get("matches", []) if isinstance(data, dict) else []
                best = _choose_best_match(matches)
                sel = (best or {}).get("selector") or ""
                href = (best or {}).get("href") or ""
                log.info("[appointments] matches=%d pick selector=%r href=%r", len(matches), sel, href)

                if sel:
                    return {**state, "messages": [
                        _ai("click", {"selector": sel}),
                        _ai("wait_for_idle", IDLE_HINT),
                        _ai("get_page_state", {}),
                    ]}

                # No usable selector → retry a few times
                t = state.get("tries", 0) + 1
                if t >= MAX_TRIES:
                    return {**state, "messages": [_ai("done", {"reason": "Appointments link not found"})]}
                state["tries"] = t
                return {**state, "messages": [
                    _ai("wait", {"ms": 600}),
                    _ai("get_page_state", {}),
                ]}

            if name == "done":
                return state

        # First entry → snapshot
        return {**state, "messages": [_ai("get_page_state", {})]}

    g = StateGraph(ApptState)
    g.add_node("nav", node)
    g.add_node("tools", ToolNode(tools))
    g.add_edge(START, "nav")
    g.add_edge("nav", "tools")

    def _next(s: ApptState):
        if s.get("messages") and isinstance(s["messages"][-1], ToolMessage):
            if getattr(s["messages"][-1], "name", "") == "done":
                return END
        return "nav"

    g.add_conditional_edges("tools", _next, {"nav": "nav", END: END})
    return g.compile()
