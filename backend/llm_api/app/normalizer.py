import json, re
from typing import List, Optional
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
    counts = {t:0 for t in _tokens(g)}
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


# normalizer.py (near top)
SYNONYMS = {
    "appointments": ["appointment", "appointments", "book", "schedule"],
    "payments": ["payment", "payments", "bill", "bills", "pay"],
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

def llm_normalize_goal(raw_goal: str, page_vocab: List[str]) -> Optional[str]:
    if not raw_goal: return None

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

    # 0) Generic manage→create recipe: if the goal sounds like "create/book/apply/start…"
    if re.search(rf"\b{GOAL_CREATE_VERBS}\b", raw_goal, flags=re.I):
        qs = build_query_set(raw_goal, {"buttons": [{"text": v} for v in page_vocab],
                                        "links":   [{"text": v} for v in page_vocab]})
        entry_or  = " | ".join(qs["entry_queries"])
        create_or = " | ".join(qs["create_queries"])

        # Try to snap each OR query to something actually on page (best-effort)
        snapped_entry  = _pick_from_vocab(entry_or,  page_vocab) or entry_or
        snapped_create = _pick_from_vocab(create_or, page_vocab) or create_or

        # Deterministic, tool-friendly canonical plan (multi-hop)
        return (
            f"find('{snapped_entry}') then click the best match, then wait(600), "
            f"find('{snapped_create}') then click the best match, then wait(600), then done"
        )

    # 1) Otherwise fall back to your existing few-shot LLM normalisation (single hop)
    resp = llm.invoke(messages)
    try:
        data = parse_strict_json(resp.content)
    except Exception:
        return None

    data = data or {}
    q = (data.get("query") or "").strip()
    snapped = _pick_from_vocab(q or (data.get("target") or ""), page_vocab)
    if not snapped and page_vocab:
        for prefer in ("appointments", "payments", "search", "results", "login"):
            snapped = _pick_from_vocab(prefer, page_vocab)
            if snapped: break
    if snapped:
        return f"find('{snapped}') then click the best match, then done"
    return (data.get("canonical_goal") or "").strip() or None