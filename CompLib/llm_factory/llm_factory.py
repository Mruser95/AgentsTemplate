from __future__ import annotations

import os
from typing import Any, Optional


class ChatModelFactory:
    """构造 langchain ChatOpenAI，从 ``<role>_llm_{model,base_url,key,temperature}`` 读取。

    role 对应 .env 前缀：agent / code / thinking / small。
    """

    def __init__(self, role: str = "agent") -> None:
        self.role = role

    def build(self, *, temperature: Optional[float] = None, **kwargs) -> Any:
        from langchain_openai import ChatOpenAI

        r = self.role
        temp = temperature
        if temp is None:
            raw = os.getenv(f"{r}_llm_temperature")
            temp = float(raw) if raw is not None else 0.7
        return ChatOpenAI(
            model=os.getenv(f"{r}_llm_model"),
            base_url=os.getenv(f"{r}_llm_base_url"),
            api_key=os.getenv(f"{r}_llm_key"),
            temperature=temp,
            **kwargs,
        )


class EmbeddingFactory:
    """构造嵌入客户端。framework='langchain' → OpenAIEmbeddings；'llama_index' → OpenAILikeEmbedding。

    均取 ``embedding_model`` + ``small_llm_{key,base_url}``；返回对象带 ``aembed_documents``。
    """

    def __init__(self, framework: str = "langchain") -> None:
        self.framework = framework

    def build(self, **kwargs) -> Any:
        model, base, key = os.getenv("embedding_model"), os.getenv("small_llm_base_url"), os.getenv("small_llm_key")
        if self.framework == "llama_index":
            from llama_index.embeddings.openai_like import OpenAILikeEmbedding

            return OpenAILikeEmbedding(model_name=model, api_base=base, api_key=key, **kwargs)
        from langchain_openai import OpenAIEmbeddings

        return OpenAIEmbeddings(model=model, base_url=base, api_key=key, **kwargs)


class LlamaIndexLLMFactory:
    """构造 llama-index OpenAILike（取 ``small_llm_*``），供 QueryFusion 的 ``Settings.llm``。"""

    def build(self, *, context_window: int = 32768, **kwargs) -> Any:
        from llama_index.llms.openai_like import OpenAILike

        return OpenAILike(
            model=os.getenv("small_llm_model"),
            api_base=os.getenv("small_llm_base_url"),
            api_key=os.getenv("small_llm_key"),
            is_chat_model=True,
            context_window=context_window,
            **kwargs,
        )
