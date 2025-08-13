# app/graph.py
import json
import uuid
from typing import TypedDict, Annotated, List, Optional

from langgraph.graph import StateGraph, START, END
from langgraph.prebuilt import ToolNode, tools_condition
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph.message import add_messages
from langchain_core.messages import AnyMessage, HumanMessage, AIMessage, ToolMessage

from .llm import make_llm
from .config import SCHEMA_HINT
from .normalizer import llm_normalize_goal
from .tools import build_tools
from .adapter import as_tool_call_ai_message
from .utils import safe_excerpt, norm_text

MAX_ITERATIONS = 10
CHECKPOINTER = MemorySaver()


class AgentState(TypedDict):
    messages: Annotated[List[AnyMessage], add_messages]
    iteration_count: int
    feedback_required: bool
    pending_options: Optional[List[dict]]


def _build_page_vocab(page: dict, max_items: int = 80) -> List[str]:
    """Local page vocab builder to keep this module decoupled from normalizer."""
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


def build_app_for_page(page: dict):
    """
    Minimal agent graph that ALWAYS follows one of four workflows:
      - appointments | lab_results | payments | immunisations

    The routing + plan are produced upstream by llm_normalize_goal(), which rewrites
    the user's GOAL to a deterministic tool plan (find→click→[wait]→find→click→done).
    """
    llm = make_llm(temperature=0)
    tools = build_tools(page)
    allowed = {t.name for t in tools}
    page_vocab = _build_page_vocab(page)

    def _ensure_defaults(state: AgentState) -> AgentState:
        state.setdefault("iteration_count", 0)
        state.setdefault("feedback_required", False)
        state.setdefault("messages", [])
        state.setdefault("pending_options", None)
        return state

    def _auto_click(selector: str, state: AgentState) -> AgentState:
        return {
            **state,
            "messages": [AIMessage(content="", tool_calls=[{
                "id": f"call_click_{uuid.uuid4().hex[:6]}",
                "type": "tool_call",
                "name": "click",
                "args": {"selector": selector},
            }])]
        }

    def _ask_user(prompt: str, state: AgentState, options: Optional[List[dict]] = None, examples: Optional[List[str]] = None) -> AgentState:
        # langgraph interrupt for human-in-the-loop
        from langgraph.types import interrupt  # local import keeps top clean
        payload = {"awaiting_user": True, "prompt": prompt}
        if options is not None:
            payload["options"] = options
        if examples is not None:
            payload["examples"] = examples
        interrupt(json.dumps(payload))
        state["feedback_required"] = True
        return state

    # ── Nodes ─────────────────────────────────────────────────────────────────

    def normalize_node(state: AgentState) -> AgentState:
        """
        Rewrite any GOAL to a canonical four-path plan via llm_normalize_goal().
        Example output (appointments):
          find('Appointments') then click the best match, then wait(600), find('Book Appointment') ...
        """
        state = _ensure_defaults(state)
        if not state["messages"]:
            return state

        new_msgs = list(state["messages"])
        for i, msg in enumerate(new_msgs):
            if isinstance(msg, HumanMessage) and isinstance(msg.content, str) and msg.content.startswith("GOAL:"):
                raw_goal = msg.content[len("GOAL:"):].strip()
                canon = llm_normalize_goal(raw_goal, page_vocab)
                # If for any reason normalizer returns None, default to appointments entry
                plan = canon or "find('appointments') then click the best match, then done"
                new_msgs[i] = HumanMessage(f"GOAL: {plan}")
                break
        state["messages"] = new_msgs
        return state

    def agent_node(state: AgentState) -> AgentState:
        """
        Execute the plan strictly via tool calls.
        - On find(): if 0 matches → ask user; if 1 match → auto-click; else show options.
        - On click(): continue; if click fails → ask user.
        - Stop on 'done'.
        """
        state = _ensure_defaults(state)

        # Resume after human feedback
        if state.get("feedback_required"):
            ans_msg = next((m for m in reversed(state["messages"]) if isinstance(m, HumanMessage)), None)
            if ans_msg:
                ans = (ans_msg.content or "").strip()
                opts = state.get("pending_options") or []
                chosen = None
                if ans.isdigit() and opts:
                    idx = int(ans) - 1
                    if 0 <= idx < len(opts):
                        chosen = opts[idx]
                if chosen and chosen.get("selector"):
                    state["feedback_required"] = False
                    state["pending_options"] = None
                    return _auto_click(chosen["selector"], state)
                return _ask_user("I couldn't match your reply to an option.", state, options=opts)
            return _ask_user("Still waiting for your choice.", state, options=state.get("pending_options") or [])

        state["iteration_count"] += 1

        # Handle tool results
        if state["messages"] and isinstance(state["messages"][-1], ToolMessage):
            last = state["messages"][-1]
            last_name = (getattr(last, "name", "") or "").lower()

            if last_name == "done":
                return {**state, "messages": [AIMessage(content="✅ Finished.")]}

            if last_name == "find":
                payload = json.loads(last.content or "{}")
                payload = payload.get("data", payload)
                total = int(payload.get("total", 0))
                matches = payload.get("matches", []) or []

                if total <= 0:
                    return _ask_user("I couldn't find anything. What should I search or click instead?", state)

                if total == 1 and matches[0].get("selector"):
                    return _auto_click(matches[0]["selector"], state)

                # Multiple options → ask user to pick
                opts = [{"n": i + 1,
                         "label": (m.get("text") or "").strip(),
                         "selector": m.get("selector") or ""} for i, m in enumerate(matches[:6])]
                state["pending_options"] = opts
                return _ask_user("I found several matches. Please pick a number or type a command.", state, options=opts)

            if last_name == "click":
                payload = json.loads(last.content or "{}")
                payload = payload.get("data", payload)
                if not payload.get("ok", True):
                    sel = payload.get("selector") or ""
                    return _ask_user(f"Click failed for '{sel}'. What should I click instead?", state)

        if state["iteration_count"] >= MAX_ITERATIONS:
            return _ask_user("I've tried multiple steps. Please guide me on the exact next action.", state)

        # Normal LLM step: generate the next tool call following the canonical plan
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
            HumanMessage(SCHEMA_HINT),
        ]
        resp = llm.invoke(messages)
        ai_msg = as_tool_call_ai_message(resp.content, allowed)
        return {**state, "messages": [ai_msg]}

    def after_tools(state: AgentState):
        last = state["messages"][-1]
        if isinstance(last, ToolMessage) and (getattr(last, "name", "").lower() in ("done",)):
            return END
        return "agent"

    # ── Graph wiring ──────────────────────────────────────────────────────────
    g = StateGraph(AgentState)
    g.add_node("normalize", normalize_node)
    g.add_node("agent", agent_node)
    g.add_node("tools", ToolNode(tools))

    g.add_edge(START, "normalize")
    g.add_edge("normalize", "agent")
    g.add_conditional_edges("agent", tools_condition, {"tools": "tools", END: END})
    g.add_conditional_edges("tools", after_tools, {"agent": "agent", END: END})

    return g.compile(checkpointer=CHECKPOINTER)
