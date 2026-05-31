"""
Build an eval-only dense-only Milvus collection (`law_articles_eval`).

We isolate this from the project's main `law_articles` collection because the
hybrid BM25 path is currently broken on the deployed Milvus 2.4.10 server
(llama-index-vector-stores-milvus 1.1.0 tries to create sparse index with a
metric type only supported on Milvus >=2.5). Fixing that is a server upgrade,
out of scope here. This script lets us still measure dense+rerank quality.

Run:
  python -m Knowledge.eval.build_eval_index
"""
import os, sys, logging
from pathlib import Path

os.environ["GRPC_VERBOSITY"] = "NONE"
os.environ["GLOG_minloglevel"] = "3"
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("openai").setLevel(logging.WARNING)

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

from llama_index.core import Settings, StorageContext
from llama_index.core.ingestion import DocstoreStrategy, IngestionPipeline
from llama_index.core.storage.docstore import SimpleDocumentStore
from llama_index.vector_stores.milvus import MilvusVectorStore
from llama_index.llms.openai_like import OpenAILike
from llama_index.embeddings.openai_like import OpenAILikeEmbedding

from Knowledge.cleanout import build_nodes

EVAL_DIR = Path(__file__).resolve().parent
DOC_DIR = EVAL_DIR / "doc_store"
ALL_DOC_DIR = EVAL_DIR / "all_doc_store"
COLLECTION = "law_articles_eval"


def main():
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

    DOC_DIR.mkdir(parents=True, exist_ok=True)
    ALL_DOC_DIR.mkdir(parents=True, exist_ok=True)

    vector_store = MilvusVectorStore(
        uri=os.getenv("MILVUS_URI", str(EVAL_DIR.parent / "milvus.db")),
        token=os.getenv("MILVUS_TOKEN", ""),
        collection_name=COLLECTION,
        dim=4096,
        overwrite=True,
        output_fields=["text", "doc_id"],
        index_config={"index_type": "HNSW", "M": 16, "efConstruction": 256},
        search_config={"ef": 64},
        enable_sparse=False,
    )

    docstore = SimpleDocumentStore()
    all_docstore = SimpleDocumentStore()
    sc = StorageContext.from_defaults(vector_store=vector_store, docstore=all_docstore)

    all_nodes, nodes = build_nodes()
    print(f"[build] leaf nodes={len(nodes)}  all nodes={len(all_nodes)}")

    pipeline = IngestionPipeline(
        transformations=[Settings.embed_model],
        vector_store=vector_store,
        docstore=docstore,
        docstore_strategy=DocstoreStrategy.UPSERTS,
    )
    pipeline.run(nodes=nodes, show_progress=True)
    pipeline.persist(persist_dir=str(DOC_DIR))

    all_docstore.add_documents(all_nodes)
    all_docstore.persist(persist_path=str(ALL_DOC_DIR / "docstore.json"))
    print(f"[build] done. persisted docstores under {DOC_DIR} and {ALL_DOC_DIR}")


if __name__ == "__main__":
    main()
