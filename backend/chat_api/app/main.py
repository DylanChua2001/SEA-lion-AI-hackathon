from __future__ import annotations

import os
import re
from typing import Optional, Dict, Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from dotenv import load_dotenv

from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_core.runnables import Runnable
from langchain_core.runnables.history import RunnableWithMessageHistory
from langchain_openai import ChatOpenAI
from langchain_community.chat_message_histories import MongoDBChatMessageHistory

# ───────────────────────────── env ─────────────────────────────
load_dotenv()

MONGODB_URI = os.getenv("MONGODB_URI", "").strip()
DB_NAME = os.getenv("DB_NAME", "chat_service")
SEA_LION_API_BASE = os.getenv("SEA_LION_API_BASE", "").rstrip("/")
SEA_LION_API_KEY = os.getenv("SEA_LION_API_KEY", "")
SEA_LION_MODEL = os.getenv("SEA_LION_MODEL", "sealion-chat")

if not MONGODB_URI:
    raise RuntimeError("MONGODB_URI is not set")
if not SEA_LION_API_BASE:
    raise RuntimeError("SEA_LION_API_BASE is not set")
if not SEA_LION_API_KEY:
    raise RuntimeError("SEA_LION_API_KEY is not set")

# ───────────────────────────── app ─────────────────────────────
DEFAULT_SYSTEM_PROMPT = (
    "You are a calm, patient assistant for elderly users navigating "
    "Singapore e-government websites. Use simple language, step-by-step instructions, "
    "and mirror the user's latest message language. If information is missing, ask one concise question."
)

app = FastAPI(title="Sea Lion Chat API", version="1.5.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],           # tighten for production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─────────────────────────── schemas ───────────────────────────
class ChatRequest(BaseModel):
    thread_id: str
    message: str
    user_id: Optional[str] = None
    context: Optional[Dict[str, Any]] = None

class ChatResponse(BaseModel):
    thread_id: str
    reply: str

class SimpleChatRequest(BaseModel):
    thread_id: str = Field(..., description="Conversation/session id.")
    prompt: str = Field(..., description="User's latest message (any language).")

class SimpleChatResponse(BaseModel):
    thread_id: str
    reply: str

# ───────────────────────────── LLMs ────────────────────────────
# Main conversational model (language-mirroring)
chat_llm = ChatOpenAI(
    api_key=SEA_LION_API_KEY,
    base_url=SEA_LION_API_BASE,
    model=SEA_LION_MODEL,
    temperature=0.2,
)

# Deterministic intent classifier (tiny budget; same provider)
intent_llm = ChatOpenAI(
    api_key=SEA_LION_API_KEY,
    base_url=SEA_LION_API_BASE,
    model=SEA_LION_MODEL,
    temperature=0.0,
    max_tokens=16,
)

# ───────────────────── chat chain (with history) ───────────────
chat_prompt = ChatPromptTemplate.from_messages(
    [
        ("system", DEFAULT_SYSTEM_PROMPT),
        MessagesPlaceholder(variable_name="history"),
        ("human", "{input}"),
    ]
)
core_chain: Runnable = chat_prompt | chat_llm

def get_session_history(session_id: str) -> MongoDBChatMessageHistory:
    return MongoDBChatMessageHistory(
        connection_string=MONGODB_URI,
        database_name=DB_NAME,
        collection_name="chat_histories",
        session_id=session_id,
    )

chat_chain = RunnableWithMessageHistory(
    core_chain,
    get_session_history,
    input_messages_key="input",
    history_messages_key="history",
)

# ─────────────────── intent classifier prompt ──────────────────
# Labels we care about. We’ll short-circuit /chat on any of these.
label_to_trigger = {
    "view_appointments":  "view appointments",
    "view_lab_results":   "view lab results",
    "view_immunisations": "view immunisations",
    "view_payments":      "view payments",
}

intent_prompt = ChatPromptTemplate.from_messages(
    [
        ("system",
         "You are an intent classifier for HealthHub.\n"
         "Return exactly ONE label and nothing else:\n"
         "view_appointments | view_lab_results | view_immunisations | view_payments | none\n\n"
         "Guidance:\n"
         "- 'appointments' includes clinic/hospital/polyclinic check up/visit/consultation.\n"
         "- 'lab results' includes lab report, blood test results.\n"
         "- 'immunisations' includes vaccination records, jabs, shots, vax.\n"
         "- 'payments' includes bills, invoices, payment status.\n"
         "- If unclear, return 'none'.\n\n"
         "Examples:\n"
         "Q: i may have a kallang polyclinic check up, help me check\nA: view_appointments\n"
         "Q: show my blood test report\nA: view_lab_results\n"
         "Q: see my vax record\nA: view_immunisations\n"
         "Q: check my bills\nA: view_payments\n"
         "Q: tell me a joke\nA: none\n"
        ),
        ("human", "{q}"),
    ]
)

def classify_intent_llm(text: str) -> str:
    try:
        msgs = intent_prompt.format_messages(q=text)
        out = intent_llm.invoke(msgs).content.strip().lower()
        allowed = set(label_to_trigger.keys()) | {"none"}
        return out if out in allowed else "none"
    except Exception:
        return "none"

# ──────────────── light normalization for intent ───────────────
_ABBREV = [
    (re.compile(r"\b(appt|appts)\b", re.I), "appointments"),
    (re.compile(r"\bvax\b", re.I), "vaccination"),
    (re.compile(r"\bimms?\b", re.I), "immunisations"),
]
def normalize_for_intent(s: str) -> str:
    t = s or ""
    for pat, rep in _ABBREV:
        t = pat.sub(rep, t)
    return t

# Regex fallback for appointment-like phrasing (e.g., polyclinic check up)
APPT_FALLBACK = re.compile(
    r"\b(polyclinic|clinic|check[- ]?up|doctor visit|consultation|see (a )?doctor)\b",
    re.I,
)

# ─────────────────────────── health ────────────────────────────
@app.get("/health")
def health() -> dict:
    return {"ok": True}

# ─────────────────────────── /chat ─────────────────────────────
# If LLM (or fallback) detects a HealthHub-record intent, SHORT-CIRCUIT:
#   reply with: type or say "<canonical trigger>"
# Otherwise, return normal chat response.
@app.post("/chat", response_model=SimpleChatResponse)
def chat_simple(req: SimpleChatRequest) -> SimpleChatResponse:
    try:
        user_raw = req.prompt or ""
        to_classify = normalize_for_intent(user_raw)

        label = classify_intent_llm(to_classify)
        if label == "none" and APPT_FALLBACK.search(user_raw):
            label = "view_appointments"

        # Short-circuit path: force exact instruction that triggers agent/run
        if label in label_to_trigger:
            phrase = label_to_trigger[label]   # e.g. "view appointments"
            return SimpleChatResponse(
                thread_id=req.thread_id,
                reply=f'type or say "{phrase}"'
            )

        # Normal chat path (language mirroring + history)
        cfg = {"configurable": {"session_id": req.thread_id}}
        ai_msg = chat_chain.invoke({"input": user_raw}, cfg)
        reply_text = getattr(ai_msg, "content", "") or ""
        if not reply_text:
            raise ValueError("Empty response from LLM")

        return SimpleChatResponse(thread_id=req.thread_id, reply=reply_text)

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
