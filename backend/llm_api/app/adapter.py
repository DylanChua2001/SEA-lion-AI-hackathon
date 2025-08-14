# app/adapter.py
import json, uuid
from langchain_core.messages import AIMessage

def as_tool_call_ai_message(raw_text: str, allowed: set[str]) -> AIMessage:
    raw = raw_text.strip()
    if raw.startswith("```"):
        raw = raw.strip("` \n")
        i = raw.find("{")
        if i != -1:
            raw = raw[i:]
    data = json.loads(raw)
    name = data.get("tool")
    args = data.get("args", {})
    if name not in allowed or not isinstance(args, dict):
        raise ValueError(f"Bad tool or args. tool={name} allowed={sorted(allowed)} args={args}")
    return AIMessage(
        content="",
        tool_calls=[{
            "id": f"call_{uuid.uuid4().hex[:8]}",
            "type": "tool_call",
            "name": name,
            "args": args,
        }],
    )
