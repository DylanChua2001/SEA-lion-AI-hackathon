from pydantic import BaseModel

class ChatIn(BaseModel):
    sessionId: str
    text: str

class ChatOut(BaseModel):
    reply: str
