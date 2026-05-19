import os, httpx
from llama_index.core import Settings
from llama_index.core.retrievers import VectorIndexRetriever, QueryFusionRetriever
from llama_index.core.retrievers import AutoMergingRetriever
from llama_index.core.vector_stores import MetadataFilters, MetadataFilter
from llama_index.core.postprocessor.types import BaseNodePostprocessor
from pathlib import Path

import sys
sys.path.append(str(Path(__file__).resolve().parent.parent))
from Knowledge.createIndex import get_index # noqa: E402


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
reranker = RerankAPI()


def retrieve(query: str):
    nodes = auto_merging_retriever.retrieve(query)
    nodes = nodes[:20]                                  
    nodes = reranker.postprocess_nodes(nodes, query_str=query)   
    return nodes


if __name__ == "__main__":
    ques = "香港"
    for n in retrieve(ques):
        print(f"[{n.score:.4f}] {n.metadata.get('article', '')}")
        print(n.text[:200])
        print("-" * 80)

