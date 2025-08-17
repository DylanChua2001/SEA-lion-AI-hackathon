from langchain_core.output_parsers import StrOutputParser
from langchain_core.runnables.history import RunnableWithMessageHistory
from langchain_mongodb import MongoDBChatMessageHistory
from sea_lion_llm_api.services.langchain_llm import make_llm
from sea_lion_llm_api.services.langchain_prompt import build_prompt
from sea_lion_llm_api.config import settings
import logging

logger = logging.getLogger(__name__)

def _history(session_id: str):
    logger.info(f"Creating MongoDB history for session: {session_id}")
    try:
        history = MongoDBChatMessageHistory(
            connection_string=settings.MONGODB_URI,
            database_name=settings.DB_NAME,
            collection_name="messages_langchain",  # separate LC history collection
            session_id=session_id,
        )
        logger.info(f"[OK] MongoDB history created successfully")
        return history
    except Exception as e:
        logger.error(f"[ERROR] Failed to create MongoDB history: {e}")
        raise

def build_chain(session_id: str):
    logger.info(f"=== BUILDING CHAIN ===")
    logger.info(f"Session ID: {session_id}")
    
    try:
        logger.info("STEP 1: Building prompt template")
        prompt = build_prompt(session_id)
        logger.info("[OK] Prompt template built successfully")
        
        logger.info("STEP 2: Creating LLM instance")
        llm = make_llm()
        logger.info("[OK] LLM instance created successfully")
        
        logger.info("STEP 3: Creating chain (prompt | llm | StrOutputParser)")
        chain = prompt | llm | StrOutputParser()
        logger.info("[OK] Chain created successfully")

        logger.info("STEP 4: Wrapping chain with message history")
        chain_with_history = RunnableWithMessageHistory(
            chain,
            lambda sid: _history(sid),
            input_messages_key="input",
            history_messages_key="chat_history",
        )
        logger.info("[OK] Chain with history created successfully")
        
        logger.info("=== CHAIN BUILDING COMPLETE ===")
        return chain_with_history
        
    except Exception as e:
        logger.error(f"[ERROR] CHAIN BUILDING FAILED: {e}")
        logger.error(f"Exception type: {type(e).__name__}")
        import traceback
        logger.error(f"Traceback: {traceback.format_exc()}")
        raise

def chat_with_memory(session_id: str, user_text: str) -> str:
    logger.info(f"=== CHAT WITH MEMORY START ===")
    logger.info(f"Session ID: {session_id}")
    logger.info(f"User text: {user_text[:100]}...")
    
    try:
        logger.info("STEP 1: Building chain")
        chain = build_chain(session_id)
        logger.info("[OK] Chain built successfully")
        
        logger.info("STEP 2: Invoking chain with user input")
        logger.info(f"Input payload: {{'input': '{user_text[:50]}...'}}")
        logger.info(f"Config: {{'configurable': {{'session_id': '{session_id}'}}}}")
        
        result = chain.invoke({"input": user_text},
                            config={"configurable": {"session_id": session_id}})
        
        logger.info(f"[OK] Chain invocation successful")
        logger.info(f"Result: {result[:200]}...")  # Log first 200 chars
        logger.info("=== CHAT WITH MEMORY COMPLETE ===")
        return result
        
    except Exception as e:
        logger.error(f"[ERROR] CHAT WITH MEMORY FAILED: {e}")
        logger.error(f"Exception type: {type(e).__name__}")
        import traceback
        logger.error(f"Traceback: {traceback.format_exc()}")
        raise