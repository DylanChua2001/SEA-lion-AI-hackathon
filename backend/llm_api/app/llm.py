from langchain_openai import ChatOpenAI
from .config import SEA_LION_API_KEY, SEA_LION_BASE_URL, SEA_LION_MODEL

def make_llm(temperature: float = 0) -> ChatOpenAI:
    return ChatOpenAI(
        base_url=SEA_LION_BASE_URL,
        api_key=SEA_LION_API_KEY,
        model=SEA_LION_MODEL,
        temperature=temperature,
    )