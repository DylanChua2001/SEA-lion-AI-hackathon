from fastapi import APIRouter, Request, BackgroundTasks
from fastapi.responses import JSONResponse
from services.stt_service import transcribe_audio
import uuid
import os
import logging

logger = logging.getLogger(__name__)
router = APIRouter()

@router.post("/transcribe")
async def transcribe(
    request: Request,
    background_tasks: BackgroundTasks = None,
):
    try:
        logger.info("Receiving raw audio data...")
        audio_bytes = await request.body()
        if not audio_bytes:
            return JSONResponse(status_code=400, content={"error": "No audio data received"})

        temp_filename = f"stt_{uuid.uuid4().hex}.webm"
        with open(temp_filename, "wb") as f:
            f.write(audio_bytes)

        text = transcribe_audio(temp_filename)

        background_tasks.add_task(os.remove, temp_filename)
        return JSONResponse(content={"text": text})

    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})
