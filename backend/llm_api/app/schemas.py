# app/schemas.py
from typing import Any, Dict, List, Optional, Union
from pydantic import BaseModel

class AgentRunRequest(BaseModel):
    goal: str
    page_state: Union[Dict[str, Any], str]
    # keep optional fields to avoid breaking existing callers; ignored by stateless server
    current_url: Optional[str] = None
    last_tool: Optional[str] = None
    last_observation: Optional[str] = None
    user_reply: Optional[str] = None
    thread_id: Optional[str] = None

class Step(BaseModel):
    tool: str
    args: Dict[str, Any] = {}

class AgentPlanResponse(BaseModel):
    steps: List[Step] = []
    hint: Dict[str, Any] = {}
