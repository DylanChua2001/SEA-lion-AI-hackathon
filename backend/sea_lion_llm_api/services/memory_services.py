import json
from sea_lion_llm_api.config import settings
from sea_lion_llm_api.data.repositories import memory_repo, message_repo
from typing import List, Dict
import re

def build_context_block(session_id: str) -> str:
    summary, facts = memory_repo.get_for_session(session_id)
    return (
        f"Context summary:\n{summary}\n\n"
        f"Known facts (JSON): {json.dumps(facts, ensure_ascii=False)}\n"
        "If facts conflict with the user, politely ask to confirm."
    )

def _simple_summarize(messages: List[Dict[str, str]]) -> str:
    """Very lightweight summarizer: keeps last user intent and assistant action."""
    last_user = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")
    last_assistant = next((m["content"] for m in reversed(messages) if m["role"] == "assistant"), "")
    # Trim to avoid excessive growth
    last_user = re.sub(r"\s+", " ", last_user).strip()[:300]
    last_assistant = re.sub(r"\s+", " ", last_assistant).strip()[:300]
    return f"Last user intent: {last_user}\nAssistant reply: {last_assistant}"

def on_new_turn(session_id: str):
    # Summarize the last few turns and upsert into memory.
    msgs = recent_messages(session_id)
    summary = _simple_summarize(msgs)
    # Facts extraction could be added here; keep as empty dict for now
    memory_repo.upsert(session_id=session_id, user_id=session_id, summary=summary, facts={})

def recent_messages(session_id: str):
    return message_repo.recent(session_id, settings.CONTEXT_LAST_N)
