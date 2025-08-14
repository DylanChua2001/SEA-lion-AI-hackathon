from pydantic_settings import BaseSettings, SettingsConfigDict
# import os
# from dotenv import load_dotenv

# load_dotenv()

class Settings(BaseSettings):
    # Mongo
    MONGODB_URI: str = ""
    DB_NAME: str = "lingolah_sea_lion_llm"

    # vLLM (OpenAI-compatible)
    # Use 8002 by default to avoid conflicts with FastAPI app on 8001
    VLLM_BASE_URL: str = "http://localhost:8002/v1"
    VLLM_API_KEY: str = "ignored"  # vLLM can accept a dummy API key
    MODEL_ID: str = "aisingapore/Llama-SEA-LION-v3.5-8B-R"
    LLM_TIMEOUT_SEC: int = 60
    USE_MOCK_LLM: bool = False

    # Prompts
    PROMPT_KEY: str = "elderly_assistant_v1"

    # Sessions & Context
    SESSION_TTL_HOURS: int = 24
    CONTEXT_LAST_N: int = 12

    ADMIN_API_KEY: str = ""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",          # ignore unexpected envs
    )

settings = Settings()