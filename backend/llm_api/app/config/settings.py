from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    SEA_LION_API_KEY: str
    SEA_LION_BASE_URL: str = "https://api.sea-lion.ai/v1"
    SEA_LION_MODEL: str = "aisingapore/Gemma-SEA-LION-v3-9B-IT"
    TIMEOUT_S: float = 20.0
    class Config: env_file = ".env"

settings = Settings()
