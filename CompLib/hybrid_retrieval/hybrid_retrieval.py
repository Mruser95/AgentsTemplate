from __future__ import annotations

import os
from typing import Callable, Optional

import httpx
from llama_index.core import Settings
from llama_index.core.retrievers import (
    AutoMergingRetriever,
    QueryFusionRetriever,
    VectorIndexRetriever,
)
from llama_index.core.postprocessor.types import BaseNodePostprocessor


class RerankAPI(BaseNodePostprocessor):
    """通用 rerank 后处理：调 OpenAI 兼容 /rerank 端点重排候选节点。

    endpoint / model / key 参数化，留空回退 env ``rerank_base_url`` / ``rerank_model`` / ``small_llm_key``。
    """

    top_n: int = 10
    base_url: Optional[str] = None
    model: Optional[str] = None
    api_key: Optional[str] = None

    def _postprocess_nodes(self, nodes, query_bundle=None):
        res = httpx.post(
            self.base_url or os.getenv("rerank_base_url"),
            headers={"Authorization": f"Bearer {self.api_key or os.getenv('small_llm_key')}"},
            json={
                "model": self.model or os.getenv("rerank_model"),
                "query": query_bundle.query_str,
                "documents": [n.get_content() for n in nodes],
                "top_n": self.top_n,
            },
        ).json()
        out = []
        for r in res["results"]:
            node = nodes[r["index"]]
            node.score = float(r.get("relevance_score", r.get("score", 0.0)))
            out.append(node)
        return out


class HybridRetriever:
    """通用混合检索：dense(vector+BM25) → QueryFusion(RRF + 多 query 改写) → AutoMerging → 可选 rerank。

    与索引解耦：``index_loader`` 是 ``() -> (index, storage_context)`` 回调（如 MilvusVectorIndex.load）。
    各档参数化，``reranker`` 可选；懒构建——首次 ``retrieve`` 才装配检索链。
    """

    def __init__(
        self,
        index_loader: Callable[[], tuple],
        *,
        llm=None,
        reranker: Optional[BaseNodePostprocessor] = None,
        dense_top_k: int = 30,
        dense_query_mode: str = "hybrid",
        num_queries: int = 4,
        fusion_top_k: int = 50,
        fusion_mode: str = "reciprocal_rerank",
        automerge_ratio: float = 0.5,
        pre_rerank_top_k: int = 20,
    ) -> None:
        self.index_loader = index_loader
        self.llm = llm
        self.reranker = reranker
        self.dense_top_k = dense_top_k
        self.dense_query_mode = dense_query_mode
        self.num_queries = num_queries
        self.fusion_top_k = fusion_top_k
        self.fusion_mode = fusion_mode
        self.automerge_ratio = automerge_ratio
        self.pre_rerank_top_k = pre_rerank_top_k
        self._retriever = None

    def _build(self):
        if self._retriever is None:
            index, sc = self.index_loader()
            base = VectorIndexRetriever(
                index=index, similarity_top_k=self.dense_top_k, vector_store_query_mode=self.dense_query_mode
            )
            fused = QueryFusionRetriever(
                retrievers=[base],
                llm=self.llm or Settings.llm,
                num_queries=self.num_queries,
                similarity_top_k=self.fusion_top_k,
                mode=self.fusion_mode,
                use_async=True,
                verbose=False,
            )
            self._retriever = AutoMergingRetriever(
                fused, storage_context=sc, simple_ratio_thresh=self.automerge_ratio, verbose=False
            )
        return self._retriever

    def retrieve(self, query: str):
        nodes = self._build().retrieve(query)[: self.pre_rerank_top_k]
        if self.reranker is not None:
            nodes = self.reranker.postprocess_nodes(nodes, query_str=query)
        return nodes
