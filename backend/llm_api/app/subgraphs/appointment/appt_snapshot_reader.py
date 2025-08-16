# app/subgraphs/appt_snapshot_reader.py
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
TARGET_HOST = os.getenv("APPT_TARGET_HOST", "eservices.healthhub.sg")
APPT_URL_TOKEN = os.getenv("APPT_SNAPSHOT_URL_TOKEN", "/appointments")

# Gate until URL shows the token
APPT_GATE_MAX_TRIES   = int(os.getenv("APPT_GATE_MAX_TRIES", "12"))   # total polls for URL token
APPT_GATE_POLL_MS     = int(os.getenv("APPT_GATE_POLL_MS", "250"))    # interval between polls
APPT_GATE_INITIAL_MS  = int(os.getenv("APPT_GATE_INITIAL_MS", "300")) # one-time grace before first poll

# After URL token is seen, optionally do a few extra settle polls
APPT_SETTLE_TRIES     = int(os.getenv("APPT_SETTLE_TRIES", "2"))
IDLE_HINT = {
    "quietMs": int(os.getenv("APPT_IDLE_QUIET_MS", "700")),
    "timeout": int(os.getenv("APPT_IDLE_TIMEOUT_MS", "8000")),
}

# Optional structure checks (can be zeroed via env)
MIN_TEXTS    = int(os.getenv("APPT_MIN_TEXTS", "20"))
MIN_MATCHED  = int(os.getenv("APPT_MIN_MATCHED", "1"))

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
def _is_appt_url(url: Optional[str]) -> bool:
    if not isinstance(url, str):
        return False
    u = url.lower()
    return (TARGET_HOST in u) and (APPT_URL_TOKEN.lower() in u)

# ───────────────────────── parsing helpers ─────────────────────────
_WS = re.compile(r"\s+")
_MONTHS = {
    "jan","feb","mar","apr","may","jun","jul","aug","sep","sept","oct","nov","dec",
    "january","february","march","april","june","july","august","september","october","november","december"
}
_DOW = {"mon","tue","tues","wed","thu","thur","thurs","fri","sat","sun","monday","tuesday","wednesday","thursday","friday","saturday","sunday"}
_TIME_RX = re.compile(r"\b([01]?\d|2[0-3]):[0-5]\d\s*(AM|PM)?\b", re.I)
_DAY_RX  = re.compile(r"^\s*(\d{1,2})\s*$")
_YEAR_RX = re.compile(r"^\s*(20\d{2})\s*$")

def _norm(s: str) -> str:
    return _WS.sub(" ", (s or "").strip())

def _lower(s: str) -> str:
    return _norm(s).lower()

def _looks_card_header(lines: List[str], i: int) -> Optional[Dict[str,str]]:
    """
    Heuristic for the card you showed:
      [i]   = '27'                   (day)
      [i+1] = 'Aug'                  (month)
      [i+2] = '2025'                 (year)
      [i+3] = 'Geylang Polyclinic ' (clinic)
      [i+4] = 'Wed, 09:10 AM'       (dow + time)
      [i+5] = 'Dental Cleaning ...' (procedure)
      [i+6] = 'GEYD LEVEL ...'      (location)
    We allow some drift and missing pieces.
    """
    N = len(lines)
    if i + 2 >= N:
        return None

    day_ok = _DAY_RX.match(lines[i] or "")
    mon_ok = _lower(lines[i+1]) in _MONTHS
    yr_ok  = _YEAR_RX.match(lines[i+2] or "")

    if not (day_ok and mon_ok and yr_ok):
        return None

    info = {
        "date": f"{day_ok.group(1)} {lines[i+1]} {yr_ok.group(1)}",
        "clinic": "",
        "time": "",
        "procedure": "",
        "location": "",
        "provider": "",
    }

    # try to read subsequent fields, up to a small window
    j = i + 3
    end = min(N, i + 12)
    while j < end:
        t = _norm(lines[j]); tl = _lower(t)
        if not info["clinic"] and t and len(t) > 2:
            # often ends with 'Polyclinic' or has proper nouns
            info["clinic"] = t
            j += 1
            continue

        # time often 'Wed, 09:10 AM' or '09:10 AM'
        if not info["time"]:
            m = _TIME_RX.search(t)
            if m:
                info["time"] = m.group(0).upper()
                j += 1
                continue
            # tolerate "Wed, 09:10 AM" — if contains comma and a time, strip DOW
            if "," in t:
                m2 = _TIME_RX.search(t)
                if m2:
                    info["time"] = m2.group(0).upper()
                    j += 1
                    continue

        # procedure: a descriptive title (avoid room codes)
        if not info["procedure"] and t and not t.isupper() and len(t) >= 6 and "room" not in tl:
            info["procedure"] = t
            j += 1
            continue

        # location: often uppercase block with 'LEVEL', 'ROOM'
        if not info["location"] and ("level" in tl or "room" in tl or tl.isupper()):
            info["location"] = t
            j += 1
            continue

        j += 1

    return info

def _extract_provider_from_images(state: Dict[str,Any]) -> Optional[str]:
    """
    If your snapshot includes images with alt='provider' (like NHGP logo),
    try to pull a short provider code/name.
    """
    imgs = state.get("images") or []  # depends on your content.js snapshot; safe if missing
    for im in imgs:
        alt = _lower(str(im.get("alt","")))
        if "provider" in alt:
            src = str(im.get("src","")).rsplit("/", 1)[-1]
            name = (im.get("alt") or "").strip() or src
            return name
    return None

def _extract_appts_from_page_state(state: Dict[str, Any]) -> List[Dict[str, str]]:
    texts_raw = state.get("texts") or []
    # Your snapshot usually stores [{"text": "..."}] — normalize
    lines: List[str] = [_norm(x.get("text","")) for x in texts_raw if isinstance(x, dict)]
    lines = [x for x in lines if x]  # drop empties

    items: List[Dict[str,str]] = []
    N = len(lines)
    i = 0
    while i < N:
        card = _looks_card_header(lines, i)
        if card:
            # attach provider if available globally (logos are often out of the text stream)
            prov = _extract_provider_from_images(state)
            if prov:
                card["provider"] = prov
            items.append(card)
            # skip a chunk to avoid double-counting inside the same card
            i += 7
            continue
        i += 1

    # If nothing matched, try a looser pass: detect any line that has both a date-ish month and a time
    if not items:
        for k in range(N-1):
            t = lines[k]; tl = _lower(t)
            if any(m in tl for m in _MONTHS) and _TIME_RX.search(" ".join(lines[k:k+3]) ):
                # best-effort fallback
                item = {
                    "date": t,
                    "time": _TIME_RX.search(" ".join(lines[k:k+3])).group(0).upper() if _TIME_RX.search(" ".join(lines[k:k+3])) else "",
                    "clinic": "",
                    "procedure": "",
                    "location": "",
                    "provider": _extract_provider_from_images(state) or "",
                }
                # guess clinic/procedure near by
                if k+1 < N and not item["clinic"]:
                    item["clinic"] = lines[k+1]
                if k+2 < N and not item["procedure"]:
                    item["procedure"] = lines[k+2]
                items.append(item)

    # dedupe
    seen: set[Tuple[str,str,str,str,str]] = set()
    uniq: List[Dict[str,str]] = []
    for it in items:
        key = (it.get("date",""), it.get("time",""), it.get("clinic",""), it.get("procedure",""), it.get("location",""))
        if key not in seen:
            seen.add(key); uniq.append(it)
    return uniq

def _looks_structured(snap: Dict[str, Any]) -> bool:
    # very light structure check; ensure we have enough text lines and at least some month/time hints
    texts = snap.get("texts") or []
    if len(texts) < MIN_TEXTS:
        return False
    joined = " ".join([str(t.get("text","")) for t in texts if isinstance(t, dict)]).lower()
    months_present = any(m in joined for m in _MONTHS)
    time_present = bool(_TIME_RX.search(joined))
    return months_present and time_present

def _summarize(items: List[Dict[str, str]]) -> str:
    if not items: return "No appointments found."
    return " | ".join([
        f"{it.get('date') or 'Unknown Date'} @ {it.get('time') or '?'} — "
        f"{it.get('clinic') or 'Unknown Clinic'} — "
        f"{it.get('procedure') or 'Unknown Procedure'} — "
        f"{it.get('location') or 'Unknown Location'}"
        for it in items
    ])

# ------------- graph -------------
class ApptReadState(TypedDict):
    messages: Annotated[List[AnyMessage], add_messages]
    prep_tries: int
    settle_tries: int
    initial_wait_done: bool

def build_appointments_snapshot_reader_subgraph(page: Dict[str, Any], tools: Optional[List] = None):
    if tools is None:
        from ..tools import build_tools  # type: ignore
        tools = build_tools(page)

    def node(state: ApptReadState) -> ApptReadState:
        state.setdefault("prep_tries", 0)
        state.setdefault("settle_tries", 0)
        state.setdefault("initial_wait_done", False)

        # If we don't have a get_page_state payload yet, request one.
        if not (state.get("messages") and isinstance(state["messages"][-1], ToolMessage)
                and getattr(state["messages"][-1], "name", "") == "get_page_state"):
            # First time in → an initial grace before first poll (once)
            if not state["initial_wait_done"] and APPT_GATE_INITIAL_MS > 0:
                state["initial_wait_done"] = True
                return {**state, "messages": [
                    _ai_tool_call("wait_for_idle", {"quietMs": min(APPT_GATE_INITIAL_MS, 1000), "timeout": APPT_GATE_INITIAL_MS + 2000}),
                    _ai_tool_call("wait", {"ms": APPT_GATE_INITIAL_MS}),
                    _ai_tool_call("get_page_state", {}),
                ]}
            # Otherwise, just get a snapshot
            return {**state, "messages": [_ai_tool_call("get_page_state", {})]}

        # We have a snapshot – decide whether to gate or extract.
        last = state["messages"][-1]
        snap = _last_payload(last) or {}
        url = (snap or {}).get("url", "")
        texts = snap.get("texts") or []

        # Stage 1: URL token gate (only proceed once the Appointments URL is visible)
        if not _is_appt_url(url):
            tries = state["prep_tries"] + 1
            state["prep_tries"] = tries
            log.info("[appt_read] waiting for appointments URL (url=%s) try=%d/%d", url, tries, APPT_GATE_MAX_TRIES)
            if tries <= APPT_GATE_MAX_TRIES:
                return {**state, "messages": [
                    _ai_tool_call("wait_for_idle", {"quietMs": 200, "timeout": 2000}),
                    _ai_tool_call("wait", {"ms": APPT_GATE_POLL_MS}),
                    _ai_tool_call("get_page_state", {}),
                ]}
            # Timeout: proceed anyway with whatever we have
            log.info("[appt_read] appointments URL gate timed out; continuing with current snapshot")

        # Stage 2 (optional): give the DOM a moment to settle after URL switch
        if APPT_SETTLE_TRIES > 0 and not _looks_structured(snap):
            settles = state["settle_tries"] + 1
            state["settle_tries"] = settles
            log.info("[appt_read] settling DOM (url=%s, texts=%d) settle=%d/%d",
                     url, len(texts), settles, APPT_SETTLE_TRIES)
            if settles <= APPT_SETTLE_TRIES:
                return {**state, "messages": [
                    _ai_tool_call("wait_for_idle", IDLE_HINT),
                    _ai_tool_call("get_page_state", {}),
                ]}

        # Extract & finish
        items = _extract_appts_from_page_state(snap)
        summary = _summarize(items)
        payload = {
            "url": url,
            "count": len(items),
            "summary": summary,
            "items": items,
            "reason": f"Extracted {len(items)} appointment(s)",
            "gated": (state.get("prep_tries", 0) > 0) or (state.get("settle_tries", 0) > 0),
            "prep_tries": state.get("prep_tries", 0),
            "settle_tries": state.get("settle_tries", 0),
        }
        pretty = (json.dumps(payload, ensure_ascii=False)[:MAX_LOG_CHARS]
                  if isinstance(payload, dict) else str(payload))
        log.info("[appt_read] extracted: %s", pretty)
        try:
            print("[appt_read] extracted:", pretty)
        except Exception:
            pass
        return {**state, "messages": [_ai_tool_call("done", payload)]}

    g = StateGraph(ApptReadState)
    g.add_node("appt_read", node)
    g.add_node("tools", ToolNode(tools))
    g.add_edge(START, "appt_read")
    g.add_edge("appt_read", "tools")

    def _route(state: ApptReadState):
        if state.get("messages") and isinstance(state["messages"][-1], ToolMessage):
            if getattr(state["messages"][-1], "name", "") == "done":
                return END
        return "appt_read"

    g.add_conditional_edges("tools", _route, {"appt_read": "appt_read", END: END})
    return g.compile()
