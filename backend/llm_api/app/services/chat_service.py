from typing import Dict, Any, List
from langchain.schema import HumanMessage, SystemMessage, AIMessage
from app.clients.sea_lion import SeaLionClient

def map_msgs(msgs: List[Dict[str,str]]):
    out=[]
    for m in msgs:
        r=m["role"].lower(); c=m["content"]
        out.append(SystemMessage(c) if r=="system" else AIMessage(c) if r in ("assistant","ai") else HumanMessage(c))
    return out

class ChatService:
    def __init__(self, llm: SeaLionClient): self.llm = llm
    def run(self, *, messages, thinking_mode=None, **opts):
        kwargs={}
        if thinking_mode in ("on","off"): kwargs["thinking_mode"]=thinking_mode
        return self.llm.chat(map_msgs(messages), **kwargs)
