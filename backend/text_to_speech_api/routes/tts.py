# routes/tts.py
from fastapi import APIRouter, BackgroundTasks
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field
from services.tts_service import generate_tts_mp3
from services.translate_service import (
    translate_with_sealion,
    detect_language_with_sealion,
    normalize_lang_for_tts,
)
import uuid
import os
import logging

logger = logging.getLogger(__name__)
router = APIRouter()

class SpeakRequest(BaseModel):
    text: str = Field(..., description="Input text to speak")
    # gTTS (or your TTS layer) code. 'auto' triggers Sea Lion language detection.
    lang: str = Field("auto", description="TTS language code, or 'auto' to detect")
    # 'auto' = detect language from text and speak it back in that language.
    translate_to: str | None = Field(
        "auto",
        description="Target language for Sea Lion translation, or 'auto' to detect from text."
    )
    source_lang: str | None = Field(None, description="Optional hint for source language")

@router.post("/speak")
def speak(
    request: SpeakRequest,
    background_tasks: BackgroundTasks
):
    """
    Flow:
      - If translate_to/lang are 'auto' (or missing), detect the language with Sea Lion
      - Translate text into that language (no-op if same language)
      - TTS using the mapped voice code
    """
    try:
        text = request.text or ""
        if not text.strip():
            return JSONResponse(status_code=400, content={"error": "Empty text"})

        # 1) Detect language if needed
        target_name = None
        tts_code = None

        if (request.translate_to is None) or (str(request.translate_to).lower() == "auto") \
           or (request.lang is None) or (str(request.lang).lower() == "auto"):
            try:
                detected_label = detect_language_with_sealion(text)
                target_name, tts_code = normalize_lang_for_tts(detected_label)
                logger.info("Detected language '%s' → target=%s, tts=%s",
                            detected_label, target_name, tts_code)
            except Exception:
                # fallback: English
                target_name, tts_code = ("English", "en")
        else:
            # explicit values: normalize anyway for robust TTS code
            target_name, tts_code = normalize_lang_for_tts(str(request.translate_to))

        # honor explicit lang override if user gave one (not 'auto')
        if request.lang and str(request.lang).lower() != "auto":
            # trust the caller; still keep normalized code around if needed
            tts_code = request.lang

        # 2) Translate (idempotent if same language)
        final_text = text
        try:
            final_text = translate_with_sealion(
                text=text,
                target_lang=target_name or "English",
                source_lang=request.source_lang
            )
        except Exception:
            logger.exception("Sea Lion translation failed; falling back to original text")
            final_text = text

        # 3) Synthesize
        filename = f"tts_{uuid.uuid4().hex}.mp3"
        generate_tts_mp3(final_text, tts_code or "en", filename)
        background_tasks.add_task(os.remove, filename)

        return FileResponse(filename, media_type="audio/mpeg", filename=filename)

    except ValueError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})
    except Exception:
        logger.exception("Unexpected error in /speak")
        return JSONResponse(status_code=500, content={"error": "Internal server error"})
