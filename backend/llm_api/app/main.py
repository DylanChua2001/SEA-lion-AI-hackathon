# app/main.py
import os
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.tools import set_latest_snapshot

from .schemas import AgentRunRequest, AgentPlanResponse
from .runner import run_plan_once

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

@app.post("/agent/run", response_model=AgentPlanResponse)
async def agent_run(req: AgentRunRequest) -> AgentPlanResponse:
    """
    Stateless, single-shot planning endpoint.
    Returns:
      { "steps": [ { "tool": str, "args": dict }, ... ],
        "hint":  { "summary"?: str } }
    """
    plan = run_plan_once(goal=req.goal, page_state=req.page_state)
    # plan already matches AgentPlanResponse schema
    return AgentPlanResponse(**plan)

@app.post("/bridge/snapshot")
async def bridge_snapshot(payload: dict):
    set_latest_snapshot(payload)
    return {"ok": True}