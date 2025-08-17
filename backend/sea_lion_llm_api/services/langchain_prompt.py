import json
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from sea_lion_llm_api.data.repositories import memory_repo
import logging

logger = logging.getLogger(__name__)

# Hardcoded system prompt for SEA-LION chatbot
DEFAULT_SYSTEM_PROMPT = (
    "You are a calm, patient assistant for elderly users navigating "
    "Singapore e-government websites. Use simple language, step-by-step "
    "instructions, and the user's preferred dialect when possible. "
    "If information is missing, ask one concise question."
)

def build_prompt(session_id: str) -> ChatPromptTemplate:
    logger.info(f"=== BUILDING PROMPT ===")
    logger.info(f"Session ID: {session_id}")
    
    try:
        logger.info("STEP 1: Loading system prompt")
        sys = DEFAULT_SYSTEM_PROMPT
        logger.info(f"[OK] System prompt loaded: {sys[:100]}...")
        
        logger.info("STEP 2: Getting memory data for session")
        summary, facts = memory_repo.get_for_session(session_id)
        logger.info(f"[OK] Memory retrieved - Summary: {summary[:100]}...")
        logger.info(f"[OK] Facts count: {len(facts) if facts else 0}")
        
        logger.info("STEP 3: Processing facts JSON")
        facts_json = json.dumps(facts, ensure_ascii=False)
        # Escape curly braces so they are treated as literals by the template engine
        facts_json_escaped = facts_json.replace("{", "{{").replace("}", "}}")
        logger.info(f"[OK] Facts JSON processed and escaped")
        
        logger.info("STEP 4: Building context block")
        context_block = (
            f"Context summary:\n{summary}\n\n"
            f"Known facts (JSON): {facts_json_escaped}\n"
            "If facts conflict with the user, politely ask to confirm."
        )
        logger.info(f"[OK] Context block created: {context_block[:200]}...")
        
        logger.info("STEP 5: Creating ChatPromptTemplate")
        prompt_template = ChatPromptTemplate.from_messages([
            ("system", sys),
            ("system", context_block),
            MessagesPlaceholder("chat_history"),
            ("human", "{input}")
        ])
        logger.info("[OK] ChatPromptTemplate created successfully")
        
        logger.info("=== PROMPT BUILDING COMPLETE ===")
        return prompt_template
        
    except Exception as e:
        logger.error(f"[ERROR] PROMPT BUILDING FAILED: {e}")
        logger.error(f"Exception type: {type(e).__name__}")
        import traceback
        logger.error(f"Traceback: {traceback.format_exc()}")
        raise
