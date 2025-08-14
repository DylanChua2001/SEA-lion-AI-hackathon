import json
from typing import List, Optional
from uuid import uuid4
from langchain_core.messages import AnyMessage, SystemMessage, HumanMessage, AIMessage, ToolMessage
from .config import SYSTEM
from .graph import build_app_for_page
from .planner import messages_to_plan, done_reason


def run_one(
    goal: str,
    page_state: str | dict,
    last_tool: Optional[str] = None,
    last_obs: Optional[str] = None,
    *,
    user_reply: Optional[str] = None,
    thread_id: str = "web-agent",
):
    """
    Invoke the agent graph once. Supports human-in-the-loop resumes via:
      - user_reply: user's clarification after an interrupt()
      - thread_id:  stable id to resume from the same checkpoint

    Also threads client-side tool observations back to the graph as a ToolMessage:
      - last_tool: tool name that just ran in the browser (e.g., "find", "click")
      - last_obs:  JSON string of the observation ({ok:..., data:{...}} or {...})
    """
    page = json.loads(page_state) if isinstance(page_state, str) else page_state
    app = build_app_for_page(page)

    # First run: send SYSTEM/GOAL/PAGE_STATE.
    # Resume after interrupt: send ONLY the user's reply (keeps the thread state clean).
    if user_reply:
        msgs: List[AnyMessage] = [HumanMessage(user_reply)]
    else:
        msgs = [
            SystemMessage(SYSTEM),
            HumanMessage(f"GOAL: {goal}"),
            HumanMessage(f"PAGE_STATE: {json.dumps({'url': page.get('url'), 'title': page.get('title')})}"),
        ]

    # ⬇️ IMPORTANT: Do NOT append a ToolMessage on resume turns.
    # Only forward the previous tool observation when we're NOT resuming with user_reply.
    if not user_reply and last_tool and last_obs:
        try:
            obs_obj = json.loads(last_obs)
        except Exception:
            obs_obj = {"raw": last_obs}

        # Unwrap common shape {ok:..., data:{...}} → payload
        payload = obs_obj.get("data", obs_obj)
        try:
            payload_str = json.dumps(payload)
        except Exception:
            payload_str = str(payload)

        # LangChain ToolMessage requires a tool_call_id
        msgs.append(
            ToolMessage(
                name=last_tool,
                content=payload_str,
                tool_call_id=f"ext_{last_tool}_{uuid4().hex[:8]}",
            )
        )

    cfg = {"configurable": {"thread_id": thread_id}, "recursion_limit": 40}

    try:
        final_state = app.invoke({"messages": msgs}, config=cfg)

        plan = messages_to_plan(final_state["messages"])
        reason = done_reason(final_state["messages"])

        plan_msg = AIMessage(
            content=json.dumps({"steps": plan, "summary": reason}),
            additional_kwargs={},
            name="EXECUTION_PLAN",
        )
        return final_state["messages"] + [plan_msg]

    except Exception as e:
        # If the graph paused via interrupt(), LangGraph raises with the interrupt payload.
        # Pass through structured JSON (with options/examples) so the client can render it.
        raw = (e.args[0] if e.args else "") or ""
        try:
            payload = json.loads(raw) if raw else {}
            if isinstance(payload, dict):
                payload.setdefault("awaiting_user", True)
            else:
                payload = {"awaiting_user": True, "prompt": str(raw)}
        except Exception:
            payload = {
                "awaiting_user": True,
                "prompt": raw or "I need clarification to continue. What should I do next?",
            }

        interrupt_msg = AIMessage(
            content=json.dumps(payload),
            name="NEEDS_CLARIFICATION",
        )
        # No EXECUTION_PLAN while waiting for the user's clarification
        return [interrupt_msg]