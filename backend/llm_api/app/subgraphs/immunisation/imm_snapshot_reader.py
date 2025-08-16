# app/subgraphs/immunisation/imm_snapshot_reader.py
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
TARGET_HOST = os.getenv("IMM_TARGET_HOST", "eservices.healthhub.sg")
IMM_URL_TOKEN = os.getenv("IMM_SNAPSHOT_URL_TOKEN", "/immunisation")

# Gate until URL shows the token
IMM_GATE_MAX_TRIES   = int(os.getenv("IMM_GATE_MAX_TRIES", "12"))
IMM_GATE_POLL_MS     = int(os.getenv("IMM_GATE_POLL_MS", "250"))
IMM_GATE_INITIAL_MS  = int(os.getenv("IMM_GATE_INITIAL_MS", "300"))

# After URL token is seen, optionally do a few extra settle polls
IMM_SETTLE_TRIES     = int(os.getenv("IMM_SETTLE_TRIES", "2"))
IDLE_HINT = {
    "quietMs": int(os.getenv("IMM_IDLE_QUIET_MS", "700")),
    "timeout": int(os.getenv("IMM_IDLE_TIMEOUT_MS", "8000")),
}

# Optional structure checks (can be zeroed via env)
MIN_TEXTS     = int(os.getenv("IMM_MIN_TEXTS", "15"))
REQUIRE_MONTH = os.getenv("IMM_REQUIRE_MONTH", "1").strip() not in {"0","false","False"}

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
def _is_imm_url(url: Optional[str]) -> bool:
    if not isinstance(url, str):
        return False
    u = url.lower()
    return (TARGET_HOST in u) and (IMM_URL_TOKEN.lower() in u)

# ───────────────────────── parsing helpers ─────────────────────────
_WS = re.compile(r"\s+")
_MONTHS = {
    "jan","feb","mar","apr","may","jun","jul","aug","sep","sept","oct","nov","dec",
    "january","february","march","april","june","july","august","september","october","november","december"
}
_DATE_RXES = [
    re.compile(r"\b(\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4})\b"),
    re.compile(r"\b(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})\b"),
    re.compile(r"\b(\d{4}-\d{2}-\d{2})\b"),
]
_DOSE_HINTS = ("dose", "booster", "primary", "1st", "2nd", "3rd", "4th", "first", "second", "third")
_STATUS_HINTS = ("completed", "done", "administered", "overdue", "due", "pending", "scheduled", "declined")
_FACILITY_HINTS = ("clinic", "hospital", "polyclinic", "centre", "center", "facility", "site", "location")
_BATCH_HINTS = ("batch", "lot", "batch no", "lot no", "batch number", "lot number")
_VACCINE_HINTS = (
    "mmr", "measles", "mumps", "rubella", "bcg", "hepatitis", "hep b", "hib", "pneumo", "pcv",
    "varicella", "hpv", "diphtheria", "tetanus", "pertussis", "dtap", "tdap", "polio", "ipv", "opv",
    "covid", "pfizer", "moderna", "sinovac", "influenza", "flu", "yellow fever", "meningococcal",
    "var", "mmrv", "rotavirus", "zoster", "shingles", "td", "dt"
)

def _norm(s: str) -> str:
    return _WS.sub(" ", (s or "").strip())

def _lower(s: str) -> str:
    return _norm(s).lower()

def _find_first_date(text: str) -> Optional[str]:
    for rx in _DATE_RXES:
        m = rx.search(text)
        if m:
            return m.group(1)
    return None

def _looks_structured(snap: Dict[str, Any]) -> bool:
    texts = snap.get("texts") or []
    if len(texts) < MIN_TEXTS:
        return False
    if not REQUIRE_MONTH:
        return True
    joined = " ".join([str(t.get("text","")) for t in texts if isinstance(t, dict)]).lower()
    return any(m in joined for m in _MONTHS)

def _is_signal_line(tl: str) -> bool:
    return (
        any(k in tl for k in _VACCINE_HINTS)
        or any(k in tl for k in _DOSE_HINTS)
        or any(k in tl for k in _STATUS_HINTS)
        or any(k in tl for k in _FACILITY_HINTS)
        or any(k in tl for k in _BATCH_HINTS)
        or _find_first_date(tl) is not None
    )

def _window(lines: List[str], i: int, span: int = 8) -> List[str]:
    a = max(0, i - 1)
    b = min(len(lines), i + span)
    return lines[a:b]

def _extract_imm_from_window(win: List[str]) -> Dict[str, str]:
    vaccine = ""
    dose = ""
    date = ""
    status = ""
    facility = ""
    batch = ""

    for raw in win:
        t = _norm(raw)
        tl = _lower(t)

        if not date:
            d = _find_first_date(t)
            if d:
                date = d

        if not dose:
            if "booster" in tl:
                dose = "Booster"
            elif "1st" in tl or "first" in tl:
                dose = "1st dose"
            elif "2nd" in tl or "second" in tl:
                dose = "2nd dose"
            elif "3rd" in tl or "third" in tl:
                dose = "3rd dose"
            elif "4th" in tl or "fourth" in tl:
                dose = "4th dose"
            elif "dose" in tl:
                dose = t

        if not status:
            for k in _STATUS_HINTS:
                if k in tl:
                    if "completed" in tl or "administered" in tl or "done" in tl:
                        status = "Completed"
                    elif "overdue" in tl:
                        status = "Overdue"
                    elif "pending" in tl or "scheduled" in tl or "due" in tl:
                        status = "Due/Pending"
                    else:
                        status = t
                    break

        if not facility:
            for k in _FACILITY_HINTS:
                if k in tl:
                    facility = t
                    break

        if not batch:
            for k in _BATCH_HINTS:
                if k in tl:
                    batch = t
                    break

        if not vaccine:
            if any(h in tl for h in _VACCINE_HINTS):
                vaccine = t
            elif t and t[0].isupper() and len(t) > 3 and ":" not in t and not tl.endswith(("am","pm")):
                vaccine = t

    return {
        "vaccine": vaccine,
        "dose": dose,
        "date": date,
        "status": status,
        "facility": facility,
        "batch": batch,
    }

def _extract_immunisations_from_page_state(state: Dict[str, Any]) -> List[Dict[str, str]]:
    texts_raw = state.get("texts") or []
    lines: List[str] = [_norm(x.get("text","")) for x in texts_raw if isinstance(x, dict)]
    lines = [x for x in lines if x]

    items: List[Dict[str, str]] = []
    N = len(lines)
    i = 0
    status_section: Optional[str] = None

    while i < N:
        tl = _lower(lines[i])

        # Section headers update status_section
        if "completed immunisations" in tl:
            status_section = "Completed"
            i += 1
            continue
        if "nationally recommended" in tl:
            status_section = "Recommended"
            i += 1
            continue

        if _is_signal_line(tl):
            win = _window(lines, i, span=8)
            item = _extract_imm_from_window(win)

            # Inherit section status if not explicitly set
            if not item.get("status") and status_section:
                item["status"] = status_section

            if item.get("vaccine") or item.get("date"):
                items.append(item)
                i += 5
                continue

        i += 1

    # dedupe on (vaccine, dose, date)
    seen: set[Tuple[str,str,str]] = set()
    uniq: List[Dict[str,str]] = []
    for it in items:
        key = (it.get("vaccine",""), it.get("dose",""), it.get("date",""))
        if key not in seen:
            seen.add(key); uniq.append(it)
    return uniq

def _summarize(items: List[Dict[str, str]]) -> str:
    if not items: return "No immunisation records found."
    return " | ".join([
        f"{it.get('vaccine') or 'Unknown Vaccine'} — {it.get('dose') or '?'} — "
        f"{it.get('date') or 'Unknown Date'} — {it.get('status') or 'Unknown Status'} — "
        f"{it.get('facility') or 'Unknown Facility'}"
        + (f" — {it.get('batch')}" if it.get('batch') else "")
        for it in items
    ])

# ------------- graph -------------
class ImmReadState(TypedDict):
    messages: Annotated[List[AnyMessage], add_messages]
    prep_tries: int
    settle_tries: int
    initial_wait_done: bool

def build_immunisations_snapshot_reader_subgraph(page: Dict[str, Any], tools: Optional[List] = None):
    if tools is None:
        from ..tools import build_tools  # type: ignore
        tools = build_tools(page)

    def node(state: ImmReadState) -> ImmReadState:
        state.setdefault("prep_tries", 0)
        state.setdefault("settle_tries", 0)
        state.setdefault("initial_wait_done", False)

        # If we don't have a get_page_state payload yet, request one.
        if not (state.get("messages") and isinstance(state["messages"][-1], ToolMessage)
                and getattr(state["messages"][-1], "name", "") == "get_page_state"):
            if not state["initial_wait_done"] and IMM_GATE_INITIAL_MS > 0:
                state["initial_wait_done"] = True
                return {**state, "messages": [
                    _ai_tool_call("wait_for_idle", {"quietMs": min(IMM_GATE_INITIAL_MS, 1000), "timeout": IMM_GATE_INITIAL_MS + 2000}),
                    _ai_tool_call("wait", {"ms": IMM_GATE_INITIAL_MS}),
                    _ai_tool_call("get_page_state", {}),
                ]}
            return {**state, "messages": [_ai_tool_call("get_page_state", {})]}

        # We have a snapshot – decide whether to gate or extract.
        last = state["messages"][-1]
        snap = _last_payload(last) or {}
        url = (snap or {}).get("url", "")
        texts = snap.get("texts") or []

        # Stage 1: URL token gate
        if not _is_imm_url(url):
            tries = state["prep_tries"] + 1
            state["prep_tries"] = tries
            log.info("[imm_read] waiting for immunisation URL (url=%s) try=%d/%d", url, tries, IMM_GATE_MAX_TRIES)
            if tries <= IMM_GATE_MAX_TRIES:
                return {**state, "messages": [
                    _ai_tool_call("wait_for_idle", {"quietMs": 200, "timeout": 2000}),
                    _ai_tool_call("wait", {"ms": IMM_GATE_POLL_MS}),
                    _ai_tool_call("get_page_state", {}),
                ]}
            log.info("[imm_read] immunisation URL gate timed out; continuing with current snapshot")

        # Stage 2: give the DOM a moment to settle after URL switch
        if IMM_SETTLE_TRIES > 0 and not _looks_structured(snap):
            settles = state["settle_tries"] + 1
            state["settle_tries"] = settles
            log.info("[imm_read] settling DOM (url=%s, texts=%d) settle=%d/%d",
                     url, len(texts), settles, IMM_SETTLE_TRIES)
            if settles <= IMM_SETTLE_TRIES:
                return {**state, "messages": [
                    _ai_tool_call("wait_for_idle", IDLE_HINT),
                    _ai_tool_call("get_page_state", {}),
                ]}

        # Extract & finish
        items = _extract_immunisations_from_page_state(snap)
        summary = _summarize(items)
        payload = {
            "url": url,
            "count": len(items),
            "summary": summary,
            "items": items,
            "reason": f"Extracted {len(items)} immunisation record(s)",
            "gated": (state.get("prep_tries", 0) > 0) or (state.get("settle_tries", 0) > 0),
            "prep_tries": state.get("prep_tries", 0),
            "settle_tries": state.get("settle_tries", 0),
        }
        pretty = (json.dumps(payload, ensure_ascii=False)[:MAX_LOG_CHARS]
                  if isinstance(payload, dict) else str(payload))
        log.info("[imm_read] extracted: %s", pretty)
        try:
            print("[imm_read] extracted:", pretty)
        except Exception:
            pass
        return {**state, "messages": [_ai_tool_call("done", payload)]}

    g = StateGraph(ImmReadState)
    g.add_node("imm_read", node)
    g.add_node("tools", ToolNode(tools))
    g.add_edge(START, "imm_read")
    g.add_edge("imm_read", "tools")

    def _route(state: ImmReadState):
        if state.get("messages") and isinstance(state["messages"][-1], ToolMessage):
            if getattr(state["messages"][-1], "name", "") == "done":
                return END
        return "imm_read"

    g.add_conditional_edges("tools", _route, {"imm_read": "imm_read", END: END})
    return g.compile()
