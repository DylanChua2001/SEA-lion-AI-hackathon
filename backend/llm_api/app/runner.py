import json
from typing import List, Optional
from langchain_core.messages import AnyMessage, SystemMessage, HumanMessage, AIMessage
from .config import SYSTEM
from .graph import build_app_for_page
from .planner import messages_to_plan, done_reason

def run_one(goal: str, page_state: str | dict, last_tool: Optional[str] = None, last_obs: Optional[str] = None):
    page = json.loads(page_state) if isinstance(page_state, str) else page_state
    app = build_app_for_page(page)

    msgs: List[AnyMessage] = [
        SystemMessage(SYSTEM),
        HumanMessage(f"GOAL: {goal}"),
        HumanMessage(f"PAGE_STATE: {json.dumps({'url': page.get('url'), 'title': page.get('title')})}"),
    ]
    if last_tool:
        msgs.append(HumanMessage(f"LAST_TOOL: {last_tool}"))
    if last_obs:
        msgs.append(HumanMessage(f"OBS: {last_obs}"))

    cfg = {"configurable": {"thread_id": "web-agent"}, "recursion_limit": 10}
    final_state = app.invoke({"messages": msgs}, config=cfg)

    plan = messages_to_plan(final_state["messages"])
    reason = done_reason(final_state["messages"])

    plan_msg = AIMessage(
        content=json.dumps({"steps": plan, "summary": reason}),
        additional_kwargs={},
        name="EXECUTION_PLAN",
    )

    return final_state["messages"] + [plan_msg]
