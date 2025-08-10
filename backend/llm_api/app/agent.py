from typing import TypedDict
from langgraph.graph import StateGraph
from .schemas import AgentStepRequest, AgentStepResponse, ToolCall
from .llm import SeaLionChat

llm = SeaLionChat()

SYSTEM = """You are a web task agent. You cannot execute actions yourself.
You propose a single tool call at a time in JSON strictly matching the schema.
Favor robust selectors. If unsure, use `find` first to list candidate elements, then `click`/`type`.
Stop with tool=done when the goal is achieved."""

def format_messages(req: AgentStepRequest):
    msgs = [{"role": "system", "content": SYSTEM}]
    msgs += [
        {"role": "user", "content": f"GOAL: {req.goal}"},
        {"role": "user", "content": f"PAGE_STATE: {req.page_state}"},
    ]
    if req.last_tool:
        msgs.append({"role": "user", "content": f"LAST_TOOL: {req.last_tool.model_dump()}"})
    if req.last_observation:
        msgs.append({"role": "user", "content": f"OBS: {req.last_observation.model_dump()}"})
    msgs.append({
        "role": "user",
        "content": "Return ONLY a JSON object with keys: tool, args. Example: {\"tool\":\"find\",\"args\":{\"query\":\"button: Appointments\"}}"
    })
    return msgs

async def step(req: AgentStepRequest) -> AgentStepResponse:
    # ask LLM for the next tool call
    content = await llm.chat(messages=format_messages(req), temperature=0.1, max_tokens=300)
    # be defensive: strip code fences or extra text
    import json, re
    m = re.search(r"\{.*\}", content, re.S)
    payload = json.loads(m.group(0)) if m else {"tool":"fail","args":{"reason":"invalid LLM output","raw":content}}
    tc = ToolCall.model_validate(payload)
    return AgentStepResponse(next=tc, thoughts="ok")
