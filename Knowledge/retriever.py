import os, httpx
from llama_index.core import Settings
from llama_index.core.retrievers import VectorIndexRetriever, QueryFusionRetriever
from llama_index.core.retrievers import AutoMergingRetriever
from llama_index.core.vector_stores import MetadataFilters, MetadataFilter
from llama_index.core.postprocessor.types import BaseNodePostprocessor
from pathlib import Path
from typing import Type
from pydantic import BaseModel, Field, PrivateAttr
from langchain_core.tools import BaseTool

import sys
sys.path.append(str(Path(__file__).resolve().parent.parent))
from Tools.utils import bump_budget, current_thread_id  # noqa: E402


class RerankAPI(BaseNodePostprocessor):
    top_n: int = 10
    def _postprocess_nodes(self, nodes, query_bundle=None):
        res = httpx.post(
            os.getenv("rerank_base_url"),
            headers={"Authorization": f"Bearer {os.getenv('small_llm_key')}"},
            json={"model": os.getenv("rerank_model"), "query": query_bundle.query_str,
                  "documents": [n.get_content() for n in nodes], "top_n": self.top_n}
        ).json()
        out = []
        for r in res["results"]:
            node = nodes[r["index"]]
            node.score = float(r.get("relevance_score", r.get("score", 0.0)))
            out.append(node)
        return out


reranker = RerankAPI()
auto_merging_retriever = None


def _get_retriever():
    global auto_merging_retriever
    if auto_merging_retriever is not None:
        return auto_merging_retriever

    from Knowledge.createIndex import get_index
    index, sc = get_index()
    base_retriever = VectorIndexRetriever(
        index=index,
        similarity_top_k=30,
        vector_store_query_mode="hybrid",
    )
    hybrid_retriever = QueryFusionRetriever(
        retrievers=[base_retriever],
        llm=Settings.llm,
        num_queries=4,                  # 原 query + 3 条改写 = 4 路
        similarity_top_k=50,
        mode="reciprocal_rerank",       # RRF
        use_async=True,
        verbose=False,
    )
    auto_merging_retriever = AutoMergingRetriever(
        hybrid_retriever,
        storage_context=sc,
        simple_ratio_thresh=0.5,
        verbose=False,
    )
    return auto_merging_retriever


def retrieve(query: str):
    nodes = _get_retriever().retrieve(query)
    nodes = nodes[:20]                                  
    nodes = reranker.postprocess_nodes(nodes, query_str=query)   
    return nodes


class KnowledgeInput(BaseModel):
    query: str = Field(description="自然语言检索 query")


class KnowledgeSearch(BaseTool):
    name: str = "knowledge_search"
    description: str = "本地 Milvus 知识库混合检索（vector + BM25 + rerank）。"
    args_schema: Type[BaseModel] = KnowledgeInput
    max_tool_calls: int = 20
    _call_counts: dict[str, int] = PrivateAttr(default_factory=dict)

    def reset(self) -> None:
        self._call_counts.clear()

    def _run(self, query: str) -> str:
        tid = current_thread_id()
        ok, n, rem = bump_budget(self._call_counts, tid, self.max_tool_calls)
        if not ok:
            return f"Tool call limit reached ({self.max_tool_calls})."
        try:
            nodes = retrieve(query)
        except Exception as e:
            return f"knowledge_search failed: {e!r}"
        if not nodes:
            return f"No results.\n\n[Tool call {n}/{self.max_tool_calls}, remaining: {rem}]"
        parts = [f"[{i}] score={(nd.score or 0):.4f}\n{nd.get_content()[:500]}"
                 for i, nd in enumerate(nodes, 1)]
        return "\n\n".join(parts) + f"\n\n[Tool call {n}/{self.max_tool_calls}, remaining: {rem}]"

    async def _arun(self, query: str) -> str:
        return self._run(query)


if __name__ == "__main__":
    ques = "香港"
    for n in retrieve(ques):
        print(f"[{n.score:.4f}] {n.metadata.get('article', '')}")
        print(n.text[:200])
        print("-" * 80)

