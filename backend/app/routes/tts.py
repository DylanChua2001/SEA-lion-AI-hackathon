from fastapi import APIRouter, Query, BackgroundTasks
from fastapi.responses import FileResponse, JSONResponse
from services.tts_service import generate_tts_mp3
import uuid
import os

import logging
logger = logging.getLogger(__name__)

router = APIRouter()

@router.get("/speak")
def speak(
    text: str = Query(..., min_length=1),
    lang: str = Query("en"),
    background_tasks: BackgroundTasks = None
):
    try:
        logger.info('hi')
        filename = f"tts_{uuid.uuid4().hex}.mp3"
        generate_tts_mp3(text, lang, filename)
        background_tasks.add_task(os.remove, filename)
        return FileResponse(filename, media_type="audio/mpeg", filename=filename)
    except ValueError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})
