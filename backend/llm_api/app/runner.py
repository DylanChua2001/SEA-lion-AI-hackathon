# app/runner.py
import json
from typing import Any, Dict, List

from langchain_core.messages import AnyMessage, SystemMessage, HumanMessage
from .config import SYSTEM
from .graph import build_app_for_page
from .planner import messages_to_plan, done_reason

SAFE_RECURSION_LIMIT = 20


def _coerce_page(page_state: str | Dict[str, Any] | None) -> Dict[str, Any]:
    """Accepts a dict or JSON string and returns a minimal page dict."""
    if page_state is None:
        return {}
    if isinstance(page_state, str):
        try:
            return json.loads(page_state) or {}
        except Exception:
            return {}
    if isinstance(page_state, dict):
        return page_state
    return {}


def run_plan_once(goal: str, page_state: str | dict) -> dict:
    """
    Single-shot: build a plan from SYSTEM / GOAL / PAGE_STATE and return:
      { "steps": [ {tool, args}, ... ], "hint": { "summary"?: str } }

    No threads, no resumption, no human-in-the-loop.
    """
    page = _coerce_page(page_state)
    app = build_app_for_page(page)

    goal_text = (goal or "").strip() or "Find the Book Appointment button"
    page_url = (page.get("url") or "")
    page_title = (page.get("title") or "")

    msgs: List[AnyMessage] = [
        SystemMessage(SYSTEM),  # LLM must emit ONE valid tool call at a time (find/click/type/wait/done)
        HumanMessage(f"GOAL: {goal_text}"),
        HumanMessage(f"PAGE_STATE: {json.dumps({'url': page_url, 'title': page_title})}"),
    ]

    try:
        final_state = app.invoke({"messages": msgs}, config={"recursion_limit": SAFE_RECURSION_LIMIT})
        steps = messages_to_plan(final_state.get("messages", []))
        summary = done_reason(final_state.get("messages", []))
        return {"steps": steps or [], "hint": ({"summary": summary} if summary else {})}
    except Exception as e:
        # Gracefully degrade: no steps, but return a hint with the error summary.
        return {"steps": [], "hint": {"summary": f"Planner error: {e}"}}
