from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from .schemas import AgentStepRequest, AgentStepResponse
from .agent import step

app = FastAPI(title="Agentic HealthHub API")

# --- CORS ---
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # You can restrict this to your extension's origin
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/health")
async def health():
    return {"ok": True}

@app.post("/agent/step", response_model=AgentStepResponse)
async def agent_step(req: AgentStepRequest):
    return await step(req)
