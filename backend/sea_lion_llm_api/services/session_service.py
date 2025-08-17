from sea_lion_llm_api.config import settings
from sea_lion_llm_api.data.repositories import session_repo, memory_repo

def create_session(user_id: str) -> str:
    sid = session_repo.create(user_id, settings.SESSION_TTL_HOURS)
    # seed empty memory for session
    memory_repo.upsert(session_id=sid, user_id=user_id, summary="", facts={})
    return sid

def close_session(session_id: str):
    session_repo.close(session_id)
