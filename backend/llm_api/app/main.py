import os
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .schemas import AgentRunRequest, AgentRunResponse, AgentMessage
from .runner import run_one

load_dotenv()

app = FastAPI(title="Agentic HealthHub API", version="1.0.0")

# CORS — restrict in prod
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["POST", "GET", "OPTIONS"],
    allow_headers=["*"],
)

@app.get("/health")
async def health():
    return {"ok": True}

def _serialize_messages(msgs) -> list[AgentMessage]:
    out = []
    for m in msgs:
        # Use class name as a stable "type" (e.g., "AIMessage", "ToolMessage")
        msg_type = m.__class__.__name__
        content = getattr(m, "content", None)
        tool_calls = getattr(m, "tool_calls", None)
        name = getattr(m, "name", None)
        out.append(AgentMessage(type=msg_type, content=content, tool_calls=tool_calls, name=name))
    return out

@app.post("/agent/run", response_model=AgentRunResponse)
async def agent_run(req: AgentRunRequest) -> AgentRunResponse:
    msgs = run_one(
        goal=req.goal,
        page_state=req.page_state,
        last_tool=req.last_tool,
        last_obs=req.last_observation,
        # NEW:
        user_reply=req.user_reply,
        thread_id=req.thread_id or "web-agent",
    )
    return AgentRunResponse(messages=_serialize_messages(msgs))
