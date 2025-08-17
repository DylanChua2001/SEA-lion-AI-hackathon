import datetime
from bson import ObjectId
from sea_lion_llm_api.data.mongo import db

_mem = db.memories
_mem.create_index([("sessionId", 1)])
_mem.create_index([("userId", 1)])

def get_for_session(session_id: str):
    doc = _mem.find_one({"sessionId": ObjectId(session_id)}) or {}
    return doc.get("summary", ""), doc.get("facts", {})

def upsert(session_id: str, user_id: str, summary: str, facts: dict):
    _mem.update_one(
        {"sessionId": ObjectId(session_id)},
        {"$set": {
            "userId": ObjectId(user_id),
            "summary": summary,
            "facts": facts,
            "updatedAt": datetime.datetime.utcnow()
        }},
        upsert=True
    )
