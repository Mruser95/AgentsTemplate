"""
Retrieval evaluation for the legal RAG pipeline.

Bypasses the re-ingestion in createIndex.get_index() (which currently breaks
due to a missing sparse_embedding field on upsert) and loads the EXISTING
Milvus collection + persisted docstores directly. The retrieval stack
(VectorIndexRetriever -> QueryFusionRetriever -> AutoMergingRetriever -> rerank)
mirrors Knowledge/retriever.py exactly.

Metrics reported per K (default K in [1,3,5,10,20]):
  Hit@K      : fraction of queries with >=1 gold article in TopK
  Recall@K   : avg |TopK_articles n gold| / |gold|
  MRR@K      : reciprocal rank of the first gold article (0 if not found)

Run:
  python -m Knowledge.eval.eval                  # default queries.json, full stack
  python -m Knowledge.eval.eval --no-rerank      # ablation: skip rerank
  python -m Knowledge.eval.eval --no-fusion      # ablation: pure dense+sparse, no RRF
"""
import os, sys, json, argparse, time, logging
from pathlib import Path

os.environ["GRPC_VERBOSITY"] = "NONE"
os.environ["GLOG_minloglevel"] = "3"
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("openai").setLevel(logging.WARNING)

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

from llama_index.core import VectorStoreIndex, Settings, StorageContext
from llama_index.core.retrievers import VectorIndexRetriever, QueryFusionRetriever
from llama_index.core.retrievers import AutoMergingRetriever
from llama_index.core.storage.docstore import SimpleDocumentStore
from llama_index.vector_stores.milvus import MilvusVectorStore
from llama_index.llms.openai_like import OpenAILike
from llama_index.embeddings.openai_like import OpenAILikeEmbedding
from llama_index.core.postprocessor.types import BaseNodePostprocessor
import httpx


class RerankAPI(BaseNodePostprocessor):
    """Inlined copy of Knowledge/retriever.py::RerankAPI (avoid importing that
    module because it pulls in Knowledge.createIndex which crashes on this
    Milvus version)."""
    top_n: int = 10

    def _postprocess_nodes(self, nodes, query_bundle=None):
        if not nodes:
            return nodes
        res = httpx.post(
            os.getenv("rerank_base_url"),
            headers={"Authorization": f"Bearer {os.getenv('small_llm_key')}"},
            json={
                "model": os.getenv("rerank_model"),
                "query": query_bundle.query_str,
                "documents": [n.get_content() for n in nodes],
                "top_n": self.top_n,
            },
            timeout=60.0,
        ).json()
        out = []
        for r in res["results"]:
            node = nodes[r["index"]]
            node.score = float(r.get("relevance_score", r.get("score", 0.0)))
            out.append(node)
        return out

KNOWLEDGE_DIR = ROOT / "Knowledge"
EVAL_DIR = KNOWLEDGE_DIR / "eval"
DOC_DIR = EVAL_DIR / "doc_store"
ALL_DOC_DIR = EVAL_DIR / "all_doc_store"
COLLECTION = "law_articles_eval"


def _setup_settings():
    Settings.llm = OpenAILike(
        model=os.getenv("small_llm_model"),
        api_base=os.getenv("small_llm_base_url"),
        api_key=os.getenv("small_llm_key"),
        is_chat_model=True,
        context_window=32768,
    )
    Settings.embed_model = OpenAILikeEmbedding(
        model_name=os.getenv("embedding_model"),
        api_base=os.getenv("small_llm_base_url"),
        api_key=os.getenv("small_llm_key"),
        embed_batch_size=128,
    )


def _open_existing_index():
    """Open the persisted Milvus collection + docstores WITHOUT re-ingesting."""
    vector_store = MilvusVectorStore(
        uri=os.getenv("MILVUS_URI", str(KNOWLEDGE_DIR / "milvus.db")),
        token=os.getenv("MILVUS_TOKEN", ""),
        collection_name=COLLECTION,
        dim=4096,
        overwrite=False,
        output_fields=["text", "doc_id", "file_name", "article", "part", "chapter", "section"],
        index_config={"index_type": "HNSW", "M": 16, "efConstruction": 256},
        search_config={"ef": 64},
        enable_sparse=False,
    )
    vector_store.client.load_collection(COLLECTION)
    all_docstore = SimpleDocumentStore.from_persist_dir(str(ALL_DOC_DIR))
    sc = StorageContext.from_defaults(vector_store=vector_store, docstore=all_docstore)
    index = VectorStoreIndex.from_vector_store(vector_store=vector_store, storage_context=sc)
    return index, sc


def build_retrievers(use_fusion=True, use_rerank=True, rerank_top_n=10):
    index, sc = _open_existing_index()
    base = VectorIndexRetriever(
        index=index,
        similarity_top_k=30,
        vector_store_query_mode="default",
    )
    if use_fusion:
        retriever = QueryFusionRetriever(
            retrievers=[base],
            llm=Settings.llm,
            num_queries=4,
            similarity_top_k=50,
            mode="reciprocal_rerank",
            use_async=True,
            verbose=False,
        )
        retriever = AutoMergingRetriever(retriever, storage_context=sc, simple_ratio_thresh=0.5)
    else:
        retriever = base
    reranker = RerankAPI(top_n=rerank_top_n) if use_rerank else None
    return retriever, reranker


def retrieve_one(retriever, reranker, query: str, top_n_pre_rerank=20):
    nodes = retriever.retrieve(query)
    nodes = nodes[:top_n_pre_rerank]
    if reranker is not None:
        nodes = reranker.postprocess_nodes(nodes, query_str=query)
    return nodes


def _node_key(node):
    md = node.metadata or {}
    return md.get("file_name", ""), md.get("article", "")


def _gold_match(gold_pair, node_key):
    g_file, g_art = gold_pair
    n_file, n_art = node_key
    return (g_file in n_file) and (g_art == n_art)


def evaluate(queries, retriever, reranker, ks=(1, 3, 5, 10, 20), max_retries=2):
    """For each query, retrieve, dedup nodes -> ranked article list, compute metrics per K.
    Returns: per-query records + aggregate metrics dict.
    Per-query failures (e.g. transient LLM rewriter SSL errors) are retried then
    skipped so a single bad request doesn't abort the whole run.
    """
    records = []
    max_k = max(ks)
    skipped = []
    for q in queries:
        t0 = time.time()
        nodes = None
        for attempt in range(max_retries + 1):
            try:
                nodes = retrieve_one(retriever, reranker, q["query"], top_n_pre_rerank=max(20, max_k))
                break
            except Exception as e:
                if attempt == max_retries:
                    print(f"  [skip] id={q['id']} after {attempt+1} attempts: {e!r}")
                    skipped.append({"id": q["id"], "error": repr(e)})
                    break
                time.sleep(1.5 * (attempt + 1))
        if nodes is None:
            continue
        dt = time.time() - t0

        # dedup -> ranked article keys (preserving order)
        seen = set()
        ranked_keys = []
        for n in nodes:
            k = _node_key(n)
            if k in seen:
                continue
            seen.add(k)
            ranked_keys.append(k)

        gold = [tuple(g) for g in q["gold"]]
        # rank of first gold match in ranked_keys (1-indexed), or None
        first_rank = None
        for i, k in enumerate(ranked_keys, 1):
            if any(_gold_match(g, k) for g in gold):
                first_rank = i
                break

        metrics = {}
        for K in ks:
            topk = ranked_keys[:K]
            hit = any(any(_gold_match(g, k) for k in topk) for g in gold)
            matched = sum(1 for g in gold if any(_gold_match(g, k) for k in topk))
            recall = matched / len(gold) if gold else 0.0
            mrr = (1.0 / first_rank) if (first_rank is not None and first_rank <= K) else 0.0
            metrics[K] = {"hit": int(hit), "recall": recall, "mrr": mrr}

        records.append({
            "id": q["id"],
            "type": q.get("type", ""),
            "query": q["query"],
            "gold": gold,
            "ranked_topN": ranked_keys[:max_k],
            "first_gold_rank": first_rank,
            "latency_s": round(dt, 3),
            "per_k": metrics,
        })

    agg = {}
    n = max(len(records), 1)
    for K in ks:
        agg[K] = {
            "Hit@K": sum(r["per_k"][K]["hit"] for r in records) / n,
            "Recall@K": sum(r["per_k"][K]["recall"] for r in records) / n,
            "MRR@K": sum(r["per_k"][K]["mrr"] for r in records) / n,
        }
    return records, agg, skipped


def _print_table(agg, label):
    print(f"\n=== {label} ===")
    print(f"{'K':>4} {'Hit@K':>10} {'Recall@K':>10} {'MRR@K':>10}")
    for K, m in agg.items():
        print(f"{K:>4} {m['Hit@K']*100:>9.2f}% {m['Recall@K']*100:>9.2f}% {m['MRR@K']:>10.4f}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--queries", default=str(EVAL_DIR / "queries.json"))
    ap.add_argument("--out", default=str(EVAL_DIR / "results.json"))
    ap.add_argument("--no-fusion", action="store_true", help="skip QueryFusion+AutoMerging")
    ap.add_argument("--no-rerank", action="store_true", help="skip rerank stage")
    ap.add_argument("--label", default="full")
    args = ap.parse_args()

    _setup_settings()
    queries = json.loads(Path(args.queries).read_text(encoding="utf-8"))

    retriever, reranker = build_retrievers(
        use_fusion=not args.no_fusion,
        use_rerank=not args.no_rerank,
    )
    label_bits = []
    if args.no_fusion:
        label_bits.append("no-fusion")
    if args.no_rerank:
        label_bits.append("no-rerank")
    label = args.label if not label_bits else "+".join(label_bits)

    print(f"[eval] running {len(queries)} queries  label={label}")
    records, agg, skipped = evaluate(queries, retriever, reranker)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "label": label,
        "n_queries": len(queries),
        "n_scored": len(records),
        "n_skipped": len(skipped),
        "skipped": skipped,
        "aggregate": agg,
        "records": records,
    }
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    _print_table(agg, label)
    print(f"\n[eval] wrote {out_path}")


if __name__ == "__main__":
    main()
