import os
os.environ["GRPC_VERBOSITY"] = "NONE"
os.environ["GLOG_minloglevel"] = "3"
import logging
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("openai").setLevel(logging.WARNING)
from dotenv import load_dotenv
load_dotenv()
from llama_index.core import VectorStoreIndex, Settings, StorageContext
from llama_index.core.ingestion import DocstoreStrategy, IngestionPipeline
from llama_index.vector_stores.milvus import MilvusVectorStore
from llama_index.vector_stores.milvus.utils import BM25BuiltInFunction
from llama_index.llms.openai_like import OpenAILike
from llama_index.embeddings.openai_like import OpenAILikeEmbedding
from pathlib import Path

import sys
sys.path.append(str(Path(__file__).resolve().parent.parent))
from Knowledge.cleanout import build_nodes # noqa: E402

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

doc_dir = Path(__file__).resolve().parent / "doc_store"

vector_store = MilvusVectorStore(
    uri=str(Path(__file__).resolve().parent / "milvus.db"),
    collection_name="law_articles",
    dim=4096,
    overwrite=not (doc_dir / "docstore.json").exists(),
    output_fields=["text", "doc_id"],
    index_config={
        "index_type": "HNSW", 
        "M": 16, "efConstruction": 256
    }, 
    search_config={"ef": 64},
    enable_sparse=True,
    sparse_embedding_function=BM25BuiltInFunction(
        analyzer_params={"tokenizer": "jieba"}
    ),
)

def get_index():
    has_docstore = (doc_dir / "docstore.json").exists()
    sc_extra = {"persist_dir": str(doc_dir)} if has_docstore else {}
    sc = StorageContext.from_defaults(vector_store=vector_store, **sc_extra)
    if has_docstore:
        vector_store.client.load_collection("law_articles")
    _, nodes = build_nodes()
    pipeline = IngestionPipeline(
        transformations=[Settings.embed_model],
        vector_store=vector_store,
        docstore=sc.docstore,
        docstore_strategy=DocstoreStrategy.UPSERTS_AND_DELETE,
    )
    pipeline.run(nodes=nodes, show_progress=True)
    pipeline.persist(persist_dir=str(doc_dir))
    index = VectorStoreIndex.from_vector_store(vector_store=vector_store, storage_context=sc)
    return index, sc




if __name__ == "__main__":
    index, storage_context = get_index()
    print(len(Settings.embed_model.get_text_embedding("test")))
    ques = "未成年枪击国家领导人怎么处理？"
    response = index.as_query_engine(vector_store_query_mode="hybrid",similarity_top_k=5).query(ques)
    print(response)
