import datetime
from bson import ObjectId
from pymongo import ASCENDING, DESCENDING
from sea_lion_llm_api.data.mongo import db

_sessions = db.sessions
_sessions.create_index([("userId", ASCENDING), ("lastActiveAt", DESCENDING)])
_sessions.create_index("expiresAt", expireAfterSeconds=0)

def create(user_id: str, ttl_hours: int) -> str:
    now = datetime.datetime.utcnow()
    doc = {
        "userId": ObjectId(user_id),
        "status": "active",
        "createdAt": now,
        "lastActiveAt": now,
        "expiresAt": now + datetime.timedelta(hours=ttl_hours),
    }
    _id = _sessions.insert_one(doc).inserted_id
    return str(_id)

def touch(session_id: str):
    _sessions.update_one(
        {"_id": ObjectId(session_id)},
        {"$set": {"lastActiveAt": datetime.datetime.utcnow()}}
    )

def close(session_id: str):
    _sessions.update_one({"_id": ObjectId(session_id)}, {"$set": {"status": "closed"}})
