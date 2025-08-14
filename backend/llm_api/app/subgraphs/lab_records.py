# app/subgraphs/lab_records.py
import json
import uuid
from typing import TypedDict, Annotated, List, Optional, Dict, Any

from langgraph.graph import StateGraph, START, END
from langgraph.prebuilt import ToolNode, tools_condition
from langgraph.graph.message import add_messages
from langchain_core.messages import AnyMessage, HumanMessage, AIMessage, ToolMessage, SystemMessage

from app.tools import build_tools
from app.llm import make_llm
from app.adapter import as_tool_call_ai_message

# Retry a few times so we snapshot AFTER the SPA finishes rendering.
MAX_ITERATIONS = 10


class LabState(TypedDict):
    messages: Annotated[List[AnyMessage], add_messages]
    iteration_count: int


def _ai_tool_call(name: str, args: dict) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{
            "id": f"call_{name}_{uuid.uuid4().hex[:6]}",
            "type": "tool_call",
            "name": name,
            "args": args or {},
        }],
    )


SUBGRAPH_HINT = (
    "You are extracting lab report entries from a HealthHub 'Lab Reports' page snapshot.\n"
    "The only tool you can call is: done(reason).\n\n"
    "Input:\n"
    "- PAGE_SNAPSHOT_JSON: a compact JSON object with {url, title, headings, links, buttons}.\n"
    "- links: {text, href} pairs (already de-duplicated and truncated).\n"
    "- buttons: labels that may include CTAs like 'View', 'Download', etc.\n\n"
    "Task:\n"
    "1) Identify items that look like actual lab/test reports (e.g., links or rows labeled 'View', 'View Report', "
    "'Results', 'Report', 'PDF', possibly accompanied by a test name and/or date).\n"
    "2) Prefer the most specific label available (test name + date if present). If only a generic 'View' anchor is "
    "available, pair it with the most informative text in the same link label.\n"
    "3) Produce a minimal list: [{\"label\":\"...\",\"href\":\"...\"}] with 3–15 items if available.\n"
    "4) Output EXACTLY ONE tool call: done with args {\"reason\": \"<JSON string>\"} where the JSON string is:\n"
    "   {\"lab_reports\":[{\"label\":\"...\",\"href\":\"...\"},...],\"source_url\":\"<snapshot url>\",\"count\":<int>}.\n"
    "NO prose, NO markdown. Only the done() tool call."
)


def _compact_snapshot_for_llm(snap: Dict[str, Any], *, max_links: int = 120, max_buttons: int = 60) -> Dict[str, Any]:
    def norm(s: Optional[str]) -> str:
        return (" ".join((s or "").split())).strip()

    links_raw = snap.get("links") or []
    buttons_raw = snap.get("buttons") or []
    headings_raw = snap.get("headings") or []

    links: List[Dict[str, str]] = []
    seen = set()
    for x in links_raw:
        text = norm(x.get("text"))
        href = (x.get("href") or "").strip()
        if not href:
            continue
        sig = (text[:100], href)
        if sig in seen:
            continue
        seen.add(sig)
        links.append({"text": text[:160], "href": href})
        if len(links) >= max_links:
            break

    buttons: List[str] = []
    seenb = set()
    for b in buttons_raw:
        t = norm(b.get("text"))
        if not t or t in seenb:
            continue
        seenb.add(t)
        buttons.append(t[:80])
        if len(buttons) >= max_buttons:
            break

    headings = [norm(h.get("text"))[:160] for h in (headings_raw or []) if norm(h.get("text"))]

    return {
        "url": snap.get("url"),
        "title": snap.get("title"),
        "headings": headings[:12],
        "links": links,
        "buttons": buttons,
    }


def _is_lab_url(url: Optional[str]) -> bool:
    return isinstance(url, str) and "/lab-test-reports/lab" in url.lower()


def _ready_enough(snap: Dict[str, Any]) -> bool:
    # Require the Lab page URL AND enough content before we proceed
    if not snap or not isinstance(snap, dict):
        return False
    if not _is_lab_url(snap.get("url")):
        return False
    links = snap.get("links") or []
    headings = snap.get("headings") or []
    return (len(headings) >= 1) and (len(links) >= 5)


def _done_json(obj: Dict[str, Any]) -> AIMessage:
    return _ai_tool_call("done", {"reason": json.dumps(obj, ensure_ascii=False)})


def build_lab_records_subgraph(page: dict):
    """
    Subgraph for 'Lab Reports' listing:
      1) wait_for_idle → get_page_state (fresh snapshot; retry until ready & on correct URL)
      2) LLM extracts a compact list of lab report entries
      3) done(reason=<JSON string with {lab_reports, source_url, count}>)
    """
    all_tools = build_tools(page)   # includes get_page_state, wait_for_idle, wait, done, etc.

    # Restrict to just what this subgraph needs
    needed = {"wait_for_idle", "wait", "get_page_state", "done"}
    subgraph_tools = [t for t in all_tools if t.name in needed]

    llm = make_llm(temperature=0)
    allowed = {"done"}

    def seed(state: LabState) -> LabState:
        state.setdefault("messages", [])
        state.setdefault("iteration_count", 0)
        # Give the SPA a moment to settle before the first snapshot
        return {**state, "messages": [_ai_tool_call("wait_for_idle", {"quietMs": 700, "timeout": 6000})]}

    def agent(state: LabState) -> LabState:
        state["iteration_count"] = state.get("iteration_count", 0) + 1

        if state["messages"] and isinstance(state["messages"][-1], ToolMessage):
            last = state["messages"][-1]
            name = (getattr(last, "name", "") or "").lower()

            if name == "done":
                return {**state, "messages": [AIMessage(content="✅ Lab list finished.")]}

            # Normalize tool payload
            try:
                payload = json.loads(last.content or "{}")
            except Exception:
                payload = {}
            data = payload.get("data", payload) or {}

            if name in ("wait_for_idle",):
                # After idle, snapshot
                return {**state, "messages": [_ai_tool_call("get_page_state", {})]}

            if name == "get_page_state":
                snap = data or {}

                # If not on the Lab URL or not enough content yet, wait & retry
                if not _ready_enough(snap) and state["iteration_count"] < MAX_ITERATIONS:
                    return {**state, "messages": [
                        _ai_tool_call("wait", {"ms": 600}),
                        _ai_tool_call("get_page_state", {})
                    ]}

                # Build a MINIMAL prompt — DO NOT include prior ToolMessages
                compact = _compact_snapshot_for_llm(snap)
                prompt: List[AnyMessage] = [
                    SystemMessage(content="Return exactly one tool call to done(reason=JSON). No prose."),
                    HumanMessage(content=f"PAGE_SNAPSHOT_JSON: {json.dumps(compact, ensure_ascii=False)}"),
                    HumanMessage(content=SUBGRAPH_HINT),
                ]
                resp = llm.invoke(prompt)
                try:
                    ai_msg = as_tool_call_ai_message(resp.content, allowed)
                    if ai_msg.tool_calls and (ai_msg.tool_calls[0].get("name") or "").lower() == "done":
                        return {**state, "messages": [ai_msg]}
                except Exception:
                    pass

                # Fallback if the model didn't produce a done tool call
                return {**state, "messages": [_done_json({
                    "lab_reports": [],
                    "source_url": compact.get("url"),
                    "count": 0
                })]}

            if name == "wait":
                # After a wait, try snapshot again
                return {**state, "messages": [_ai_tool_call("get_page_state", {})]}

        # Safety cap
        if state.get("iteration_count", 0) >= MAX_ITERATIONS:
            return {**state, "messages": [_done_json({
                "lab_reports": [],
                "source_url": None,
                "count": 0,
                "note": "Lab listing: max steps reached."
            })]}

        # Default: try snapshot once
        return {**state, "messages": [_ai_tool_call("get_page_state", {})]}

    def after_tools(state: LabState):
        last = state["messages"][-1]
        if isinstance(last, ToolMessage) and (getattr(last, "name", "").lower() == "done"):
            return END
        return "agent"

    g = StateGraph(LabState)
    g.add_node("seed", seed)
    g.add_node("agent", agent)
    g.add_node("tools", ToolNode(subgraph_tools))  # restricted tools

    g.add_edge(START, "seed")
    g.add_edge("seed", "agent")
    g.add_conditional_edges("agent", tools_condition, {"tools": "tools", END: END})
    g.add_conditional_edges("tools", after_tools, {"agent": "agent", END: END})

    return g.compile()
