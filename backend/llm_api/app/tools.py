import json, re
from typing import Optional, List
from pydantic import BaseModel, Field
from langchain_core.tools import StructuredTool

def build_tools(page: dict) -> List[StructuredTool]:
    class FindInput(BaseModel):
        query: str = Field(..., description="Text to search in labels/link text/selectors")

    class ClickInput(BaseModel):
        selector: str

    class TypeInput(BaseModel):
        selector: str
        text: str

    class DoneInput(BaseModel):
        reason: str

    def _canon(s: str) -> str:
        return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()

    def _overlap(a: str, b: str) -> int:
        A = set(_canon(a).split())
        B = set(_canon(b).split())
        return len(A & B)

    def _search(q: str):
        ql = _canon(q)
        out = []

        def add(kind, item):
            out.append({
                "kind": kind,
                "text": item.get("text") or item.get("name") or "",
                "selector": item.get("selector", ""),
            })

        def matches(text: str, selector: str) -> bool:
            if not ql: return False
            tl = _canon(text)
            sl = _canon(selector)
            if ql in tl or ql in sl:
                return True
            # token overlap heuristic
            return _overlap(ql, tl) > 0

        for b in page.get("buttons", []):
            if matches(b.get("text",""), b.get("selector","")):
                add("button", b)
        for a in page.get("links", []):
            if matches(a.get("text",""), a.get("selector","")):
                add("link", a)
        for i in page.get("inputs", []):
            hay = " ".join([i.get("name",""), i.get("placeholder",""), i.get("selector","")]).lower()
            if ql and ql in hay:
                add("input", i)
        
        # light scoring: prefer more overlap, then shorter selectors
        def score(item):
            ov = _overlap(ql, item["text"])
            return (ov, -len(item.get("selector","")))
        out.sort(key=score, reverse=True)
        return out

    def find_func(query: str) -> str:
        matches = _search(query)
        return json.dumps({"matches": matches[:3], "total": len(matches)})

    def click_func(selector: str) -> str:
        all_sel = {
            *(x.get("selector","") for x in page.get("buttons", [])),
            *(x.get("selector","") for x in page.get("links", [])),
        }
        return json.dumps({"ok": (selector in all_sel) or not all_sel, "selector": selector, "note": "proxy-click"})

    def type_func(selector: str, text: str) -> str:
        all_sel = {x.get("selector","") for x in page.get("inputs", [])}
        return json.dumps({"ok": (selector in all_sel) or not all_sel, "selector": selector, "typed": text, "note": "proxy-type"})

    def done_func(reason: str) -> str:
        return json.dumps({"done": True, "reason": reason})

    return [
        StructuredTool.from_function(find_func,  name="find",  description="List candidate page elements/selectors", args_schema=FindInput),
        StructuredTool.from_function(click_func, name="click", description="Click an element by selector (proxy)",   args_schema=ClickInput),
        StructuredTool.from_function(type_func,  name="type",  description="Type into an input by selector (proxy)",args_schema=TypeInput),
        StructuredTool.from_function(done_func,  name="done",  description="Call when the goal is achieved",         args_schema=DoneInput),
    ]
