# app/graph.py
import json
import uuid
from typing import Annotated, Dict, Any, List, Optional, TypedDict

from langgraph.graph import StateGraph, START, END
from langgraph.prebuilt import ToolNode, tools_condition
from langgraph.graph.message import add_messages
from langchain_core.messages import AnyMessage, HumanMessage, AIMessage, ToolMessage, SystemMessage

from app.tools import build_tools
from app.llm import make_llm
from app.adapter import as_tool_call_ai_message
from backend.llm_api.app.subgraphs.lab.lab_records import build_lab_records_subgraph

MAX_ITERATIONS = 12          # headroom for main loop
LAB_PREP_MAX_TRIES = 8       # how many times to poll snapshot after click
LAB_WAIT_MS = 600            # between polls
LAB_IDLE = {"quietMs": 900, "timeout": 9000}  # initial idle after nav

class AgentState(TypedDict):
    messages: Annotated[List[AnyMessage], add_messages]
    iteration_count: int
    lab_prep: bool               # we clicked Lab and are prepping snapshots
    lab_prep_tries: int
    lab_ready: bool              # snapshot ready to hand off
    # (optional) you could stash the compact snapshot here if you want:
    # lab_snapshot: Dict[str, Any]

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

def _done(reason: str) -> AIMessage:
    return _ai_tool_call("done", {"reason": reason})

def _is_lab_url(url: Optional[str]) -> bool:
    return isinstance(url, str) and "/lab-test-reports/lab" in url.lower()

def _ready_enough(snap: Dict[str, Any]) -> bool:
    if not snap or not isinstance(snap, dict):
        return False
    if not _is_lab_url(snap.get("url")):
        return False
    links = snap.get("links") or []
    headings = snap.get("headings") or []
    return (len(headings) >= 1) and (len(links) >= 5)

def build_app_for_page(page: dict):
    llm = make_llm(temperature=0)

    # Build all tools (so ToolNode can execute any), but restrict what the LLM may call
    all_tools = build_tools(page)
    MAIN_TOOL_NAMES = {"find", "click", "type", "wait", "done"}
    main_tools_for_llm = [t for t in all_tools if t.name in MAIN_TOOL_NAMES]

    # Subgraph for lab
    lab_graph = build_lab_records_subgraph(page)

    def agent_node(state: AgentState) -> AgentState:
        state.setdefault("messages", [])
        state.setdefault("iteration_count", 0)
        state.setdefault("lab_prep", False)
        state.setdefault("lab_prep_tries", 0)
        state.setdefault("lab_ready", False)

        state["iteration_count"] += 1

        # Handle last tool observation
        if state["messages"] and isinstance(state["messages"][-1], ToolMessage):
            last = state["messages"][-1]
            name = (getattr(last, "name", "") or "").lower()

            # Normalize tool payload for convenience
            try:
                payload = json.loads(last.content or "{}")
            except Exception:
                payload = {}
            data = payload.get("data", payload) or {}

            # 1) After click, detect Lab URL and start server-side prep (idle+poll snapshot)
            if name == "click":
                href = (data.get("href") or data.get("navigate_to") or "")
                if isinstance(href, str) and _is_lab_url(href):
                    # Start prep: wait_for_idle then get_page_state
                    state["lab_prep"] = True
                    state["lab_prep_tries"] = 0
                    return {**state, "messages": [
                        _ai_tool_call("wait_for_idle", LAB_IDLE),
                        _ai_tool_call("get_page_state", {})
                    ]}

            # 2) While in prep, keep polling until the Lab page is ready
            if state["lab_prep"]:
                if name == "get_page_state":
                    snap = data or {}
                    if _ready_enough(snap):
                        state["lab_ready"] = True
                        state["lab_prep"] = False
                        # (Optional) save snapshot if you want: state["lab_snapshot"] = snap
                        # Hand over to subgraph by just returning; routing handles it
                        return state
                    # Not ready → try again a few times
                    tries = state.get("lab_prep_tries", 0) + 1
                    state["lab_prep_tries"] = tries
                    if tries < LAB_PREP_MAX_TRIES:
                        return {**state, "messages": [
                            _ai_tool_call("wait", {"ms": LAB_WAIT_MS}),
                            _ai_tool_call("get_page_state", {})
                        ]}
                    # Give up gracefully; let subgraph handle its own waiting as a fallback
                    state["lab_ready"] = False
                    state["lab_prep"] = False
                    return state

                # After the initial wait_for_idle, the next ToolMessage will be get_page_state
                if name == "wait_for_idle":
                    return {**state, "messages": [_ai_tool_call("get_page_state", {})]}

        # Safety stop
        if state["iteration_count"] >= MAX_ITERATIONS:
            return {**state, "messages": [_done("Max steps reached.")]}

        # Normal LLM planning turn (restricted tools)
        # Note: we do NOT expose heavy tools to the LLM; only to ToolNode.
        system_hint = SystemMessage(content=(
            "You can call only these tools: find(query), click(selector), type(selector,text), wait(seconds|ms), done(reason)."
        ))
        resp = llm.invoke([system_hint] + state["messages"])
        ai_msg = as_tool_call_ai_message(resp.content, MAIN_TOOL_NAMES)
        return {**state, "messages": [ai_msg]}

    # Route to subgraph when lab is ready (or you can allow subgraph to wait further)
    def router_after_tools(state: AgentState):
        # If the last tool produced 'done', end.
        last = state["messages"][-1]
        if isinstance(last, ToolMessage) and (getattr(last, "name", "").lower() == "done"):
            return END
        # If lab snapshot is ready, jump into lab subgraph
        if state.get("lab_ready"):
            return "lab"
        return "agent"

    g = StateGraph(AgentState)
    g.add_node("agent", agent_node)
    g.add_node("tools", ToolNode(all_tools))      # ToolNode has ALL tools
    g.add_node("lab", lab_graph)                  # compiled subgraph

    g.add_edge(START, "agent")
    g.add_conditional_edges("agent", tools_condition, {"tools": "tools", END: END})
    g.add_conditional_edges("tools", router_after_tools, {"agent": "agent", "lab": "lab", END: END})
    g.add_edge("lab", END)

    return g.compile()
