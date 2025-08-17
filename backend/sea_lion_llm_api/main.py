from fastapi import FastAPI
from sea_lion_llm_api.routers import chat_router, health_router
from sea_lion_llm_api.services.langchain_llm import make_llm
from pydantic import BaseModel
from typing import List, Optional
import logging

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(name)s | %(levelname)s | %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('app.log')
    ]
)

logger = logging.getLogger(__name__)

app = FastAPI(title="SEA-LION Chatbot API", version="1.0.0")

# OpenAI-compatible models for LangChain
class ChatMessage(BaseModel):
    role: str
    content: str

class ChatCompletionRequest(BaseModel):
    model: str
    messages: List[ChatMessage]
    temperature: Optional[float] = 0.7
    max_tokens: Optional[int] = 1000

class ChatCompletionResponse(BaseModel):
    id: str
    object: str = "chat.completion"
    created: int
    model: str
    choices: List[dict]
    usage: dict

@app.post("/v1/chat/completions")
async def chat_completions(request: ChatCompletionRequest):
    import time
    import uuid
    
    logger.info(f"=== OPENAI COMPATIBLE ENDPOINT CALLED ===")
    logger.info(f"Model: {request.model}")
    logger.info(f"Messages count: {len(request.messages)}")
    logger.info(f"Temperature: {request.temperature}")
    
    try:
        logger.info("Creating LLM instance for OpenAI-compatible endpoint")
        llm = make_llm()
        
        # Convert messages to string
        messages_text = "\n".join([f"{msg.role}: {msg.content}" for msg in request.messages])
        logger.info(f"Converted messages to text: {messages_text[:200]}...")
        
        # Get response from LLM
        logger.info("Invoking LLM")
        response = llm.invoke(messages_text)
        logger.info(f"LLM response: {response[:200]}...")
        
        result = ChatCompletionResponse(
            id=f"chatcmpl-{uuid.uuid4().hex}",
            created=int(time.time()),
            model=request.model,
            choices=[{
                "index": 0,
                "message": {"role": "assistant", "content": response},
                "finish_reason": "stop"
            }],
            usage={"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        )
        
        logger.info("[OK] OpenAI-compatible response created successfully")
        return result
        
    except Exception as e:
        logger.error(f"[ERROR] OPENAI COMPATIBLE ENDPOINT FAILED: {e}")
        logger.error(f"Exception type: {type(e).__name__}")
        import traceback
        logger.error(f"Traceback: {traceback.format_exc()}")
        raise

# Essential routers only
app.include_router(health_router.router)
app.include_router(chat_router.router)

logger.info("=== SEA-LION CHATBOT API STARTED ===")
logger.info("Available endpoints:")
logger.info("- /healthz (health check)")
logger.info("- /chat (main chat endpoint)")
logger.info("- /v1/chat/completions (OpenAI-compatible)")
logger.info("REMOVED: auth, sessions, prompts routers (not needed for basic chat)")
