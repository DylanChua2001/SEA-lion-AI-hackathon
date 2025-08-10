import os, httpx
from typing import Optional

PUPPETEER_BASE_URL = os.getenv("PUPPETEER_BASE_URL", "http://puppeteer:3000")

class BrowserSession:
    def __init__(self, session_id: str):
        self.session_id = session_id

async def browser_start(headless: bool = True) -> BrowserSession:
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(f"{PUPPETEER_BASE_URL}/session/start", json={"headless": headless})
        r.raise_for_status()
        sid = r.json()["sessionId"]
        return BrowserSession(sid)

async def browser_goto(session: BrowserSession, url: str):
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(f"{PUPPETEER_BASE_URL}/session/{session.session_id}/navigate", json={"url": url})
        r.raise_for_status()
        return r.json()

async def browser_click(session: BrowserSession, selector: str):
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(f"{PUPPETEER_BASE_URL}/session/{session.session_id}/click", json={"selector": selector})
        r.raise_for_status()
        return r.json()

async def browser_type(session: BrowserSession, selector: str, text: str, press_enter: bool=False):
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(
            f"{PUPPETEER_BASE_URL}/session/{session.session_id}/type",
            json={"selector": selector, "text": text, "pressEnter": press_enter},
        )
        r.raise_for_status()
        return r.json()

async def browser_wait_for(session: BrowserSession, selector: str, timeout_ms: int = 30000):
    async with httpx.AsyncClient(timeout=timeout_ms/1000 + 5) as client:
        r = await client.post(
            f"{PUPPETEER_BASE_URL}/session/{session.session_id}/waitFor",
            json={"selector": selector, "timeout": timeout_ms},
        )
        r.raise_for_status()
        return r.json()

async def browser_close(session: BrowserSession):
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(f"{PUPPETEER_BASE_URL}/session/{session.session_id}/close")
        r.raise_for_status()
        return r.json()
