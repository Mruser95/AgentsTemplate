---
name: hybrid_retrieval
description: 通用混合检索组件：dense(vector+BM25) → QueryFusion(RRF 多 query 改写) → AutoMerging → 可选 rerank。与索引解耦（接收 index_loader 回调），含通用 RerankAPI。一个完整功能：对已建索引做高召回检索。
---

# hybrid_retrieval — HybridRetriever / RerankAPI

实现文件：`CompLib/hybrid_retrieval/hybrid_retrieval.py`（单文件）

## 用途
对一个已建好的向量索引做高召回检索：多路 query 改写 + RRF 融合 + 把碎片叶子合并回大块上下文，
末尾可选远端 rerank。不绑定具体索引/领域，配合 `vector_index` 使用。

## 接口
`from CompLib.hybrid_retrieval.hybrid_retrieval import HybridRetriever, RerankAPI`

- `RerankAPI(top_n=10, base_url=None, model=None, api_key=None)`：OpenAI 兼容 /rerank 后处理（env 兜底 `rerank_base_url`/`rerank_model`/`small_llm_key`）
- `HybridRetriever(index_loader, *, llm=None, reranker=None, dense_top_k=30, dense_query_mode="hybrid", num_queries=4, fusion_top_k=50, automerge_ratio=0.5, pre_rerank_top_k=20)`
  - `index_loader`：`() -> (index, storage_context)`，如 `MilvusVectorIndex(...).load`
  - `llm`：query 改写用 LLM，None=回退 `Settings.llm`
  - `retrieve(query) -> list[NodeWithScore]`（懒装配，首次调用才建链）

## 依赖
`llama-index-core`（VectorIndexRetriever / QueryFusionRetriever / AutoMergingRetriever）、`httpx`

## 用法示例
```python
from CompLib.vector_index.vector_index import MilvusVectorIndex
from CompLib.hybrid_retrieval.hybrid_retrieval import HybridRetriever, RerankAPI
from CompLib.llm_factory.llm_factory import LlamaIndexLLMFactory
idx = MilvusVectorIndex(...)
retr = HybridRetriever(idx.load, llm=LlamaIndexLLMFactory().build(), reranker=RerankAPI(top_n=10))
for n in retr.retrieve("查询语句"):
    print(n.score, n.get_content()[:80])
```
