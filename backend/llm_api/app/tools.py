# app/tools.py
import json, re
from typing import Optional, List
from pydantic import BaseModel, Field
from langchain_core.tools import StructuredTool


def build_tools(page: dict) -> List[StructuredTool]:
    # ── Schemas ───────────────────────────────────────────────────────────────
    class FindInput(BaseModel):
        query: str = Field(..., description="Text to search in labels/link text/selectors")

    class ClickInput(BaseModel):
        selector: str

    class TypeInput(BaseModel):
        selector: str
        text: str

    class WaitInput(BaseModel):
        seconds: int = Field(..., ge=0, le=60, description="How long to wait (seconds)")

    class DoneInput(BaseModel):
        reason: str

    # ── Helpers ───────────────────────────────────────────────────────────────
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
                "href": item.get("href") or "",
            })

        def matches(text: str, selector: str) -> bool:
            if not ql:
                return False
            tl = _canon(text)
            sl = _canon(selector)
            if ql in tl or ql in sl:
                return True
            # token overlap heuristic
            return _overlap(ql, tl) > 0

        for b in page.get("buttons", []):
            if matches(b.get("text", ""), b.get("selector", "")):
                add("button", b)
        for a in page.get("links", []):
            if matches(a.get("text", ""), a.get("selector", "")):
                add("link", a)
        for i in page.get("inputs", []):
            hay = " ".join([i.get("name", ""), i.get("placeholder", ""), i.get("selector", "")]).lower()
            if ql and ql in hay:
                add("input", i)

        # light scoring: prefer more overlap, then shorter selectors
        def score(item):
            ov = _overlap(ql, item["text"])
            return (ov, -len(item.get("selector", "")))
        out.sort(key=score, reverse=True)
        return out

    # ── Tool impls ────────────────────────────────────────────────────────────
    def find_func(query: str) -> str:
        matches = _search(query)
        # Up to 6 so the graph can enumerate clear choices
        return json.dumps({"matches": matches[:6], "total": len(matches)})

    def click_func(selector: str) -> str:
        # Collect known selectors (optional sanity)
        buttons = page.get("buttons", [])
        links   = page.get("links", [])
        all_sel = {*(x.get("selector", "") for x in buttons), *(x.get("selector", "") for x in links)}

        # If it's a link, surface its href in case the client wants to navigate
        nav = None
        for a in links:
            if a.get("selector", "") == selector and a.get("href"):
                nav = a.get("href")
                break

        # Always ok=True for proxy click; navigation (if any) is indicated separately
        return json.dumps({
            "ok": True,
            "selector": selector,
            "navigate_to": nav,   # harmless; graph may ignore it
            "note": "proxy-click"
        })

    def type_func(selector: str, text: str) -> str:
        all_sel = {x.get("selector", "") for x in page.get("inputs", [])}
        return json.dumps({
            "ok": (selector in all_sel) or not all_sel,
            "selector": selector,
            "typed": text,
            "note": "proxy-type"
        })

    def wait_func(seconds: int) -> str:
        # Client/browser should perform the real wait; this is an acknowledgment
        return json.dumps({"ok": True, "waited": int(seconds)})

    def done_func(reason: str) -> str:
        return json.dumps({"done": True, "reason": reason})

    # ── Export ONLY the minimal set ───────────────────────────────────────────
    return [
        StructuredTool.from_function(find_func,  name="find",  description="List candidate page elements/selectors", args_schema=FindInput),
        StructuredTool.from_function(click_func, name="click", description="Click an element by selector (proxy)",   args_schema=ClickInput),
        StructuredTool.from_function(type_func,  name="type",  description="Type into an input by selector (proxy)",args_schema=TypeInput),
        StructuredTool.from_function(wait_func,  name="wait",  description="Wait for N seconds (proxy)",             args_schema=WaitInput),
        StructuredTool.from_function(done_func,  name="done",  description="Call when the goal is achieved",         args_schema=DoneInput),
    ]
