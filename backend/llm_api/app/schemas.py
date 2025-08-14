# app/schemas.py
from typing import Any, Optional, Union
from pydantic import BaseModel, Field

class AgentMessage(BaseModel):
    type: str
    content: Optional[Union[str, list[Any], dict]] = None
    tool_calls: Optional[Any] = None   # LangChain puts a list[dict] here
    name: Optional[str] = None

class AgentRunRequest(BaseModel):
    goal: str
    # Accept the scraped page either as a dict (preferred) or as a JSON string
    page_state: Union[dict, str]
    last_tool: Optional[str] = None
    last_observation: Optional[str] = Field(default=None, alias="last_obs")  # allow either key
    # NEW: for interrupt/resume
    user_reply: Optional[str] = None                     # user's clarification when resuming
    thread_id: Optional[str] = Field(default="web-agent")# keep constant across resumes

    class Config:
        populate_by_name = True  # let FastAPI accept last_obs or last_observation

class AgentRunResponse(BaseModel):
    messages: list[AgentMessage]