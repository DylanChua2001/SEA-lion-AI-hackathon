import json
from typing import List, Optional
from langchain_core.messages import AnyMessage, AIMessage, ToolMessage

def messages_to_plan(msgs: List[AnyMessage]) -> List[dict]:
    steps: List[dict] = []
    for m in msgs:
        if isinstance(m, AIMessage) and getattr(m, "tool_calls", None):
            for tc in m.tool_calls:
                name = tc.get("name")
                args = tc.get("args", {})
                if name in ("find", "click", "type"):
                    steps.append({"tool": name, "args": args})
                elif name == "done":
                    steps.append({"tool": "done", "args": args})
                    return steps
    return steps

def done_reason(msgs: List[AnyMessage]) -> Optional[str]:
    for m in msgs[::-1]:
        if isinstance(m, ToolMessage) and getattr(m, "name", "") == "done":
            try:
                payload = json.loads(m.content or "{}")
                return payload.get("reason")
            except Exception:
                return None
    return None
