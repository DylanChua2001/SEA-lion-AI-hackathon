# app/supervisor.py
from __future__ import annotations
import json, logging, re, os, time
from typing import Annotated, Dict, Any, List, Optional, TypedDict

from langgraph.graph import StateGraph, START, END
from langgraph.graph.message import add_messages
from langchain_core.messages import AnyMessage, SystemMessage, HumanMessage

from .tools import build_tools
from .llm import make_llm

# Subgraphs: navigation
from .subgraphs.lab.lab_records import build_lab_records_subgraph
from .subgraphs.appointment.appointments import build_appointments_subgraph
from .subgraphs.payment.payments import build_payments_subgraph
from .subgraphs.immunisation.immunisations import build_immunisations_subgraph

# Subgraphs: snapshot readers
from .subgraphs.lab.lab_snapshot_reader import build_lab_snapshot_reader_subgraph
from .subgraphs.appointment.appt_snapshot_reader import build_appointments_snapshot_reader_subgraph
from .subgraphs.immunisation.imm_snapshot_reader import build_immunisations_snapshot_reader_subgraph
from .subgraphs.payment.pay_snapshot_reader import build_payments_snapshot_reader_subgraph

# ───────────────────────── logging setup ─────────────────────────
log = logging.getLogger("app.supervisor")
log.propagate = True
if not log.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("%(levelname)s %(process)d %(name)s: %(message)s"))
    log.addHandler(_h)
log.setLevel(logging.INFO)

# ───────────────────────── config ─────────────────────────
LAB_SNAPSHOT_DELAY_MS = int(os.getenv("LAB_SNAPSHOT_DELAY_MS", "0"))

TARGET_HOST     = "eservices.healthhub.sg"
LAB_URL_TOKEN   = "/lab-test-reports/lab"
APPT_URL_TOKEN  = "/appointments"
IMM_URL_TOKEN   = "/immunisation"
PAY_URL_TOKEN   = "/payments"

def _is_lab_url(url: Optional[str]) -> bool:
    return isinstance(url, str) and (TARGET_HOST in url.lower()) and (LAB_URL_TOKEN in url.lower())
def _is_appt_url(url: Optional[str]) -> bool:
    return isinstance(url, str) and (TARGET_HOST in url.lower()) and (APPT_URL_TOKEN in url.lower())
def _is_imm_url(url: Optional[str]) -> bool:
    return isinstance(url, str) and (TARGET_HOST in url.lower()) and (IMM_URL_TOKEN in url.lower())
def _is_pay_url(url: Optional[str]) -> bool:
    return isinstance(url, str) and (TARGET_HOST in url.lower()) and (PAY_URL_TOKEN in url.lower())

# ───────────────────────── login heuristics ─────────────────────────
def _lower_list(x):
    return [str(i).lower() for i in x] if isinstance(x, list) else []

def _html_contains(page: Dict[str, Any], needle: str) -> bool:
    html = page.get("html") or ""
    try:
        return needle.lower() in str(html).lower()
    except Exception:
        return False

def _looks_like_singpass_redirect(page: Dict[str, Any]) -> bool:
    url = (page.get("url") or "").lower()
    if any(k in url for k in ["singpass", "login.singpass", "authorize", "oauth", "account/login"]):
        return True
    tb = _lower_list(page.get("top_buttons", []))
    tl = _lower_list(page.get("top_links", []))
    if any("singpass" in t for t in (tb + tl)):
        return True
    if _html_contains(page, "singpass"):
        return True
    return False

def _looks_logged_in(page: Dict[str, Any]) -> bool:
    if not isinstance(page, dict):
        return False
    session = page.get("session") or {}
    if isinstance(session, dict) and session.get("is_authenticated") is True:
        return True
    if _html_contains(page, 'sslIsAnonymous') and _html_contains(page, 'sslIsAnonymous = "True"'):
        return False
    if _html_contains(page, 'btn-login'):
        return False
    tb = _lower_list(page.get("top_buttons", []))
    tl = _lower_list(page.get("top_links", []))
    if any("login" in t for t in (tb + tl)):
        return False
    headings = _lower_list(page.get("top_headings", []))
    if any(h for h in (tb + tl + headings) if ("logout" in h or "my profile" in h or "welcome" in h or h.startswith("hi "))):
        return True
    url = (page.get("url") or "").lower()
    if TARGET_HOST in url and "login" not in url and not any("login" in t for t in (tb + tl)):
        return True
    return False

# ───────────────────────── state ─────────────────────────
class SupervisorState(TypedDict):
    messages: Annotated[List[AnyMessage], add_messages]
    goal: str
    page: Dict[str, Any]
    # route now supports *_read so we can jump straight to the reader on pass 2
    route: Optional[str]  # 'lab_results'|'appointments'|'payments'|'immunisations'|'lab_read'|'appt_read'|'imm_read'|'pay_read'

# ───────────────────────── utils ─────────────────────────
_GOAL_RE = re.compile(r"^\s*GOAL:\s*(.*)$", re.I)
_PAGE_RE = re.compile(r"^\s*PAGE_STATE:\s*(\{.*\})\s*$", re.I)

def _extract_goal_and_page_from_messages(msgs: List[AnyMessage]) -> tuple[str, Dict[str, Any]]:
    goal = ""
    page: Dict[str, Any] = {}
    for m in msgs or []:
        if hasattr(m, "content"):
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
    t = (text or "").strip().lower().replace('"','').replace("'","")
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
    lab_g       = build_lab_records_subgraph(page, tools)
    appt_g      = build_appointments_subgraph(page, tools)
    imm_g       = build_immunisations_subgraph(page, tools)
    pay_g       = build_payments_subgraph(page, tools)

    lab_read_g  = build_lab_snapshot_reader_subgraph(page, tools)
    appt_read_g = build_appointments_snapshot_reader_subgraph(page, tools)
    imm_read_g  = build_immunisations_snapshot_reader_subgraph(page, tools)
    pay_read_g  = build_payments_snapshot_reader_subgraph(page, tools)
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

    # ── Step 1: Decide (what to run THIS turn)
    def decide(state: SupervisorState) -> SupervisorState:
        goal = (state.get("goal") or "").strip()
        page0 = state.get("page") or {}
        if not goal or not page0:
            g2, p2 = _extract_goal_and_page_from_messages(state.get("messages", []))
            goal = goal or g2
            page0 = page0 or p2

        url = (page0.get("url") or "")
        log.info("[supervisor] deciding route for goal=%r url=%s", goal, url)

        sys = SystemMessage(content=DECISION_INSTRUCTIONS)
        usr = HumanMessage(content=f"Goal: {goal}\nURL: {url}")
        raw = (llm.invoke([sys, usr]).content or "").strip()
        route = _normalize_route(raw) or "appointments"

        # ── IMPORTANT: if we're ALREADY on a target page, jump straight to the reader on this run.
        if route == "lab_results" and _is_lab_url(url):
            route = "lab_read"
        elif route == "appointments" and _is_appt_url(url):
            route = "appt_read"
        elif route == "immunisations" and _is_imm_url(url):
            route = "imm_read"
        elif route == "payments" and _is_pay_url(url):
            route = "pay_read"

        log.info("[supervisor] route=%s (raw=%r)", route, raw)
        return {**state, "route": route, "goal": goal, "page": page0}

    def router(state: SupervisorState):
        r = state.get("route")
        if r == "lab_results": return "lab"
        if r == "appointments": return "appointments"
        if r == "immunisations": return "immunisations"
        if r == "payments": return "payments"
        if r == "lab_read": return "lab_read"
        if r == "appt_read": return "appt_read"
        if r == "imm_read": return "imm_read"
        if r == "pay_read": return "pay_read"
        return "appointments"

    # ── Step 2: Post-navigation login gate (only for NAV nodes)
    def post_nav_login_check(state: SupervisorState) -> SupervisorState:
        page_now = state.get("page") or {}
        logged_in = _looks_logged_in(page_now)
        singpass  = _looks_like_singpass_redirect(page_now)
        url = (page_now.get("url") or "")
        log.info("[supervisor] post_nav_login_check: logged_in=%s singpass=%s url=%s", logged_in, singpass, url)
        need_login = (singpass or not logged_in)
        return {**state, "route": ("login_needed" if need_login else "nav_ok")}

    def post_nav_router(state: SupervisorState):
        return state.get("route")  # "login_needed" | "nav_ok"

    def pause_before_lab_read(state: SupervisorState) -> SupervisorState:
        if LAB_SNAPSHOT_DELAY_MS > 0:
            log.info("[supervisor] pausing %d ms before read to allow snapshot to persist", LAB_SNAPSHOT_DELAY_MS)
            time.sleep(LAB_SNAPSHOT_DELAY_MS / 1000.0)
        return state

    # ───────────────────────── graph ─────────────────────────
    g = StateGraph(SupervisorState)

    # Nodes
    g.add_node("decide", decide)

    # Navigation (pass 1)
    g.add_node("lab",           lambda s: lab_g.invoke(s))
    g.add_node("appointments",  lambda s: appt_g.invoke(s))
    g.add_node("immunisations", lambda s: imm_g.invoke(s))
    g.add_node("payments",      lambda s: pay_g.invoke(s))

    # Login gate
    g.add_node("post_nav_login_check", post_nav_login_check)

    # Readers (pass 2)
    g.add_node("lab_read",      lambda s: lab_read_g.invoke(s))
    g.add_node("appt_read",     lambda s: appt_read_g.invoke(s))
    g.add_node("imm_read",      lambda s: imm_read_g.invoke(s))
    g.add_node("pay_read",      lambda s: pay_read_g.invoke(s))
    g.add_node("pause_before_lab_read", pause_before_lab_read)

    # Edges
    g.add_edge(START, "decide")
    g.add_conditional_edges("decide", router, {
        "lab": "lab",
        "appointments": "appointments",
        "immunisations": "immunisations",
        "payments": "payments",
        # Jump straight to reader when already on target page (pass 2 behavior)
        "lab_read": "lab_read",
        "appt_read": "appt_read",
        "imm_read": "imm_read",
        "pay_read": "pay_read",
    })

    # After any NAV, do login gate then END this turn
    for nav_node in ["lab", "appointments", "immunisations", "payments"]:
        g.add_edge(nav_node, "post_nav_login_check")
    g.add_conditional_edges("post_nav_login_check", post_nav_router, {
        "login_needed": END,
        "nav_ok": END,
    })

    # Readers end their own turn
    g.add_edge("lab_read", END)
    g.add_edge("appt_read", END)
    g.add_edge("imm_read", END)
    g.add_edge("pay_read", END)

    log.info("[supervisor] compiled with flow: NAVIGATE → (Login gate) → END; if already on target, jump to *_read and END")
    return g.compile()
