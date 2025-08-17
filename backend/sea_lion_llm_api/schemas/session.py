from pydantic import BaseModel

class NewSessionIn(BaseModel):
    userId: str

class NewSessionOut(BaseModel):
    sessionId: str

class CloseSessionIn(BaseModel):
    sessionId: str

class CloseSessionOut(BaseModel):
    ok: bool
