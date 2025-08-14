from fastapi import APIRouter, HTTPException
from sea_lion_llm_api.schemas.chat import ChatIn, ChatOut
from sea_lion_llm_api.services import (
    memory_services, message_service
)
from sea_lion_llm_api.services.langchain_chain import chat_with_memory
import logging

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
