import os
from pathlib import Path
from typing import Any

from langchain.chat_models import BaseChatModel

from langchain_openai import ChatOpenAI

from langchain_ollama import ChatOllama
from pydantic import SecretStr


def load_llm(provider: str, name: str, temperature: float | None = None) -> BaseChatModel:
    model_kwargs: dict[str, Any] = {}
    if temperature is not None:
        model_kwargs["temperature"] = temperature

    provider_key = provider.lower()
    if provider_key == "openai":
        if os.getenv("OPENAI_API_KEY"):
            return ChatOpenAI(model=name, **model_kwargs)
        key_path = Path(__file__).resolve().parent / "API_KEY.txt"
        api_key = None
        if key_path.exists():
            api_key = key_path.read_text().strip() or None
        if api_key:
            return ChatOpenAI(model=name, api_key=SecretStr(api_key), **model_kwargs)
        return ChatOpenAI(model=name, **model_kwargs)
    if provider_key == "ollama":
        return ChatOllama(model=name, **model_kwargs)

    raise ValueError("Only 'openai' and 'ollama' providers are supported")
