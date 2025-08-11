import json, re
from typing import List, Optional
from langchain_core.messages import SystemMessage, HumanMessage, AIMessage, AnyMessage
from .config import NORMALIZER_SYSTEM
from .llm import make_llm
from .utils import parse_strict_json, norm_text

# normalizer.py (near top)
SYNONYMS = {
    "appointments": ["appointment", "appointments", "book", "schedule"],
    "payments": ["payment", "payments", "bill", "bills", "pay"],
    "results": ["result", "results", "lab", "lab results", "test", "tests"],
    "login": ["login", "log in", "sign in", "account"],
    "search": ["search", "find"],
}

def _pick_from_vocab(target: str, vocab: List[str]) -> str | None:
    t = target.lower()
    # exact/substring
    for v in vocab:
        if t in v.lower() or v.lower() in t:
            return v
    # synonym pass
    for canon, alts in SYNONYMS.items():
        if t == canon or any(a in t for a in alts):
            for v in vocab:
                if any(a in v.lower() for a in alts):
                    return v
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

        HumanMessage(content="Goal: search for flu vaccine\nPAGE_VOCAB: [\"Search\", \"Appointments\", \"Articles\"]"),
        AIMessage(content='{"intent":"search","target":"flu vaccine","query":"search","canonical_goal":"find(\'search\') then type into the best input with the query text, then done"}'),
    ]

    llm = make_llm(temperature=0)
    messages: List[AnyMessage] = [SystemMessage(content=NORMALIZER_SYSTEM)] + fewshot + [
        HumanMessage(content=f"Goal: {raw_goal}\nPAGE_VOCAB: {json.dumps(page_vocab, ensure_ascii=False)}")
    ]

    resp = llm.invoke(messages)
    try:
        data = parse_strict_json(resp.content)
    except Exception:
        return None

    data = data or {}
    # snap query to on-page vocab (or synonym)
    q = (data.get("query") or "").strip()
    snapped = _pick_from_vocab(q or (data.get("target") or ""), page_vocab)
    if not snapped and page_vocab:
        # last-resort: choose a sensible default on the page
        for prefer in ("appointments", "payments", "search", "results", "login"):
            snapped = _pick_from_vocab(prefer, page_vocab)
            if snapped: break
    # rebuild canonical goal deterministically
    if snapped:
        canon = f"find('{snapped}') then click the best match, then done"
        return canon
    return (data.get("canonical_goal") or "").strip() or None
