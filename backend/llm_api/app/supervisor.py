# app/supervisor.py
from __future__ import annotations
import json
import logging
import re
import os
import time
from typing import Annotated, Dict, Any, List, Optional, TypedDict

from langgraph.graph import StateGraph, START, END
from langgraph.graph.message import add_messages
from langchain_core.messages import AnyMessage, SystemMessage, HumanMessage

from .tools import build_tools
from .llm import make_llm
from .subgraphs.lab.lab_records import build_lab_records_subgraph
from .subgraphs.appointments import build_appointments_subgraph
from .subgraphs.payments import build_payments_subgraph
from .subgraphs.immunisations import build_immunisations_subgraph
from .subgraphs.lab.lab_snapshot_reader import build_lab_snapshot_reader_subgraph

# ───────────────────────── logging setup ─────────────────────────
log = logging.getLogger("app.supervisor")
log.propagate = True
if not log.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("%(levelname)s %(process)d %(name)s: %(message)s"))
    log.addHandler(_h)
log.setLevel(logging.INFO)

# ───────────────────────── config ─────────────────────────
LAB_SNAPSHOT_DELAY_MS = int(os.getenv("LAB_SNAPSHOT_DELAY_MS", "0"))  # optional pause, mostly for single-shot
LAB_TWO_STEP = os.getenv("LAB_TWO_STEP", "1").strip() not in {"0", "false", "False"}  # default: two-step ON

TARGET_HOST = "eservices.healthhub.sg"
LAB_URL_TOKEN = "/lab-test-reports/lab"

def _is_lab_url(url: Optional[str]) -> bool:
    if not isinstance(url, str):
        return False
    u = url.lower()
    return (TARGET_HOST in u) and (LAB_URL_TOKEN in u)

# ───────────────────────── state ─────────────────────────
class SupervisorState(TypedDict):
    messages: Annotated[List[AnyMessage], add_messages]
    goal: str
    page: Dict[str, Any]
    route: Optional[str]           # 'lab_results' | 'appointments' | 'payments' | 'immunisations' | 'lab_read'

# ───────────────────────── utils ─────────────────────────
_GOAL_RE = re.compile(r"^\s*GOAL:\s*(.*)$", re.I)
_PAGE_RE = re.compile(r"^\s*PAGE_STATE:\s*(\{.*\})\s*$", re.I)

def _extract_goal_and_page_from_messages(msgs: List[AnyMessage]) -> tuple[str, Dict[str, Any]]:
    goal = ""
    page: Dict[str, Any] = {}
    for m in msgs or []:
        if isinstance(m, HumanMessage):
            t = (m.content or "").strip()
            if not goal:
                g = _GOAL_RE.match(t)
                if g:
                    goal = g.group(1).strip()
            if not page:
                p = _PAGE_RE.match(t)
                if p:
                    try:
                        page = json.loads(p.group(1))
                    except Exception:
                        page = {}
    return goal, page

def _normalize_route(text: str) -> Optional[str]:
    t = (text or "").strip().lower()
    t = t.replace('"', '').replace("'", "")
    try:
        if t.startswith("{"):
            obj = json.loads(t)
            t = (obj.get("route") or obj.get("choice") or obj.get("workflow") or "").strip().lower()
    except Exception:
        pass
    if t in {"lab_results", "appointments", "payments", "immunisations"}:
        return t
    if re.search(r"\blab", t):
        return "lab_results"
    if re.search(r"\bappoint", t):
        return "appointments"
    if re.search(r"\bpay|bill|invoice", t):
        return "payments"
    if re.search(r"\bimmuni|vaccin|booster|jab|shot", t):
        return "immunisations"
    return None

# ───────────────────────── builder ─────────────────────────
def build_supervisor_app(page: dict):
    log.info("[supervisor] building shared tools")
    tools = build_tools(page)

    log.info("[supervisor] compiling subgraphs…")
    lab_g   = build_lab_records_subgraph(page, tools)          # navigation to lab
    lab_read_g = build_lab_snapshot_reader_subgraph(page, tools)  # extract from lab page
    appt_g  = build_appointments_subgraph(page, tools)
    pay_g   = build_payments_subgraph(page, tools)
    imm_g   = build_immunisations_subgraph(page, tools)
    log.info("[supervisor] subgraphs compiled")

    llm = make_llm(temperature=0)

    DECISION_INSTRUCTIONS = (
        "You are a router. Read the user's goal and choose exactly one workflow.\n"
        "Valid outputs:\n"
        "  - lab_results\n"
        "  - appointments\n"
        "  - payments\n"
        "  - immunisations\n\n"
        "Rules:\n"
        "  • Reply with ONLY one of the four tokens above. No explanations.\n"
        "  • If the goal involves viewing lab results, lab reports, blood tests, pathology → lab_results.\n"
        "  • If it involves booking, rescheduling, or checking appointment slots → appointments.\n"
        "  • If it involves paying bills, invoices, fees, or making a payment → payments.\n"
        "  • If it involves vaccines, immunisations, boosters, jabs, shots → immunisations.\n"
        "  • If ambiguous, pick the most likely.\n"
    )

    def decide(state: SupervisorState) -> SupervisorState:
        goal = (state.get("goal") or "").strip()
        page = state.get("page") or {}
        if not goal:
            g2, p2 = _extract_goal_and_page_from_messages(state.get("messages", []))
            if g2:
                goal = g2
            if p2:
                page = p2

        goal_text = goal or ""
        url = (page.get("url") or "")

        log.info("[supervisor] deciding route for goal=%r url=%s", goal_text, url)

        # Ask LLM for initial route
        sys = SystemMessage(content=DECISION_INSTRUCTIONS)
        usr = HumanMessage(content=f"Goal: {goal_text}\nURL: {url}")
        resp = llm.invoke([sys, usr])
        raw = (getattr(resp, "content", None) or "").strip()
        route = _normalize_route(raw)
        if not route:
            log.warning("[supervisor] LLM returned %r; falling back to appointments", raw)
            route = "appointments"

        # Two-step override: if we’re already at the lab page, skip navigation and read directly
        if route == "lab_results" and _is_lab_url(url):
            log.info("[supervisor] already on lab page; overriding route to lab_read")
            route = "lab_read"

        log.info("[supervisor] route=%s (raw=%r)", route, raw)
        return {**state, "route": route, "goal": goal_text, "page": page}

    def router(state: SupervisorState):
        r = state.get("route")
        if r == "lab_results":
            return "lab"
        if r == "lab_read":
            return "lab_read"
        if r == "appointments":
            return "appointments"
        if r == "payments":
            return "payments"
        if r == "immunisations":
            return "immunisations"
        return "appointments"  # safety

    # Optional tiny pause node (kept for single-shot mode only)
    def pause_before_lab_read(state: SupervisorState) -> SupervisorState:
        delay_ms = LAB_SNAPSHOT_DELAY_MS
        if delay_ms > 0:
            log.info("[supervisor] pausing %d ms before lab_read to allow snapshot to persist", delay_ms)
            time.sleep(delay_ms / 1000.0)
        return state

    g = StateGraph(SupervisorState)
    g.add_node("decide", decide)

    # Wrap compiled subgraphs
    g.add_node("lab",           lambda s: lab_g.invoke(s))
    g.add_node("lab_read",      lambda s: lab_read_g.invoke(s))
    g.add_node("appointments",  lambda s: appt_g.invoke(s))
    g.add_node("payments",      lambda s: pay_g.invoke(s))
    g.add_node("immunisations", lambda s: imm_g.invoke(s))
    g.add_node("pause_before_lab_read", pause_before_lab_read)

    g.add_edge(START, "decide")
    g.add_edge(START, "decide")

    # Route selection
    g.add_conditional_edges("decide", router, {
        "lab": "lab",
        "lab_read": "lab_read",
        "appointments": "appointments",
        "payments": "payments",
        "immunisations": "immunisations",
    })

    if LAB_TWO_STEP:
        # Two-step mode: stop after navigation; caller triggers a second /agent/run once snapshot is fresh.
        g.add_edge("lab", END)             # end after click/navigation
        g.add_edge("lab_read", END)        # extract and end
    else:
        # Single-shot legacy: chain lab → (optional pause) → lab_read
        if LAB_SNAPSHOT_DELAY_MS > 0:
            g.add_edge("lab", "pause_before_lab_read")
            g.add_edge("pause_before_lab_read", "lab_read")
        else:
            g.add_edge("lab", "lab_read")
        g.add_edge("lab_read", END)

    # Others
    g.add_edge("appointments", END)
    g.add_edge("payments", END)
    g.add_edge("immunisations", END)

    log.info("[supervisor] compiled (two_step=%s)", LAB_TWO_STEP)
    return g.compile()