---
name: llm_factory
description: 从环境变量构造模型客户端：聊天模型 / 嵌入 / llama-index LLM。按 role 前缀读 model/base_url/key，懒导入按需拉依赖。一个完整功能：拼装 agent 时的模型接线层。
---

# llm_factory — ChatModelFactory / EmbeddingFactory / LlamaIndexLLMFactory

实现文件：`CompLib/llm_factory/llm_factory.py`（单文件）

## 用途
把散落各处的客户端构造收成一处，按 .env 约定前缀读取，要哪个拿哪个。喂给 agent_factory / vector_memory / hybrid_retrieval。

## 接口
`from CompLib.llm_factory.llm_factory import ChatModelFactory, EmbeddingFactory, LlamaIndexLLMFactory`

- `ChatModelFactory(role="agent").build(*, temperature=None, **kwargs) -> ChatOpenAI`
  - role 对应 env 前缀：`agent` / `code` / `thinking` / `small`，读 `<role>_llm_{model,base_url,key,temperature}`；kwargs 透传（如 `max_tokens`）
- `EmbeddingFactory(framework="langchain").build(**kwargs)`
  - `langchain` → OpenAIEmbeddings；`llama_index` → OpenAILikeEmbedding；取 `embedding_model` + `small_llm_{key,base_url}`；返回对象带 `aembed_documents`
- `LlamaIndexLLMFactory().build(*, context_window=32768, **kwargs)` → OpenAILike（取 `small_llm_*`）

## 约定环境变量
`agent_llm_* / code_llm_* / thinking_llm_* / small_llm_*`、`embedding_model`、`rerank_model`、`rerank_base_url`

## 依赖
按需懒导入：`langchain-openai`、`llama-index-llms-openai-like` / `-embeddings-openai-like`

## 用法示例
```python
from CompLib.llm_factory.llm_factory import ChatModelFactory, EmbeddingFactory, LlamaIndexLLMFactory
llm = ChatModelFactory("agent").build(max_tokens=4096)
emb = EmbeddingFactory().build()              # 喂 vector_memory
li_llm = LlamaIndexLLMFactory().build()        # 喂 hybrid_retrieval 的 llm=
```
