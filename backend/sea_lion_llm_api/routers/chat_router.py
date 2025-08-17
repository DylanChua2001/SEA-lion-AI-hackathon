from fastapi import APIRouter, HTTPException
from sea_lion_llm_api.config import settings
from sea_lion_llm_api.schemas.chat import ChatIn, ChatOut
from sea_lion_llm_api.services import (
    memory_services, message_service
)
from sea_lion_llm_api.services.langchain_chain import chat_with_memory
import logging
import requests

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/chat", tags=["chat"])

@router.post("", response_model=ChatOut)
def chat(body: ChatIn):
    logger.info(f"=== CHAT WORKFLOW START ===")
    logger.info(f"Session ID: {body.sessionId}")
    logger.info(f"User message: {body.text[:100]}...")  # Log first 100 chars
    
    try:
        logger.info("STEP 1: Storing user message in database")
        message_service.append(body.sessionId, "user", body.text)
        logger.info("[OK] User message stored successfully")
        
        logger.info("STEP 2: Calling chat_with_memory function")
        reply = chat_with_memory(body.sessionId, body.text)
        logger.info(f"[OK] LLM response received: {reply[:100]}...")  # Log first 100 chars
        
        logger.info("STEP 3: Storing assistant reply in database")
        message_service.append(body.sessionId, "assistant", reply)
        logger.info("[OK] Assistant reply stored successfully")
        
        logger.info("STEP 4: Updating memory for new turn")
        memory_services.on_new_turn(body.sessionId)
        logger.info("[OK] Memory updated successfully")
        
        logger.info("=== CHAT WORKFLOW COMPLETE ===")
        return ChatOut(reply=reply)
        
    except Exception as e:
        logger.error(f"[ERROR] CHAT WORKFLOW FAILED: {str(e)}")
        logger.error(f"Exception type: {type(e).__name__}")
        import traceback
        logger.error(f"Traceback: {traceback.format_exc()}")
        raise HTTPException(status_code=502, detail=f"LLM error: {e}")

DEFAULT_SYSTEM_PROMPT = (
    "You are a calm, patient assistant for elderly users navigating "
    "Singapore e-government websites. Use simple language, step-by-step "
    "instructions, and the user's preferred dialect when possible. "
    "If information is missing, ask one concise question."
)

@router.post("/sealion", response_model=ChatOut)
def chat_sealion(body: ChatIn):
    """
    Route that forwards user input to SEA-LION API directly.
    """
    logger.info("=== SEA-LION API CHAT START ===")
    logger.info(f"Session ID: {body.sessionId}")
    logger.info(f"User message: {body.text[:100]}...")

    headers = {
        "accept": "application/json",
        "Authorization": f"Bearer {settings.ADMIN_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": settings.MODEL_ID,
        "max_completion_tokens": 200,
        "messages": [
            {"role": "system", "content": DEFAULT_SYSTEM_PROMPT}, 
            {"role": "user", "content": body.text}
        ]
    }

    try:
        resp = requests.post(settings.SEA_LION_API_URL, headers=headers, json=payload, timeout=30)
        if resp.status_code == 200:
            data = resp.json()
            reply = data["choices"][0]["message"]["content"]
            logger.info(f"[OK] SEA-LION API reply: {reply[:100]}...")
            return ChatOut(reply=reply)
        else:
            logger.error(f"[ERROR] SEA-LION API failed: {resp.text}")
            raise HTTPException(status_code=resp.status_code, detail=resp.text)

    except Exception as e:
        logger.error(f"[ERROR] SEA-LION API call failed: {e}")
        raise HTTPException(status_code=502, detail=f"SEA-LION API error: {e}")