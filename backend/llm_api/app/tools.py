# app/tools.py
from __future__ import annotations

import json
import re
import time
import threading
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field
from langchain_core.tools import StructuredTool


# ────────────────────────── Bridge: latest page snapshot ─────────────────────
# The Chrome extension/background should POST its fresh snapshot to a FastAPI
# endpoint which calls set_latest_snapshot(). Graph tools can then read it.

_BRIDGE_LOCK = threading.Lock()
_LATEST_SNAPSHOT: Optional[Dict[str, Any]] = None


def set_latest_snapshot(snap: Dict[str, Any]) -> None:
    """Server endpoint calls this to publish the newest browser snapshot."""
    global _LATEST_SNAPSHOT
    with _BRIDGE_LOCK:
        _LATEST_SNAPSHOT = snap


def get_latest_snapshot() -> Optional[Dict[str, Any]]:
    with _BRIDGE_LOCK:
        # return a shallow copy to avoid accidental mutation
        return _LATEST_SNAPSHOT.copy() if _LATEST_SNAPSHOT else None


# ────────────────────────── Tool factory ─────────────────────────────────────
def build_tools(page: Dict[str, Any]) -> List[StructuredTool]:
    """
    Return the toolset used by graphs/subgraphs.

    Includes:
      - find, click, type (proxy; read-only over initial `page`)
      - wait (supports {seconds} or {ms})
      - wait_for_idle (server-side timed pause)
      - get_page_state (reads latest snapshot from bridge; fallback to `page`)
      - done
    """

    # ── Pydantic schemas (input models) ──────────────────────────────────────
    class FindInput(BaseModel):
        query: str = Field(..., description="Text to search in labels/link text/selectors")

    class ClickInput(BaseModel):
        selector: str

    class TypeInput(BaseModel):
        selector: str
        text: str

    class WaitInput(BaseModel):
        # Support either seconds or ms; both optional, at least one should be provided
        seconds: Optional[int] = Field(default=None, ge=0, le=60, description="How long to wait (seconds)")
        ms: Optional[int] = Field(default=None, ge=0, le=60000, description="How long to wait (milliseconds)")

    class WaitIdleInput(BaseModel):
        quietMs: Optional[int] = Field(default=600, ge=0, le=60000)
        timeout: Optional[int] = Field(default=3000, ge=0, le=180000)

    class DoneInput(BaseModel):
        reason: str

    class EmptyInput(BaseModel):
        """Schema for tools with no args (e.g., get_page_state)."""
        pass

    # ── Helpers for proxy find() over the *provided* `page` snapshot ─────────
    def _canon(s: str) -> str:
        return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()

    def _overlap(a: str, b: str) -> int:
        A = set(_canon(a).split())
        B = set(_canon(b).split())
        return len(A & B)

    def _search(q: str) -> List[Dict[str, Any]]:
        ql = _canon(q)
        out: List[Dict[str, Any]] = []

        def add(kind: str, item: Dict[str, Any]) -> None:
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
            return _overlap(ql, tl) > 0

        for b in page.get("buttons", []) or []:
            if matches(b.get("text", ""), b.get("selector", "")):
                add("button", b)
        for a in page.get("links", []) or []:
            if matches(a.get("text", ""), a.get("selector", "")):
                add("link", a)
        for i in page.get("inputs", []) or []:
            hay = " ".join([i.get("name", ""), i.get("placeholder", ""), i.get("selector", "")]).lower()
            if ql and ql in hay:
                add("input", i)

        def score(item: Dict[str, Any]):
            ov = _overlap(ql, item.get("text", ""))
            return (ov, -len(item.get("selector", "") or ""))

        out.sort(key=score, reverse=True)
        return out

    # ── Tool impls (return JSON strings; graphs parse ToolMessage.content) ───
    def find_func(query: str) -> str:
        matches = _search(query)
        return json.dumps({"matches": matches[:6], "total": len(matches)})

    def click_func(selector: str) -> str:
        links = page.get("links", []) or []
        nav = None
        for a in links:
            if a.get("selector", "") == selector and a.get("href"):
                nav = a.get("href")
                break
        return json.dumps({
            "ok": True,
            "selector": selector,
            "navigate_to": nav,  # client can decide to navigate
            "note": "proxy-click"
        })

    def type_func(selector: str, text: str) -> str:
        all_sel = {x.get("selector", "") for x in (page.get("inputs", []) or [])}
        return json.dumps({
            "ok": (selector in all_sel) or not all_sel,
            "selector": selector,
            "typed": text,
            "note": "proxy-type"
        })

    def wait_func(seconds: Optional[int] = None, ms: Optional[int] = None) -> str:
        wait_ms = 0
        if isinstance(seconds, int):
            wait_ms = max(wait_ms, min(seconds, 60) * 1000)
        if isinstance(ms, int):
            wait_ms = max(wait_ms, min(ms, 60_000))
        if wait_ms > 0:
            time.sleep(wait_ms / 1000.0)
        return json.dumps({"ok": True, "waited": wait_ms})

    def wait_for_idle_func(quietMs: Optional[int] = 600, timeout: Optional[int] = 3000) -> str:
        # Server cannot detect real browser idleness; we simulate a small pause.
        slept = max(0, min(int(quietMs or 0), int(timeout or 0), 60_000))
        if slept:
            time.sleep(slept / 1000.0)
        return json.dumps({"idle": True, "slept_ms": slept})

    def get_page_state_func() -> str:
        """
        Return the latest extension-provided snapshot if available; otherwise
        fallback to the initial `page` this toolset was built with.
        """
        snap = get_latest_snapshot() or page or {}
        try:
            print("[get_page_state]",
                "bridge" if get_latest_snapshot() else "fallback",
                (snap.get("url") or "")[:120])
        except Exception:
            pass
        return json.dumps(snap)

    def done_func(reason: str) -> str:
        return json.dumps({"done": True, "reason": reason})

    # ── Export tools ─────────────────────────────────────────────────────────
    return [
        # Main-agent proxies
        StructuredTool.from_function(
            find_func,
            name="find",
            description="List candidate page elements/selectors from the current (cached) snapshot",
            args_schema=FindInput,
        ),
        StructuredTool.from_function(
            click_func,
            name="click",
            description="Proxy click by selector; returns navigate_to if the selector is an <a>",
            args_schema=ClickInput,
        ),
        StructuredTool.from_function(
            type_func,
            name="type",
            description="Proxy type into an input by selector",
            args_schema=TypeInput,
        ),
        StructuredTool.from_function(
            wait_func,
            name="wait",
            description="Pause execution; accepts either {seconds} or {ms}",
            args_schema=WaitInput,
        ),
        StructuredTool.from_function(
            wait_for_idle_func,
            name="wait_for_idle",
            description="Server-side idle simulation; sleeps briefly to let SPA settle",
            args_schema=WaitIdleInput,
        ),

        # Subgraph-required tools
        StructuredTool.from_function(
            get_page_state_func,
            name="get_page_state",
            description="Return the freshest DOM snapshot pushed by the extension; fallback to initial page",
            args_schema=EmptyInput,
        ),
        StructuredTool.from_function(
            done_func,
            name="done",
            description="Call when the goal is achieved / terminal message with a reason string",
            args_schema=DoneInput,
        ),
    ]
