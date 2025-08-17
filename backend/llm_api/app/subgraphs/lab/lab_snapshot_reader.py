# app/subgraphs/lab_snapshot_reader.py
from __future__ import annotations

import json
import logging
import os
import re
from typing import Annotated, Dict, Any, List, Optional, TypedDict, Tuple

from langgraph.graph import StateGraph, START, END
from langgraph.prebuilt import ToolNode
from langgraph.graph.message import add_messages
from langchain_core.messages import AnyMessage, AIMessage, ToolMessage

log = logging.getLogger(__name__)
log.propagate = True
if not log.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("%(levelname)s %(process)d %(name)s: %(message)s"))
    log.addHandler(_h)
log.setLevel(logging.INFO)

MAX_LOG_CHARS = 4000

# ───────────────────────── tunables (env-overridable) ─────────────────────────
TARGET_HOST = os.getenv("LAB_TARGET_HOST", "eservices.healthhub.sg")
LAB_URL_TOKEN = os.getenv("LAB_SNAPSHOT_URL_TOKEN", "/lab-test-reports/lab")

# Gate until URL shows the token
LAB_GATE_MAX_TRIES   = int(os.getenv("LAB_GATE_MAX_TRIES", "12"))   # total polls for URL token
LAB_GATE_POLL_MS     = int(os.getenv("LAB_GATE_POLL_MS", "250"))    # interval between polls
LAB_GATE_INITIAL_MS  = int(os.getenv("LAB_GATE_INITIAL_MS", "300")) # one-time grace before first poll

# After URL token is seen, optionally do a few extra settle polls
LAB_SETTLE_TRIES     = int(os.getenv("LAB_SETTLE_TRIES", "2"))
IDLE_HINT = {
    "quietMs": int(os.getenv("LAB_IDLE_QUIET_MS", "700")),
    "timeout": int(os.getenv("LAB_IDLE_TIMEOUT_MS", "8000")),
}

# Optional structure checks (can be zeroed via env)
MIN_HEADINGS = int(os.getenv("LAB_MIN_HEADINGS", "1"))
MIN_LINKS    = int(os.getenv("LAB_MIN_LINKS", "5"))

# ───────────────────────── tool-call shims ─────────────────────────
def _ai_tool_call(name: str, args: dict) -> AIMessage:
    return AIMessage(content="", tool_calls=[{
        "id": f"call_{name}", "type": "tool_call", "name": name, "args": args or {}
    }])

def _last_payload(msg: ToolMessage) -> Dict[str, Any]:
    try:
        payload = json.loads(msg.content or "{}")
    except Exception:
        payload = {}
    return payload.get("data", payload) or {}

# ───────────────────────── readiness checks ─────────────────────────
def _is_lab_url(url: Optional[str]) -> bool:
    if not isinstance(url, str):
        return False
    u = url.lower()
    return (TARGET_HOST in u) and (LAB_URL_TOKEN in u)

def _looks_structured(snap: Dict[str, Any]) -> bool:
    links = snap.get("links") or {}
    headings = snap.get("headings") or {}
    return (len(headings) >= MIN_HEADINGS) and (len(links) >= MIN_LINKS)

# ------------- extraction helpers -------------
_LABELS = {"date:", "ordering facility:", "performing facility:"}
_IRRELEVANT_EXACT = {
    "log out","healthier sg","health a-z","live healthy","mental wellbeing","parent hub",
    "health programmes","health services","filters","reset filters","switch","about",
    "about healthhub faq privacy policy terms of use contact us sitemap","top","/","health e-services",
    "health e-services /","lab reports","note",
}
_DATE_RX = re.compile(r"\b(\d{1,2}\s+[A-Za-z]{3}\s+\d{4})\b")
_WS = re.compile(r"\s+")

def _norm(s: str) -> str:
    return _WS.sub(" ", (s or "").strip())

def _is_label(s: str) -> bool:
    return _norm(s).lower() in _LABELS or any(_norm(s).lower().startswith(lbl) for lbl in _LABELS)

def _is_irrelevant(s: str) -> bool:
    t = _norm(s).lower()
    return (not t) or (t in _IRRELEVANT_EXACT) or (len(t) < 3)

def _grab_after_colon(s: str) -> str:
    return _norm(s.split(":", 1)[1]) if ":" in s else ""

def _pick_prev_title(texts: List[str], i: int) -> str:
    j = i - 1
    while j >= 0:
        cand = _norm(texts[j])
        if cand and (not _is_label(cand)) and (not _is_irrelevant(cand)) and (":" not in cand):
            return cand
        j -= 1
    return ""

def _extract_items_from_page_state(state: Dict[str, Any]) -> List[Dict[str, str]]:
    texts_raw = state.get("texts") or []
    texts = [_norm(x.get("text", "")) for x in texts_raw if isinstance(x, dict)]
    items: List[Dict[str, str]] = []
    i = 0
    N = len(texts)
    while i < N:
        t = texts[i]; tl = t.lower()
        has_date_label = tl.startswith("date:"); found_date: Optional[str] = None
        if has_date_label:
            same_line = _grab_after_colon(t)
            if same_line: found_date = same_line
            elif i + 1 < N:
                nxt = _norm(texts[i + 1]); m = _DATE_RX.search(nxt); found_date = m.group(1) if m else (nxt or None)
        else:
            if "date" in tl and ":" in t:
                m = _DATE_RX.search(t);  found_date = m.group(1) if m else None
        if found_date:
            item = {"test_name":"", "date":found_date, "ordering_facility":"", "performing_facility":""}
            item["test_name"] = _pick_prev_title(texts, i)
            j = i; end = min(N, i + 12)
            while j < end:
                line = texts[j]; ll = line.lower()
                if ll.startswith("ordering facility:"):
                    v = _grab_after_colon(line) or (_norm(texts[j+1]) if j+1 < N else "")
                    item["ordering_facility"] = v or item["ordering_facility"]
                elif ll.startswith("performing facility:"):
                    v = _grab_after_colon(line) or (_norm(texts[j+1]) if j+1 < N else "")
                    item["performing_facility"] = v or item["performing_facility"]
                j += 1
            if any([item["test_name"], item["ordering_facility"], item["performing_facility"]]):
                items.append(item)
            i = j
            continue
        i += 1
    seen: set[Tuple[str,str,str,str]] = set(); uniq: List[Dict[str,str]] = []
    for it in items:
        key = (it.get("test_name",""), it.get("date",""), it.get("ordering_facility",""), it.get("performing_facility",""))
        if key not in seen: seen.add(key); uniq.append(it)
    return uniq

def _summarize(items: List[Dict[str, str]]) -> str:
    if not items: return "No lab items found."
    return " | ".join([
        f"{it.get('test_name') or 'Unknown Test'} — {it.get('date') or 'Unknown Date'} — "
        f"{it.get('ordering_facility') or 'Unknown Ordering Facility'} — "
        f"{it.get('performing_facility') or 'Unknown Performing Facility'}"
        for it in items
    ])

def _tts_list(items: List[Dict[str, str]], limit: int = 3) -> str:
    if not items:
        return "No lab items were found."
    parts = []
    for it in items[:limit]:
        name = it.get("test_name") or "Unknown test"
        date = it.get("date") or "unknown date"
        ord_fac = it.get("ordering_facility") or ""
        segs = [f"{name} on {date}"]
        if ord_fac:
            segs.append(f"from {ord_fac}")
        parts.append(", ".join(segs))
    more = len(items) - limit
    if more > 0:
        parts.append(f"and {more} more")
    return "; ".join(parts) + "."

# ------------- graph -------------
class LabReadState(TypedDict):
    messages: Annotated[List[AnyMessage], add_messages]
    prep_tries: int          # polls spent waiting for URL token
    settle_tries: int        # extra polls after URL token to let DOM settle
    initial_wait_done: bool  # applied LAB_GATE_INITIAL_MS once

def build_lab_snapshot_reader_subgraph(page: Dict[str, Any], tools: Optional[List] = None):
    if tools is None:
        from ..tools import build_tools  # type: ignore
        tools = build_tools(page)

    def node(state: LabReadState) -> LabReadState:
        state.setdefault("prep_tries", 0)
        state.setdefault("settle_tries", 0)
        state.setdefault("initial_wait_done", False)

        # If we don't have a get_page_state payload yet, request one.
        if not (state.get("messages") and isinstance(state["messages"][-1], ToolMessage)
                and getattr(state["messages"][-1], "name", "") == "get_page_state"):
            # First time in → an initial grace before first poll (once)
            if not state["initial_wait_done"] and LAB_GATE_INITIAL_MS > 0:
                state["initial_wait_done"] = True
                return {**state, "messages": [
                    _ai_tool_call("wait_for_idle", {"quietMs": min(LAB_GATE_INITIAL_MS, 1000), "timeout": LAB_GATE_INITIAL_MS + 2000}),
                    _ai_tool_call("wait", {"ms": LAB_GATE_INITIAL_MS}),
                    _ai_tool_call("get_page_state", {}),
                ]}
            # Otherwise, just get a snapshot
            return {**state, "messages": [_ai_tool_call("get_page_state", {})]}

        # We have a snapshot – decide whether to gate or extract.
        last = state["messages"][-1]
        snap = _last_payload(last) or {}
        url = (snap or {}).get("url", "")
        links = snap.get("links") or []
        headings = snap.get("headings") or []

        # Stage 1: URL token gate (only proceed once the lab URL is visible)
        if not _is_lab_url(url):
            tries = state["prep_tries"] + 1
            state["prep_tries"] = tries
            log.info("[lab_read] waiting for lab URL (url=%s) try=%d/%d", url, tries, LAB_GATE_MAX_TRIES)
            if tries <= LAB_GATE_MAX_TRIES:
                return {**state, "messages": [
                    _ai_tool_call("wait_for_idle", {"quietMs": 200, "timeout": 2000}),
                    _ai_tool_call("wait", {"ms": LAB_GATE_POLL_MS}),
                    _ai_tool_call("get_page_state", {}),
                ]}
            # Timeout: proceed anyway with whatever we have
            log.info("[lab_read] lab URL gate timed out; continuing with current snapshot")

        # Stage 2 (optional): give the DOM a moment to settle after URL switch
        if LAB_SETTLE_TRIES > 0 and not _looks_structured(snap):
            settles = state["settle_tries"] + 1
            state["settle_tries"] = settles
            log.info("[lab_read] settling DOM (url=%s, links=%d, headings=%d) settle=%d/%d",
                     url, len(links), len(headings), settles, LAB_SETTLE_TRIES)
            if settles <= LAB_SETTLE_TRIES:
                return {**state, "messages": [
                    _ai_tool_call("wait_for_idle", IDLE_HINT),
                    _ai_tool_call("get_page_state", {}),
                ]}

        # Extract & finish
        items = _extract_items_from_page_state(snap)
        summary = _summarize(items)
        payload = {
            "url": url,
            "count": len(items),
            "summary": summary,          # UI-friendly
            "tts": _tts_list(items),     # NEW: TTS-friendly
            "items": items,
            "reason": f"Extracted {len(items)} lab item(s)",
            "gated": (state.get("prep_tries", 0) > 0) or (state.get("settle_tries", 0) > 0),
            "prep_tries": state.get("prep_tries", 0),
            "settle_tries": state.get("settle_tries", 0),
        }
        pretty = (json.dumps(payload, ensure_ascii=False)[:MAX_LOG_CHARS]
                  if isinstance(payload, dict) else str(payload))
        log.info("[lab_read] extracted: %s", pretty)
        try:
            print("[lab_read] extracted:", pretty)
        except Exception:
            pass
        return {**state, "messages": [_ai_tool_call("done", payload)]}

    g = StateGraph(LabReadState)
    g.add_node("lab_read", node)
    g.add_node("tools", ToolNode(tools))
    g.add_edge(START, "lab_read")
    g.add_edge("lab_read", "tools")

    def _route(state: LabReadState):
        if state.get("messages") and isinstance(state["messages"][-1], ToolMessage):
            if getattr(state["messages"][-1], "name", "") == "done":
                return END
        return "lab_read"

    g.add_conditional_edges("tools", _route, {"lab_read": "lab_read", END: END})
    return g.compile()
