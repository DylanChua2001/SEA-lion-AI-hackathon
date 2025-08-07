from fastapi import APIRouter, BackgroundTasks
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
from services.tts_service import generate_tts_mp3
import uuid
import os
import logging

logger = logging.getLogger(__name__)
router = APIRouter()

# Define the request body schema
class SpeakRequest(BaseModel):
    text: str
    lang: str = "en"

@router.post("/speak")
def speak(
    request: SpeakRequest,
    background_tasks: BackgroundTasks = None
):
    try:
        logger.info('hi')
        filename = f"tts_{uuid.uuid4().hex}.mp3"
        generate_tts_mp3(request.text, request.lang, filename)
        background_tasks.add_task(os.remove, filename)
        return FileResponse(filename, media_type="audio/mpeg", filename=filename)
    except ValueError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})
