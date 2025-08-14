# app/graph.py
import json
import uuid
from typing import TypedDict, Annotated, List, Optional

from langgraph.graph import StateGraph, START, END
from langgraph.prebuilt import ToolNode, tools_condition
from langgraph.graph.message import add_messages
from langchain_core.messages import AnyMessage, HumanMessage, AIMessage, ToolMessage

from .llm import make_llm
from .config import SCHEMA_HINT
from .normalizer import llm_normalize_goal
from .tools import build_tools
from .adapter import as_tool_call_ai_message
from .utils import safe_excerpt, norm_text

MAX_ITERATIONS = 6  # keep plans tight


class AgentState(TypedDict):
    messages: Annotated[List[AnyMessage], add_messages]
    iteration_count: int


# ────────────────────────── helpers ──────────────────────────────────────────
def _build_page_vocab(page: dict, max_items: int = 80) -> List[str]:
    """Lightweight page vocab; keeps module decoupled from normalizer."""
    seen, out = set(), []

    def add(txt: Optional[str]):
        t = norm_text(txt)
        if not t:
            return
        k = t.lower()
        if k in seen:
            return
        seen.add(k)
        out.append(t)

    for item in (page.get("clickables_preview") or []):
        add(item.get("text"))
    for b in (page.get("buttons") or []):
        add(b.get("text"))
    for a in (page.get("links") or []):
        add(a.get("text"))
    for i in (page.get("inputs") or []):
        add(i.get("name") or i.get("placeholder"))

    raw = page.get("raw_html") or ""
    if isinstance(raw, str) and raw:
        import re
        for m in re.finditer(r'<a\b[^>]*>(.*?)</a\s*>', raw, flags=re.I | re.S):
            add(re.sub(r'<[^>]+>', '', m.group(1)))
        for m in re.finditer(r'<button\b[^>]*>(.*?)</button\s*>', raw, flags=re.I | re.S):
            add(re.sub(r'<[^>]+>', '', m.group(1)))
        for m in re.finditer(r'(?:aria-label|placeholder|alt)\s*=\s*["\']([^"\']{2,80})["\']', raw, flags=re.I):
            add(m.group(1))

    return out[:max_items]


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
    return _ai_tool_call("done", {"reason": reason or "Stopped."})


# ────────────────────────── graph builder ────────────────────────────────────
def build_app_for_page(page: dict):
    """
    Stateless, turn-free agent:
      - Normalizes arbitrary GOAL to one of four rails via llm_normalize_goal().
      - Executes strictly via tools (find → click → wait/type as needed).
      - On ambiguity/error: stops with done(reason) — no user clarification mid-run.
    """
    llm = make_llm(temperature=0)
    tools = build_tools(page)
    allowed = {t.name for t in tools}
    page_vocab = _build_page_vocab(page)

    def _ensure_defaults(state: AgentState) -> AgentState:
        state.setdefault("iteration_count", 0)
        state.setdefault("messages", [])
        return state

    # ── Nodes ────────────────────────────────────────────────────────────────
    def normalize_node(state: AgentState) -> AgentState:
        """
        Rewrite GOAL into a canonical four-path plan (appointments/lab_results/payments/immunisations).
        Example plan: "find('Appointments') then click the best match, then wait(600), then find('Book Appointment')..."
        """
        state = _ensure_defaults(state)
        if not state["messages"]:
            return state

        new_msgs = list(state["messages"])
        for i, msg in enumerate(new_msgs):
            if isinstance(msg, HumanMessage) and isinstance(msg.content, str) and msg.content.startswith("GOAL:"):
                raw_goal = msg.content[len("GOAL:"):].strip()
                canon = llm_normalize_goal(raw_goal, page_vocab)
                plan = canon or "find('appointments') then click the best match, then done"
                new_msgs[i] = HumanMessage(f"GOAL: {plan}")
                break
        state["messages"] = new_msgs
        return state

    def agent_node(state: AgentState) -> AgentState:
        """
        Deterministic execution with no human-in-the-loop:
          - find(): 0 → done; ≥1 → auto-pick first with selector.
          - click(): if ok=False → done(reason).
          - stop on 'done'.
        """
        state = _ensure_defaults(state)
        state["iteration_count"] += 1

        # Handle tool observations
        if state["messages"] and isinstance(state["messages"][-1], ToolMessage):
            last = state["messages"][-1]
            name = (getattr(last, "name", "") or "").lower()

            if name == "done":
                # Terminal; emit a short acknowledgment so planner can summarize
                return {**state, "messages": [AIMessage(content="✅ Finished.")]}

            # Standardize payload shape
            try:
                payload = json.loads(last.content or "{}")
            except Exception:
                payload = {}
            data = payload.get("data", payload) or {}

            if name == "find":
                total = int(data.get("total", 0))
                matches = data.get("matches", []) or []
                if total <= 0:
                    return {**state, "messages": [_done("No matching elements were found.")]}

                # Prefer first with a usable selector
                first = next((m for m in matches if m.get("selector")), None)
                if not first:
                    return {**state, "messages": [_done("Matches lacked usable selectors.")]}
                return {**state, "messages": [_ai_tool_call("click", {"selector": first["selector"]})]}

            if name == "click":
                if not data.get("ok", True):
                    sel = data.get("selector") or ""
                    return {**state, "messages": [_done(f"Click failed for '{sel}'.")]}
                # Successful click: LLM will choose next step from schema hint below

        # Safety cap
        if state["iteration_count"] >= MAX_ITERATIONS:
            return {**state, "messages": [_done("Max steps reached.")]}

        # Ask LLM for the next tool call following the normalized plan
        raw_html_excerpt = safe_excerpt(page.get("raw_html", ""), max_chars=400) if isinstance(page.get("raw_html"), str) else ""
        page_context = {
            "url": page.get("url"),
            "title": page.get("title"),
            "counts": {
                "buttons": len(page.get("buttons", [])),
                "links": len(page.get("links", [])),
                "inputs": len(page.get("inputs", [])),
                "vocab": len(page_vocab),
            },
            "vocab_top": page_vocab[:30],
            "samples": {"buttons": page.get("buttons", [])[:12], "inputs": page.get("inputs", [])[:8]},
            "raw_html_excerpt": raw_html_excerpt,
        }

        messages = state["messages"] + [
            HumanMessage(f"PAGE_CONTEXT_JSON: {json.dumps(page_context, ensure_ascii=False)}"),
            HumanMessage(SCHEMA_HINT),  # strict schema: emit ONE tool call JSON (find/click/type/wait/done)
        ]
        resp = llm.invoke(messages)
        ai_msg = as_tool_call_ai_message(resp.content, allowed)

        return {**state, "messages": [ai_msg]}

    def after_tools(state: AgentState):
        last = state["messages"][-1]
        if isinstance(last, ToolMessage) and (getattr(last, "name", "").lower() == "done"):
            return END
        return "agent"

    # ── Wiring ───────────────────────────────────────────────────────────────
    g = StateGraph(AgentState)
    g.add_node("normalize", normalize_node)
    g.add_node("agent", agent_node)
    g.add_node("tools", ToolNode(tools))

    g.add_edge(START, "normalize")
    g.add_edge("normalize", "agent")
    g.add_conditional_edges("agent", tools_condition, {"tools": "tools", END: END})
    g.add_conditional_edges("tools", after_tools, {"agent": "agent", END: END})

    # No checkpointer: truly stateless per request
    return g.compile()
