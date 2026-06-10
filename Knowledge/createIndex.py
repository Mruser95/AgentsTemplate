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
from llama_index.core.storage.docstore import SimpleDocumentStore
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
all_doc_dir = Path(__file__).resolve().parent / "all_doc_store"

def get_index():
    has_docstore = (doc_dir / "docstore.json").exists() and (all_doc_dir / "docstore.json").exists()
    vector_store = MilvusVectorStore(
        uri=os.getenv("MILVUS_URI", str(Path(__file__).resolve().parent / "milvus.db")),
        token=os.getenv("MILVUS_TOKEN", ""),
        collection_name="law_articles",
        dim=4096,
        overwrite=not has_docstore,
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
    
    if has_docstore:
        try:
            vector_store.client.load_collection("law_articles")
            docstore = SimpleDocumentStore.from_persist_dir(str(doc_dir))
            all_docstore = SimpleDocumentStore.from_persist_dir(str(all_doc_dir))
        except Exception as e:
            logging.warning("load_collection skipped: %s", e)
            docstore, all_docstore = SimpleDocumentStore(), SimpleDocumentStore()
    else:
        docstore, all_docstore = SimpleDocumentStore(), SimpleDocumentStore()
    sc = StorageContext.from_defaults(vector_store=vector_store, docstore=all_docstore)

    all_nodes, nodes = build_nodes()
    new_ids = {n.node_id for n in nodes}
    for orphan in set(docstore.get_all_document_hashes().values()) - new_ids:
        docstore.delete_ref_doc(orphan, raise_error=False)
        vector_store.delete(orphan)
    pipeline = IngestionPipeline(
        transformations=[Settings.embed_model],
        vector_store=vector_store,
        docstore=docstore,
        docstore_strategy=DocstoreStrategy.UPSERTS,
    )
    pipeline.run(nodes=nodes, show_progress=True)
    pipeline.persist(persist_dir=str(doc_dir))
    all_new = {n.node_id for n in all_nodes}
    for orphan in set(all_docstore.get_all_document_hashes().values()) - all_new:
        all_docstore.delete_document(orphan, raise_error=False)
    for r in {n.ref_doc_id for n in all_nodes if n.ref_doc_id}:
        all_docstore._kvstore.delete(r, collection=all_docstore._ref_doc_collection)
    all_docstore.add_documents(all_nodes)
    all_docstore.persist(persist_path=str(all_doc_dir / "docstore.json"))
    index = VectorStoreIndex.from_vector_store(vector_store=vector_store, storage_context=sc)
    return index, sc




if __name__ == "__main__":
    index, storage_context = get_index()
    print(len(Settings.embed_model.get_text_embedding("test")))
    ques = "香港杀人按香港法处理还是内地法？同时告知回答参考了哪些法律"
    response = index.as_query_engine(vector_store_query_mode="hybrid",similarity_top_k=5).query(ques)
    print(response)
