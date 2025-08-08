from typing import Any, Dict, List
from tenacity import retry, stop_after_attempt, wait_exponential
from langchain_openai import ChatOpenAI
from langchain.schema import BaseMessage
import httpx

class SeaLionClient:
    def __init__(self, *, api_key: str, base_url: str, model: str,
                 timeout_s: float = 20.0, cache: bool = True):
        self._base_llm = ChatOpenAI(
            api_key=api_key,
            base_url=base_url,
            model=model,
            max_tokens=512,
            temperature=0.2,
            cache=cache,
            http_client=httpx.Client(timeout=timeout_s),
        )

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=0.5, max=4), reraise=True)
    def chat(self, messages: List[BaseMessage], **per_call_kwargs) -> Dict[str, Any]:
        llm = self._base_llm.bind(**per_call_kwargs) if per_call_kwargs else self._base_llm
        res = llm.invoke(messages)
        return {"content": res.content, "meta": getattr(res, "response_metadata", {})}
