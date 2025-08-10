import os, httpx

SEA_LION_BASE_URL = os.getenv("SEA_LION_BASE_URL", "https://api.sea-lion.ai/v1")
SEA_LION_API_KEY  = os.getenv("SEA_LION_API_KEY")
SEA_LION_MODEL    = os.getenv("SEA_LION_MODEL", "aisingapore/Gemma-SEA-LION-v3-9B-IT")

class SeaLionChat:
    def __init__(self, model: str | None = None):
        self.model = model or SEA_LION_MODEL
        if not SEA_LION_API_KEY:
            raise RuntimeError("SEA_LION_API_KEY not set")

    async def chat(self, messages: list[dict], temperature: float = 0.2, max_tokens: int = 512):
        headers = {
            "Authorization": f"Bearer {SEA_LION_API_KEY}",
            "Content-Type": "application/json",
        }
        body = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.post(f"{SEA_LION_BASE_URL}/chat/completions", json=body, headers=headers)
            r.raise_for_status()
            data = r.json()
            # OpenAI-style format
            return data["choices"][0]["message"]["content"]
