import json, re
from typing import Optional

def parse_strict_json(s: str) -> dict:
    s = (s or "").strip()
    if s.startswith("```"):
        s = s.strip("`\n ")
        i = s.find("{")
        if i != -1: s = s[i:]
    return json.loads(s)

def norm_text(s: Optional[str]) -> str:
    t = (s or "").strip()
    return " ".join(t.split())

def safe_excerpt(txt: str, max_chars: int = 4000) -> str:
    if not txt: return ""
    t = txt.strip()
    if len(t) <= max_chars: return t
    cut = t[:max_chars]
    m = re.search(r'.{0,200}(</[^>]+>|>\s|\s)$', t[max_chars-200:max_chars])
    if m: return t[:max_chars-200] + m.group(0)
    return cut
