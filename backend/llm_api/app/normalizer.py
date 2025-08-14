# app/normalizer.py
import json, re
from typing import List, Optional, Dict
from langchain_core.messages import SystemMessage, HumanMessage, AIMessage, AnyMessage
from .config import NORMALIZER_SYSTEM
from .llm import make_llm
from .utils import parse_strict_json, norm_text

# ── utils ────────────────────────────────────────────────────────────────────
GOAL_CREATE_VERBS = r"(new|create|add|book|apply|start|begin|open|schedule|register)"

def _tokens(s: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", (s or "").lower())

def _json_arg(s: str) -> str:
    """Produce a JSON-escaped string literal suitable to embed in plan text."""
    return json.dumps(s or "")

# ── synonyms / snapping ──────────────────────────────────────────────────────
SYNONYMS = {
    "appointments": ["appointment", "appointments", "book", "schedule", "reschedule", "cancel"],
    "payments": ["payment", "payments", "bill", "bills", "pay", "invoice"],
    "records": ["record", "records", "medical record", "immunisation", "immunisations", "immunization", "immunizations"],
    "results": ["result", "results", "lab", "lab results", "test", "tests", "report", "reports"],
    "login": ["login", "log in", "sign in", "account"],
    "search": ["search", "find"],
}

def _pick_from_vocab(target: str, vocab: List[str]) -> Optional[str]:
    """Pick a label from page vocab matching target (exact → substring → synonyms)."""
    if not target or not vocab:
        return None
    candidates = [s.strip() for s in (target or "").split("|") if s.strip()] or [target]
    vl = [v for v in vocab if v]  # preserve original casing
    vll = [v.lower() for v in vl]

    # exact first
    for t in candidates:
        tl = t.lower()
        for i, v in enumerate(vll):
            if tl == v:
                return vl[i]

    # substring next
    for t in candidates:
        tl = t.lower()
        for i, v in enumerate(vll):
            if tl and (tl in v or v in tl):
                return vl[i]

    # synonyms
    for t in candidates:
        tl = t.lower()
        for canon, alts in SYNONYMS.items():
            if tl == canon or any(a in tl for a in alts):
                for i, v in enumerate(vll):
                    if any(a in v for a in alts):
                        return vl[i]
    return None

def build_page_vocab(page: dict, max_items: int = 80) -> List[str]:
    seen, out = set(), []

    def add(txt: Optional[str]):
        t = norm_text(txt)
        if not t:
            return
        k = t.lower()
        if k in seen:
            return
        seen.add(k)
        out.append(t)

    for item in (page.get("clickables_preview") or []):
        add(item.get("text"))

    for b in (page.get("buttons") or []):
        add(b.get("text"))
    for a in (page.get("links") or []):
        add(a.get("text"))
    for i in (page.get("inputs") or []):
        add(i.get("name") or i.get("placeholder"))

    raw = page.get("raw_html") or ""
    if isinstance(raw, str) and raw:
        for m in re.finditer(r'<a\b[^>]*>(.*?)</a\s*>', raw, flags=re.I | re.S):
            add(re.sub(r'<[^>]+>', '', m.group(1)))
        for m in re.finditer(r'<button\b[^>]*>(.*?)</button\s*>', raw, flags=re.I | re.S):
            add(re.sub(r'<[^>]+>', '', m.group(1)))
        for m in re.finditer(r'(?:aria-label|placeholder|alt)\s*=\s*["\']([^"\']{2,80})["\']', raw, flags=re.I):
            add(m.group(1))

    return out[:max_items]

# ── four-path router (hard rails) ────────────────────────────────────────────
PATH_SYNONYMS = {
    "appointments": ["appointment", "appointments", "book", "schedule", "reschedule", "cancel", "view appointments"],
    "lab_results": ["lab", "labs", "result", "results", "test", "tests", "lab report", "reports"],
    "payments": ["pay", "payment", "payments", "bill", "bills", "invoice", "outstanding", "fees"],
    "immunisations": ["immunisation", "immunization", "vaccination", "vaccine", "jab", "shots", "immunisation records", "records"],
}

PATH_QUERIES: Dict[str, Dict[str, List[str]]] = {
    "appointments": {
        "entry":  ["Appointments", "Manage Appointments", "My Appointments", "Appointment Centre"],
        "create": ["Book Appointment", "New Appointment", "Schedule Appointment", "Book Now"],
    },
    "lab_results": {
        "entry":  ["Lab Results", "Results", "Test Results", "Lab Reports", "Medical Records"],
        "create": [],
    },
    "payments": {
        "entry":  ["Payments", "Pay Bills", "Outstanding Bills", "Billing", "Make Payment"],
        "create": ["Make Payment", "Pay Now", "Settle Bill"],
    },
    "immunisations": {
        "entry":  ["Immunisation Records", "Vaccination Records", "Records", "My Records"],
        "create": ["Book Vaccination", "Schedule Vaccination", "New Vaccination", "Book Jab"],
    },
}

def classify_path(goal: str) -> str:
    g = (goal or "").lower()
    order = ["appointments", "lab_results", "payments", "immunisations"]
    for key in order:
        for kw in PATH_SYNONYMS[key]:
            if kw in g:
                return key
    toks = set(_tokens(g))
    if {"appointment", "book", "schedule"} & toks: return "appointments"
    if {"lab", "result", "results", "test", "report", "reports"} & toks: return "lab_results"
    if {"pay", "payment", "payments", "bill", "bills", "invoice"} & toks: return "payments"
    if {"immunisation", "immunization", "vaccination", "vaccine", "jab", "shots", "record", "records"} & toks:
        return "immunisations"
    return "appointments"

def _snap_any(candidates: List[str], page_vocab: List[str]) -> Optional[str]:
    for q in candidates:
        hit = _pick_from_vocab(q, page_vocab)
        if hit:
            return hit
    return None

def _plan_from_path(path: str, page_vocab: List[str], create_like: bool) -> str:
    """Return a deterministic plan constrained to a single path, with safe JSON-escaped args."""
    qset = PATH_QUERIES.get(path, PATH_QUERIES["appointments"])
    entry_label = _snap_any(qset["entry"], page_vocab) or (qset["entry"][0] if qset["entry"] else "Home")
    entry_arg = _json_arg(entry_label)
    # Keep waits small; content.js caps at 60s anyway
    if create_like and qset["create"]:
        create_label = _snap_any(qset["create"], page_vocab) or qset["create"][0]
        create_arg = _json_arg(create_label)
        return (
            f"find({entry_arg}) then click the best match, then wait(1), "
            f"find({create_arg}) then click the best match, then wait(1), then done"
        )
    return f"find({entry_arg}) then click the best match, then done"

# ── public: LLM normalizer with rails + page snapping ────────────────────────
def llm_normalize_goal(raw_goal: str, page_vocab: List[str]) -> Optional[str]:
    """
    Produce a canonical, deterministic mini-plan restricted to:
      appointments | lab_results | payments | immunisations
    """
    if not raw_goal:
        return None

    path = classify_path(raw_goal)
    create_like = bool(re.search(rf"\b{GOAL_CREATE_VERBS}\b", raw_goal, flags=re.I))

    # Deterministic plan via page snapping
    plan = _plan_from_path(path, page_vocab or [], create_like)
    if plan:
        return plan

    # Backstop to LLM few-shot (rare), still snapped to page and single-hop
    fewshot = [
        HumanMessage(content='Goal: manage my appointments\nPAGE_VOCAB: ["Appointments","Payments","Lab Results","Login"]'),
        AIMessage(content='{"intent":"manage","target":"appointments","query":"appointments","canonical_goal":"find(\\"appointments\\") then click the best match, then done"}'),
        HumanMessage(content='Goal: pay outstanding bills\nPAGE_VOCAB: ["Appointments","Payments","Lab Results","Login"]'),
        AIMessage(content='{"intent":"pay","target":"payments","query":"payments","canonical_goal":"find(\\"payments\\") then click the best match, then done"}'),
    ]
    llm = make_llm(temperature=0)
    messages: List[AnyMessage] = [SystemMessage(content=NORMALIZER_SYSTEM)] + fewshot + [
        HumanMessage(content=f"Goal: {raw_goal}\nPAGE_VOCAB: {json.dumps(page_vocab or [], ensure_ascii=False)}")
    ]

    try:
        resp = llm.invoke(messages)
        data = parse_strict_json(resp.content) or {}
    except Exception:
        data = {}

    q = (data.get("query") or data.get("target") or "").strip()
    snapped = _pick_from_vocab(q, page_vocab or [])
    if not snapped:
        snapped = _snap_any(PATH_QUERIES[path]["entry"], page_vocab or []) or PATH_QUERIES[path]["entry"][0]
    return f"find({_json_arg(snapped)}) then click the best match, then done"
