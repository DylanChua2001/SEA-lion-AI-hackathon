from sea_lion_llm_api.data.repositories import message_repo

def append(session_id: str, role: str, content: str):
    message_repo.add(session_id, role, content)
