# services/translate_service.py
from __future__ import annotations

import os
import requests
from typing import Optional, Tuple

# --- Load .env early (comment out if you load it in main.py already) ---
try:
    from dotenv import load_dotenv
    load_dotenv()  # loads ".env" from CWD; pass an explicit path if needed
except Exception:
    pass

def _getenv(name: str, default: str = "") -> str:
    val = os.getenv(name, default) or ""
    return val.strip()

# Read and normalize (never None)
SEA_LION_API_BASE = _getenv("SEA_LION_API_BASE").rstrip("/")  # e.g. https://api.sea-lion.ai/v1
SEA_LION_API_KEY  = _getenv("SEA_LION_API_KEY")               # optional
SEA_LION_MODEL    = _getenv("SEA_LION_MODEL", "sealion-translate")

def _headers():
    h = {"Content-Type": "application/json"}
    if SEA_LION_API_KEY:
        h["Authorization"] = f"Bearer {SEA_LION_API_KEY}"
    return h

def _sealion_enabled() -> bool:
    # require a proper http(s) base and a model name
    if not SEA_LION_API_BASE or not SEA_LION_MODEL:
        return False
    if not (SEA_LION_API_BASE.startswith("http://") or SEA_LION_API_BASE.startswith("https://")):
        return False
    return True

_SYSTEM_PROMPT_TRANSLATE = (
    "You are a precise translation system. Translate the user's text into the target language. "
    "Preserve numbers, medical entities, names, URLs, and formatting when appropriate. "
    "Return ONLY the translated text. Do not wrap in quotes. Do not add explanations."
)

_SYSTEM_PROMPT_DETECT = (
    "You are a language identification system. Identify the language of the user's text. "
    "Return ONLY a short language name OR ISO 639-1 code (e.g., 'en', 'English', 'zh', 'Chinese'). "
    "No extra words, no punctuation."
)

def translate_with_sealion(text: str, target_lang: str, source_lang: Optional[str] = None) -> str:
    """
    Translate via Sea Lion; if not configured or request fails, return original text.
    """
    if not _sealion_enabled():
        return text

    user_msg = f"Target language: {target_lang}\n"
    if source_lang:
        user_msg += f"Source language: {source_lang}\n"
    user_msg += f"\nText:\n{text}"

    payload = {
        "model": SEA_LION_MODEL,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT_TRANSLATE},
            {"role": "user", "content": user_msg},
        ],
        "temperature": 0.0,
    }
    url = f"{SEA_LION_API_BASE}/chat/completions"
    try:
        resp = requests.post(url, json=payload, headers=_headers(), timeout=60)
        resp.raise_for_status()
        data = resp.json()
        translated = (
            data.get("choices", [{}])[0]
                .get("message", {})
                .get("content", "")
                .strip()
        )
        return translated or text
    except Exception:
        return text  # graceful fallback

def detect_language_with_sealion(text: str) -> str:
    """
    Detect language via Sea Lion; if not configured or request fails, default to 'en'.
    """
    if not _sealion_enabled():
        return "en"

    payload = {
        "model": SEA_LION_MODEL,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT_DETECT},
            {"role": "user", "content": text},
        ],
        "temperature": 0.0,
    }
    url = f"{SEA_LION_API_BASE}/chat/completions"
    try:
        resp = requests.post(url, json=payload, headers=_headers(), timeout=30)
        resp.raise_for_status()
        data = resp.json()
        label = (
            data.get("choices", [{}])[0]
                .get("message", {})
                .get("content", "")
                .strip()
        )
        return label or "en"
    except Exception:
        return "en"

# Map many common labels to a stable pair: (normalized_name, tts_code)
_LANG_MAP = {
    # English
    "en": ("English", "en"),
    "eng": ("English", "en"),
    "english": ("English", "en"),
    # Chinese
    "zh": ("Chinese", "zh"),
    "zh-cn": ("Chinese", "zh"),
    "zh-hans": ("Chinese", "zh"),
    "chinese": ("Chinese", "zh"),
    "mandarin": ("Chinese", "zh"),
    "简体中文": ("Chinese", "zh"),
    "中文": ("Chinese", "zh"),
    # Malay
    "ms": ("Malay", "ms"),
    "msa": ("Malay", "ms"),
    "malay": ("Malay", "ms"),
    "bahasa melayu": ("Malay", "ms"),
    # Tamil
    "ta": ("Tamil", "ta"),
    "tam": ("Tamil", "ta"),
    "tamil": ("Tamil", "ta"),
    # Indonesian
    "id": ("Indonesian", "id"),
    "indonesian": ("Indonesian", "id"),
    "bahasa indonesia": ("Indonesian", "id"),
    # Tagalog/Filipino
    "tl": ("Tagalog", "tl"),
    "fil": ("Tagalog", "tl"),
    "tagalog": ("Tagalog", "tl"),
    "filipino": ("Tagalog", "tl"),
}

def normalize_lang_for_tts(label: str) -> Tuple[str, str]:
    """
    Returns (normalized_target_language_name, tts_code)
    Fallback is English/en.
    """
    key = (label or "").strip().lower()
    if key in _LANG_MAP:
        return _LANG_MAP[key]
    if "chinese" in key or "中文" in key or "mandarin" in key:
        return ("Chinese", "zh")
    if "malay" in key:
        return ("Malay", "ms")
    if "tamil" in key:
        return ("Tamil", "ta")
    if "indonesia" in key:
        return ("Indonesian", "id")
    if "tagalog" in key or "filipino" in key:
        return ("Tagalog", "tl")
    return ("English", "en")
