import json, re
from typing import List, Optional, Dict, Tuple
from langchain_core.messages import SystemMessage, HumanMessage, AIMessage, AnyMessage
from .config import NORMALIZER_SYSTEM
from .llm import make_llm
from .utils import parse_strict_json, norm_text

# ── Generic "manage → create" recipe primitives ──────────────────────────────
ENTRY_TERMS = [
    "manage {thing}", "{thing}s", "my {thing}s", "{thing} centre", "{thing} management",
    "dashboard", "records", "history", "portal", "inbox"
]
CREATE_TERMS = [
    "new {thing}", "create {thing}", "add {thing}", "book {thing}", "apply {thing}",
    "start {thing}", "begin {thing}", "+ new", "book now", "get started"
]
GOAL_CREATE_VERBS = r"(new|create|add|book|apply|start|begin|open|schedule|register)"

def _tokens(s: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", (s or "").lower())

def extract_thing(goal: str, page: dict) -> str:
    """Heuristic noun (THING) guess from goal, with light page-backed fallback."""
    g = (goal or "")
    m = re.search(rf"\b{GOAL_CREATE_VERBS}\b\s+(?:a|an|the)?\s*([a-z0-9 \-/]+)", g, flags=re.I)
    if m:
        cand = m.group(1).strip()
        cand = re.sub(r"\b(on|for|to|at|in)\b.*$", "", cand).strip()
        # tiny cleanup examples (extend as needed)
        cand = re.sub(r"\b(appt)\b", "appointment", cand)
        if cand:
            return cand
    # Backoff: intersect goal tokens with frequent page tokens
    page_texts = []
    for k in ("buttons", "links"):
        for x in page.get(k, []):
            page_texts.append(x.get("text") or "")
    counts = {t: 0 for t in _tokens(g)}
    for txt in page_texts:
        for t in _tokens(txt):
            if t in counts: counts[t] += 1
    if counts:
        t, freq = max(counts.items(), key=lambda kv: kv[1])
        if freq > 0: return t
    return "item"

def build_query_set(goal: str, page: dict) -> dict:
    """Fill generic entry/create patterns for the inferred THING."""
    thing = extract_thing(goal, page)
    def fill(patterns): return [p.format(thing=thing) for p in patterns]
    return {
        "thing": thing,
        "entry_queries": fill(ENTRY_TERMS),
        "create_queries": fill(CREATE_TERMS),
    }

# ── Canonical synonyms and page vocab snapping ───────────────────────────────
SYNONYMS = {
    "appointments": ["appointment", "appointments", "book", "schedule"],
    "payments": ["payment", "payments", "bill", "bills", "pay"],
    "records": ["record", "records", "medical record", "immunisation", "immunisations", "immunization", "immunizations"],
    "results": ["result", "results", "lab", "lab results", "test", "tests"],
    "login": ["login", "log in", "sign in", "account"],
    "search": ["search", "find"],
}

def _pick_from_vocab(target: str, vocab: List[str]) -> str | None:
    # Support "A | B | C" (try in order)
    candidates = [s.strip() for s in (target or "").split("|") if s.strip()]
    if not candidates:
        candidates = [(target or "").strip()]
    vl = [v.lower() for v in vocab]
    for t in candidates:
        tl = t.lower()
        # exact/substring
        for i, v in enumerate(vl):
            if tl and (tl in v or v in tl):
                return vocab[i]
        # synonym pass
        for canon, alts in SYNONYMS.items():
            if tl == canon or any(a in tl for a in alts):
                for i, v in enumerate(vl):
                    if any(a in v for a in alts):
                        return vocab[i]
    return None

def build_page_vocab(page: dict, max_items: int = 80) -> List[str]:
    seen, out = set(), []

    def add(txt: Optional[str]):
        t = norm_text(txt)
        if not t: return
        k = t.lower()
        if k in seen: return
        seen.add(k); out.append(t)

    for item in page.get("clickables_preview", []):
        add(item.get("text"))

    for b in page.get("buttons", []):
        add(b.get("text"))
    for a in page.get("links", []):
        add(a.get("text"))
    for i in page.get("inputs", []):
        add(i.get("name") or i.get("placeholder"))

    raw = page.get("raw_html") or ""
    if isinstance(raw, str) and raw:
        for m in re.finditer(r'<a\b[^>]*>(.*?)</a\s*>', raw, flags=re.I|re.S):
            add(re.sub(r'<[^>]+>', '', m.group(1)))
        for m in re.finditer(r'<button\b[^>]*>(.*?)</button\s*>', raw, flags=re.I|re.S):
            add(re.sub(r'<[^>]+>', '', m.group(1)))
        for m in re.finditer(r'(?:aria-label|placeholder|alt)\s*=\s*["\']([^"\']{2,80})["\']', raw, flags=re.I):
            add(m.group(1))

    return out[:max_items]

# ── Four-path router (hard rails) ────────────────────────────────────────────
PATH_SYNONYMS = {
    "appointments": ["appointment", "appointments", "book", "schedule", "reschedule", "cancel slot", "view appointments"],
    "lab_results": ["lab", "labs", "result", "results", "test", "tests", "lab report", "reports"],
    "payments": ["pay", "payment", "payments", "bill", "bills", "invoice", "outstanding", "fees"],
    "immunisations": ["immunisation", "immunization", "vaccination", "vaccine", "jab", "shots", "immunisation records", "records"],
}

# Entry + Create queries per path (used for snapping + fallbacks)
PATH_QUERIES: Dict[str, Dict[str, List[str]]] = {
    "appointments": {
        "entry": ["appointments", "manage appointments", "my appointments", "appointment centre"],
        "create": ["book appointment", "new appointment", "schedule appointment", "book now"],
    },
    "lab_results": {
        "entry": ["lab results", "results", "test results", "lab reports", "medical records"],
        "create": [],  # usually view-only; keep create empty
    },
    "payments": {
        "entry": ["payments", "pay bills", "outstanding bills", "billing", "make payment"],
        "create": ["make payment", "pay now", "settle bill"],  # 'create' = initiate payment flow
    },
    "immunisations": {
        "entry": ["immunisation records", "vaccination records", "records", "my records"],
        "create": ["book vaccination", "schedule vaccination", "new vaccination", "book jab"],
    },
}

def classify_path(goal: str) -> str:
    """Map any free-form goal to one of the four canonical paths."""
    g = (goal or "").lower()
    # priority order if multiple match
    order = ["appointments", "lab_results", "payments", "immunisations"]
    for key in order:
        for kw in PATH_SYNONYMS[key]:
            if kw in g:
                return key
    # token fallback
    toks = set(_tokens(g))
    if {"appointment", "book", "schedule"} & toks: return "appointments"
    if {"lab", "result", "results", "test", "report", "reports"} & toks: return "lab_results"
    if {"pay", "payment", "payments", "bill", "bills", "invoice"} & toks: return "payments"
    if {"immunisation", "immunization", "vaccination", "vaccine", "jab", "shots", "record", "records"} & toks:
        return "immunisations"
    # default: appointments
    return "appointments"

def _snap_any(candidates: List[str], page_vocab: List[str]) -> str | None:
    for q in candidates:
        hit = _pick_from_vocab(q, page_vocab)
        if hit: return hit
    return None

def _plan_from_path(path: str, page_vocab: List[str], create_like: bool) -> str:
    """Return a deterministic plan constrained to a single path."""
    q = PATH_QUERIES.get(path, PATH_QUERIES["appointments"])
    snapped_entry  = _snap_any(q["entry"],  page_vocab) or (q["entry"][0] if q["entry"] else "home")
    if create_like and q["create"]:
        snapped_create = _snap_any(q["create"], page_vocab) or q["create"][0]
        # multi-hop plan (entry → create)
        return (
            f"find('{snapped_entry}') then click the best match, then wait(600), "
            f"find('{snapped_create}') then click the best match, then wait(600), then done"
        )
    # single-hop plan (entry only / view)
    return f"find('{snapped_entry}') then click the best match, then done"

# ── Public: LLM normalizer with four-path hard routing ───────────────────────
def llm_normalize_goal(raw_goal: str, page_vocab: List[str]) -> Optional[str]:
    """
    Always produce a canonical plan that maps to exactly ONE of:
      - appointments | lab_results | payments | immunisations

    If the goal contains create-like verbs, we use a two-step plan (entry → create).
    Otherwise a single-hop plan to the path's entry surface.

    We still keep your LLM few-shot as a *backstop* for rare cases where snapping
    utterly fails, but the path is always constrained to the four rails.
    """
    if not raw_goal:
        return None

    # 1) Classify to one of the four paths
    path = classify_path(raw_goal)

    # 2) Decide if this is a "create-like" request
    create_like = bool(re.search(rf"\b{GOAL_CREATE_VERBS}\b", raw_goal, flags=re.I))

    # 3) Try a deterministic plan using page snapping
    plan = _plan_from_path(path, page_vocab, create_like)
    if plan:
        return plan

    # 4) (Rare) Backstop to your few-shot normalizer, but still snap to page and constrain to a single hop
    fewshot = [
        HumanMessage(content="Goal: manage my appointments\nPAGE_VOCAB: [\"Appointments\", \"Payments\", \"Lab Results\", \"Login\"]"),
        AIMessage(content='{"intent":"manage","target":"appointments","query":"appointments","canonical_goal":"find(\'appointments\') then click the best match, then done"}'),

        HumanMessage(content="Goal: pay outstanding bills\nPAGE_VOCAB: [\"Appointments\", \"Payments\", \"Lab Results\", \"Login\"]"),
        AIMessage(content='{"intent":"pay","target":"payments","query":"payments","canonical_goal":"find(\'payments\') then click the best match, then done"}'),
    ]
    llm = make_llm(temperature=0)
    messages: List[AnyMessage] = [SystemMessage(content=NORMALIZER_SYSTEM)] + fewshot + [
        HumanMessage(content=f"Goal: {raw_goal}\nPAGE_VOCAB: {json.dumps(page_vocab, ensure_ascii=False)}")
    ]

    try:
        resp = llm.invoke(messages)
        data = parse_strict_json(resp.content) or {}
    except Exception:
        data = {}

    q = (data.get("query") or data.get("target") or "").strip()
    snapped = _pick_from_vocab(q, page_vocab)
    if not snapped:
        # last-resort: use the path entry again
        snapped = _snap_any(PATH_QUERIES[path]["entry"], page_vocab) or PATH_QUERIES[path]["entry"][0]
    return f"find('{snapped}') then click the best match, then done"
