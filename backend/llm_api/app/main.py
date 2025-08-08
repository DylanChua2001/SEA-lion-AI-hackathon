# main.py (at repo root)
from fastapi import FastAPI, HTTPException, Depends

import os
from langchain.globals import set_llm_cache
from langchain.cache import SQLiteCache
if os.getenv("LC_CACHE", "1") == "1":  # set LC_CACHE=0 to disable
    set_llm_cache(SQLiteCache(database_path="/app/.lc_cache.sqlite"))
    
from app.config.settings import settings
from app.clients.sea_lion import SeaLionClient
from app.services.chat_service import ChatService
from pydantic import BaseModel
from typing import List, Optional, Dict, Any

class Message(BaseModel):
    role: str
    content: str

class ChatRequest(BaseModel):
    messages: List[Message]
    model: Optional[str] = None
    thinking_mode: Optional[str] = None
    no_cache: Optional[bool] = False
    temperature: Optional[float] = 0.2
    max_tokens: Optional[int] = 512
    extra_body: Optional[Dict[str, Any]] = None

class ChatResponse(BaseModel):
    content: str
    raw: Optional[Dict[str, Any]] = None

class PromptRequest(BaseModel):
    prompt: str

def get_chat_service():
    client = SeaLionClient(
        api_key=settings.SEA_LION_API_KEY,
        base_url=settings.SEA_LION_BASE_URL,
        model=settings.SEA_LION_MODEL,
        timeout_s=settings.TIMEOUT_S,
    )
    return ChatService(client)

app = FastAPI(title="SEA-LION LLM API", version="1.0.0")

@app.get("/health")
def health():
    return {"status": "ok"}

@app.post("/chat")
def chat(req: PromptRequest, svc: ChatService = Depends(get_chat_service)):
    try:
        # Build messages with default system + user prompt
        messages = [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": req.prompt}
        ]
        
        # Call service with default params
        data = svc.run(
            messages=messages,
            thinking_mode=None,
            temperature=0.3,
            max_tokens=100,
            no_cache=True,
            extra_body={}
        )
        return {"content": data["content"]}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

