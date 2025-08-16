# app/subgraphs/payment/pay_snapshot_reader.py
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
TARGET_HOST   = os.getenv("PAY_TARGET_HOST", "eservices.healthhub.sg")
PAY_URL_TOKEN = os.getenv("PAY_SNAPSHOT_URL_TOKEN", "/payments")

# Gate until URL shows the token
PAY_GATE_MAX_TRIES   = int(os.getenv("PAY_GATE_MAX_TRIES", "12"))    # total polls for URL token
PAY_GATE_POLL_MS     = int(os.getenv("PAY_GATE_POLL_MS", "250"))     # interval between polls
PAY_GATE_INITIAL_MS  = int(os.getenv("PAY_GATE_INITIAL_MS", "300"))  # one-time grace before first poll

# After URL token is seen, optionally do a few extra settle polls
PAY_SETTLE_TRIES     = int(os.getenv("PAY_SETTLE_TRIES", "2"))
IDLE_HINT = {
    "quietMs": int(os.getenv("PAY_IDLE_QUIET_MS", "700")),
    "timeout": int(os.getenv("PAY_IDLE_TIMEOUT_MS", "8000")),
}

# Optional structure checks (can be zeroed via env)
MIN_HEADINGS = int(os.getenv("PAY_MIN_HEADINGS", "1"))
MIN_LINKS    = int(os.getenv("PAY_MIN_LINKS", "2"))

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
def _is_pay_url(url: Optional[str]) -> bool:
    if not isinstance(url, str):
        return False
    u = url.lower()
    return (TARGET_HOST in u) and (PAY_URL_TOKEN in u)

def _looks_structured(snap: Dict[str, Any]) -> bool:
    links = snap.get("links") or []
    headings = snap.get("headings") or []
    return (len(headings) >= MIN_HEADINGS) and (len(links) >= MIN_LINKS)

# ───────────────────────── extraction helpers ─────────────────────────
_WS = re.compile(r"\s+")
# Accepts both "S$37.30" and potential encoded "S\$37.30" from text sources
_MONEY_RX = re.compile(r"S\\$\s*\d{1,3}(?:,\d{3})*(?:\.\d{2})?|\bS\$\s*\d+(?:\.\d{2})?\b", re.I)

def _norm(s: str) -> str:
    return _WS.sub(" ", (s or "").strip())

def _grab_amount(s: str) -> Optional[str]:
    m = _MONEY_RX.search(s.replace("\\", ""))
    return m.group(0) if m else None

def _extract_note(texts: List[str]) -> str:
    """
    Pull the maintenance note block if present.
    Heuristics: look for a line that starts with 'Note' (case-insensitive)
    and capture a few subsequent lines until a hard boundary (heading change or blank).
    """
    out: List[str] = []
    i = 0
    N = len(texts)
    while i < N:
        t = texts[i].strip()
        if t.lower().startswith("note"):
            out.append(_norm(t))
            j = i + 1
            while j < min(N, i + 9):
                line = texts[j].strip()
                if not line:
                    break
                # stop if this looks like a new main heading
                if len(line) < 60 and line.istitle():
                    break
                out.append(_norm(line))
                j += 1
            break
        i += 1
    # De-duplicate short pieces and join
    uniq = []
    seen = set()
    for p in out:
        if p not in seen:
            uniq.append(p)
            seen.add(p)
    return " ".join(uniq).strip()

def _extract_clusters_from_texts(texts: List[str]) -> List[Dict[str, str]]:
    """
    Extract cluster cards under 'Outstanding Bills by Cluster'
    Each card typically presents:
      - Cluster name (e.g., National Healthcare Group)
      - 'Amount to pay:' label
      - Amount (e.g., S$37.30 or S$0.00)
    Approach: find the section header first, then scan forward to pick triples.
    """
    items: List[Dict[str, str]] = []
    if not texts:
        return items

    # Find the section anchor
    section_idxs = [i for i, t in enumerate(texts) if _norm(t).lower() == "outstanding bills by cluster"]
    if not section_idxs:
        # No anchor; fallback: try to parse globally (still safe)
        return _scan_cards_globally(texts)

    start = section_idxs[0] + 1
    i = start
    N = len(texts)
    while i < N:
        t = _norm(texts[i])
        tl = t.lower()

        # Stop if we hit another major section (e.g., footer headings)
        if tl in {"about", "in partnership with", "select cluster"}:
            break

        # Heuristic: cluster name lines are reasonably long, not labels, and followed soon by "Amount to pay:"
        if t and not tl.startswith("amount to pay") and not tl.endswith(":") and len(t) >= 6:
            # Look ahead a few lines for "Amount to pay:" and an amount
            label_idx = -1
            amount_val: Optional[str] = None
            for j in range(i + 1, min(i + 8, N)):
                tj = _norm(texts[j]); tjl = tj.lower()
                if tjl.startswith("amount to pay"):
                    label_idx = j
                    # next couple of lines may hold the S$ amount; or sometimes same line
                    amount_val = _grab_amount(tj)
                    if not amount_val:
                        for k in range(j + 1, min(j + 4, N)):
                            cand = _norm(texts[k])
                            amount_val = _grab_amount(cand)
                            if amount_val:
                                break
                    break

            if label_idx != -1:
                cluster = t
                items.append({"cluster": cluster, "amount": amount_val or ""})
                # move pointer near end of this card
                i = max(i + 1, label_idx + 1)
                continue

        i += 1

    # Deduplicate
    uniq: List[Dict[str, str]] = []
    seen: set[Tuple[str, str]] = set()
    for it in items:
        key = (it.get("cluster", ""), it.get("amount", ""))
        if key not in seen:
            uniq.append(it)
            seen.add(key)
    return uniq

def _scan_cards_globally(texts: List[str]) -> List[Dict[str, str]]:
    """
    Fallback parser when the explicit section header isn't found.
    Looks for repeating pattern of:
        <Cluster Name>
        ...
        Amount to pay:
        S$X.YY
    """
    items: List[Dict[str, str]] = []
    N = len(texts)
    i = 0
    while i < N:
        t = _norm(texts[i])
        tl = t.lower()
        if t and not tl.startswith("amount to pay") and len(t) >= 6:
            # Search ahead for the amount label and value
            label_idx = -1
            amount_val: Optional[str] = None
            for j in range(i + 1, min(i + 8, N)):
                tj = _norm(texts[j]); tjl = tj.lower()
                if tjl.startswith("amount to pay"):
                    label_idx = j
                    amount_val = _grab_amount(tj)
                    if not amount_val:
                        for k in range(j + 1, min(j + 4, N)):
                            cand = _norm(texts[k])
                            amount_val = _grab_amount(cand)
                            if amount_val:
                                break
                    break
            if label_idx != -1:
                items.append({"cluster": t, "amount": amount_val or ""})
                i = max(i + 1, label_idx + 1)
                continue
        i += 1

    # Deduplicate
    uniq: List[Dict[str, str]] = []
    seen: set[Tuple[str, str]] = set()
    for it in items:
        key = (it.get("cluster", ""), it.get("amount", ""))
        if key not in seen:
            uniq.append(it)
            seen.add(key)
    return uniq

def _extract_from_snapshot(snap: Dict[str, Any]) -> Dict[str, Any]:
    texts_raw = snap.get("texts") or []
    # Texts come as [{"text": "..."}]
    texts = [_norm(x.get("text", "")) for x in texts_raw if isinstance(x, dict)]

    # 1) Extract maintenance note (if any)
    note = _extract_note(texts)

    # 2) Extract clusters with amounts
    clusters = _extract_clusters_from_texts(texts)

    # 3) Summaries
    summary = " | ".join([f"{c.get('cluster','?')} — {c.get('amount','')}" for c in clusters]) or "No outstanding bills detected."

    return {
        "note": note,
        "clusters": clusters,
        "summary": summary,
        "count": len(clusters),
    }

# ───────────────────────── TTS helpers ─────────────────────────
def _clean_amount(a: str) -> str:
    # normalize "S\$" → "S$", collapse spaces
    return _norm((a or "").replace("\\", ""))

def _tts_from_clusters(clusters: List[Dict[str, str]], note: str, limit: int = 3) -> str:
    """
    Produce a short, speech-friendly sentence:
      - 'No outstanding bills.'
      - 'Outstanding bills: NHG, S$37.30; SingHealth, S$0.00; and 1 more. Note: ...'
    """
    if not clusters:
        base = "No outstanding bills."
        if note:
            # keep the note short to avoid long reads
            trimmed = (note[:180] + "…") if len(note) > 180 else note
            return f"{base} {trimmed}"
        return base

    parts: List[str] = []
    for c in clusters[:limit]:
        name = (c.get("cluster") or "Unknown cluster").strip()
        amt = _clean_amount(c.get("amount") or "")
        if amt:
            parts.append(f"{name}, {amt}")
        else:
            parts.append(f"{name}")

    more = len(clusters) - limit
    prefix = "Outstanding bills: " if any((c.get('amount') or "").strip() not in {"", "S$0.00", "S$0", "S$0.0"} for c in clusters) else "Bills by cluster: "
    spoken = "; ".join(parts)
    if more > 0:
        spoken += f"; and {more} more"

    # Optionally append a short note if present
    if note:
        trimmed = (note[:160] + "…") if len(note) > 160 else note
        return f"{prefix}{spoken}. {trimmed}"
    return f"{prefix}{spoken}."

# ───────────────────────── graph ─────────────────────────
class PayReadState(TypedDict):
    messages: Annotated[List[AnyMessage], add_messages]
    prep_tries: int          # polls spent waiting for URL token
    settle_tries: int        # extra polls after token to let DOM settle
    initial_wait_done: bool  # applied PAY_GATE_INITIAL_MS once

def build_payments_snapshot_reader_subgraph(page: Dict[str, Any], tools: Optional[List] = None):
    if tools is None:
        from ...tools import build_tools  # type: ignore
        tools = build_tools(page)

    def node(state: PayReadState) -> PayReadState:
        state.setdefault("prep_tries", 0)
        state.setdefault("settle_tries", 0)
        state.setdefault("initial_wait_done", False)

        # If we don't have a get_page_state payload yet, request one.
        if not (state.get("messages") and isinstance(state["messages"][-1], ToolMessage)
                and getattr(state["messages"][-1], "name", "") == "get_page_state"):
            # First time in → an initial grace before first poll (once)
            if not state["initial_wait_done"] and PAY_GATE_INITIAL_MS > 0:
                state["initial_wait_done"] = True
                return {**state, "messages": [
                    _ai_tool_call("wait_for_idle", {"quietMs": min(PAY_GATE_INITIAL_MS, 1000), "timeout": PAY_GATE_INITIAL_MS + 2000}),
                    _ai_tool_call("wait", {"ms": PAY_GATE_INITIAL_MS}),
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

        # Stage 1: URL token gate (only proceed once the payments URL is visible)
        if not _is_pay_url(url):
            tries = state["prep_tries"] + 1
            state["prep_tries"] = tries
            log.info("[pay_read] waiting for payments URL (url=%s) try=%d/%d", url, tries, PAY_GATE_MAX_TRIES)
            if tries <= PAY_GATE_MAX_TRIES:
                return {**state, "messages": [
                    _ai_tool_call("wait_for_idle", {"quietMs": 200, "timeout": 2000}),
                    _ai_tool_call("wait", {"ms": PAY_GATE_POLL_MS}),
                    _ai_tool_call("get_page_state", {}),
                ]}
            # Timeout: proceed anyway with whatever we have
            log.info("[pay_read] payments URL gate timed out; continuing with current snapshot")

        # Stage 2 (optional): give the DOM a moment to settle after URL switch
        if PAY_SETTLE_TRIES > 0 and not _looks_structured(snap):
            settles = state["settle_tries"] + 1
            state["settle_tries"] = settles
            log.info("[pay_read] settling DOM (url=%s, links=%d, headings=%d) settle=%d/%d",
                     url, len(links), len(headings), settles, PAY_SETTLE_TRIES)
            if settles <= PAY_SETTLE_TRIES:
                return {**state, "messages": [
                    _ai_tool_call("wait_for_idle", IDLE_HINT),
                    _ai_tool_call("get_page_state", {}),
                ]}

        # Extract & finish
        extracted = _extract_from_snapshot(snap)
        tts = _tts_from_clusters(extracted.get("clusters", []), extracted.get("note", ""))
        payload = {
            "url": url,
            **extracted,
            "tts": tts,  # NEW: speech-friendly one-liner(s)
            "reason": f"Extracted {extracted.get('count', 0)} cluster bill item(s)",
            "gated": (state.get("prep_tries", 0) > 0) or (state.get("settle_tries", 0) > 0),
            "prep_tries": state.get("prep_tries", 0),
            "settle_tries": state.get("settle_tries", 0),
        }
        pretty = (json.dumps(payload, ensure_ascii=False)[:MAX_LOG_CHARS]
                  if isinstance(payload, dict) else str(payload))
        log.info("[pay_read] extracted: %s", pretty)
        try:
            print("[pay_read] extracted:", pretty)
        except Exception:
            pass
        return {**state, "messages": [_ai_tool_call("done", payload)]}

    g = StateGraph(PayReadState)
    g.add_node("pay_read", node)
    g.add_node("tools", ToolNode(tools))
    g.add_edge(START, "pay_read")
    g.add_edge("pay_read", "tools")

    def _route(state: PayReadState):
        if state.get("messages") and isinstance(state["messages"][-1], ToolMessage):
            if getattr(state["messages"][-1], "name", "") == "done":
                return END
        return "pay_read"

    g.add_conditional_edges("tools", _route, {"pay_read": "pay_read", END: END})
    return g.compile()
