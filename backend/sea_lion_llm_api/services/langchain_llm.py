from langchain_openai import ChatOpenAI
import logging
from sea_lion_llm_api.config import settings

logger = logging.getLogger(__name__)

def make_llm():
    logger.info("=== CREATING LLM INSTANCE ===")
    
    try:
        model_id = settings.MODEL_ID
        base_url = settings.VLLM_BASE_URL  # Different port to avoid loop
        api_key = settings.VLLM_API_KEY
        temperature = 0.2
        
        logger.info(f"Model ID: {model_id}")
        logger.info(f"Base URL: {base_url}")
        logger.info(f"API Key: {'***' if api_key != 'ignored' else 'ignored'}")
        logger.info(f"Temperature: {temperature}")
        
        # Check if we're using a local service or external API
        if settings.USE_MOCK_LLM:
            from langchain_core.language_models import BaseLLM
            from typing import List, Optional
            class MockLLM(BaseLLM):
                def _call(self, prompt: str, stop: Optional[List[str]] = None) -> str:
                    logger.info(f"Mock SEA-LION LLM called with prompt: {prompt[:200]}...")
                    return "I'm a mock SEA-LION response. Set USE_MOCK_LLM=false to call vLLM."
                @property
                def _llm_type(self) -> str:
                    return "mock_sea_lion"
            logger.warning("Using Mock LLM because USE_MOCK_LLM=True")
            return MockLLM()

        if "localhost" in base_url or "127.0.0.1" in base_url:
            logger.warning("Using local vLLM service for SEA-LION model")
            logger.warning("Make sure vLLM is running on the specified port!")
            
            # Try to use the real local vLLM service
            try:
                llm = ChatOpenAI(
                    model=model_id,
                    base_url=base_url,
                    api_key=api_key,
                    temperature=temperature,
                    timeout=settings.LLM_TIMEOUT_SEC,
                    max_retries=2,
                )
                logger.info("[OK] Real SEA-LION LLM instance created successfully")
            except Exception as vllm_error:
                logger.warning(f"Failed to connect to vLLM: {vllm_error}")
                logger.warning("Falling back to mock LLM for testing")
                
                # Fallback to mock for testing
                from langchain_core.language_models import BaseLLM
                from typing import List, Optional
                
                class MockLLM(BaseLLM):
                    def _call(self, prompt: str, stop: Optional[List[str]] = None) -> str:
                        logger.info(f"Mock SEA-LION LLM called with prompt: {prompt[:200]}...")
                        return "I'm a mock SEA-LION response. Please start your vLLM server with: vllm serve aisingapore/Llama-SEA-LION-v3.5-8B-R --port 8002"
                    
                    @property
                    def _llm_type(self) -> str:
                        return "mock_sea_lion"
                
                llm = MockLLM()
                logger.info("[OK] Mock SEA-LION LLM instance created (vLLM not available)")
        else:
            # Use external API (if you have SEA-LION hosted externally)
            llm = ChatOpenAI(
                model=model_id,
                base_url=base_url,
                api_key=api_key,
                temperature=temperature,
                timeout=settings.LLM_TIMEOUT_SEC,
                max_retries=2,
            )
            logger.info("[OK] External SEA-LION LLM instance created successfully")
        
        logger.info("=== LLM CREATION COMPLETE ===")
        return llm
        
    except Exception as e:
        logger.error(f"[ERROR] LLM CREATION FAILED: {e}")
        logger.error(f"Exception type: {type(e).__name__}")
        import traceback
        logger.error(f"Traceback: {traceback.format_exc()}")
        raise
