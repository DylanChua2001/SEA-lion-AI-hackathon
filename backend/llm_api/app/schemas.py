from pydantic import BaseModel, Field
from typing import Literal, List, Optional, Dict, Any

ToolName = Literal["get_page_state", "find", "click", "type", "wait_for", "nav", "done", "fail"]

class ToolCall(BaseModel):
    tool: ToolName
    args: Dict[str, Any] = Field(default_factory=dict)

class Observation(BaseModel):
    # what the runner reports back (result of the previous tool call)
    ok: bool
    data: Dict[str, Any] = Field(default_factory=dict)

class AgentStepRequest(BaseModel):
    session_id: str
    goal: str
    last_tool: Optional[ToolCall] = None
    last_observation: Optional[Observation] = None
    # lightweight page context from extension (url, title, key text, affordances)
    page_state: Dict[str, Any] = Field(default_factory=dict)

class AgentStepResponse(BaseModel):
    next: ToolCall
    thoughts: str
