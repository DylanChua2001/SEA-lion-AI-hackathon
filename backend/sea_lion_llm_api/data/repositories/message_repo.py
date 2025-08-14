import datetime
from bson import ObjectId
from pymongo import DESCENDING, ASCENDING
from sea_lion_llm_api.data.mongo import db

_msgs = db.messages
_msgs.create_index([("sessionId", ASCENDING), ("createdAt", DESCENDING)])

def add(session_id: str, role: str, content: str):
    _msgs.insert_one({
        "sessionId": ObjectId(session_id),
        "role": role,
        "content": content,
        "createdAt": datetime.datetime.utcnow()
    })

def recent(session_id: str, last_n: int):
    docs = list(_msgs.find({"sessionId": ObjectId(session_id)})
                .sort("createdAt", DESCENDING).limit(last_n))
    docs.reverse()
    return [{"role": d["role"], "content": d["content"]} for d in docs]
