from gtts import gTTS

def generate_tts_mp3(text: str, lang: str, filename: str):
    tts = gTTS(text=text, lang=lang)
    tts.save(filename)
