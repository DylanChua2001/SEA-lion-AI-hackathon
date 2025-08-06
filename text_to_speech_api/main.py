from fastapi import FastAPI, Query, BackgroundTasks
from fastapi.responses import FileResponse, JSONResponse
from gtts import gTTS
import uuid
import os

app = FastAPI()

@app.get("/speak")
def speak(text: str = Query(..., min_length=1), lang: str = Query("en"), background_tasks: BackgroundTasks = None):
    try:
        filename = f"tts_{uuid.uuid4().hex}.mp3"
        tts = gTTS(text=text, lang=lang)
        tts.save(filename)
        print('d')

        # Auto-delete the file after response is sent
        background_tasks.add_task(os.remove, filename)

        return FileResponse(
            filename,
            media_type="audio/mpeg",
            filename=filename
        )

    except ValueError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})
