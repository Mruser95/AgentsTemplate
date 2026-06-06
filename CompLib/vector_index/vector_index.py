from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional, Sequence

from llama_index.core import VectorStoreIndex, StorageContext
from llama_index.core.ingestion import DocstoreStrategy, IngestionPipeline
from llama_index.core.storage.docstore import SimpleDocumentStore
from llama_index.vector_stores.milvus import MilvusVectorStore
from llama_index.vector_stores.milvus.utils import BM25BuiltInFunction


class MilvusStore:
    """懒构造 / 持有 MilvusVectorStore（dense HNSW + 可选 BM25 稀疏）。首次 ``get()`` 才连库。"""

    def __init__(
        self,
        *,
        collection_name: str,
        dim: int = 4096,
        uri: Optional[str] = None,
        token: str = "",
        enable_sparse: bool = True,
        sparse_tokenizer: str = "jieba",
        hnsw_m: int = 16,
        hnsw_ef_construction: int = 256,
        search_ef: int = 64,
        output_fields: Sequence[str] = ("text", "doc_id"),
    ) -> None:
        self.collection_name = collection_name
        self.dim = dim
        self.uri = str(uri) if uri else None
        self.token = token
        self.enable_sparse = enable_sparse
        self.sparse_tokenizer = sparse_tokenizer
        self.hnsw_m = hnsw_m
        self.hnsw_ef_construction = hnsw_ef_construction
        self.search_ef = search_ef
        self.output_fields = list(output_fields)
        self._store: Optional[MilvusVectorStore] = None

    def get(self, *, overwrite: bool = False) -> MilvusVectorStore:
        if self._store is None:
            kwargs = dict(
                uri=self.uri or str(Path.cwd() / "milvus.db"),
                token=self.token,
                collection_name=self.collection_name,
                dim=self.dim,
                overwrite=overwrite,
                output_fields=self.output_fields,
                index_config={"index_type": "HNSW", "M": self.hnsw_m, "efConstruction": self.hnsw_ef_construction},
                search_config={"ef": self.search_ef},
            )
            if self.enable_sparse:
                kwargs["enable_sparse"] = True
                kwargs["sparse_embedding_function"] = BM25BuiltInFunction(
                    analyzer_params={"tokenizer": self.sparse_tokenizer}
                )
            self._store = MilvusVectorStore(**kwargs)
        return self._store


class DocStoreManager:
    """管理两套 SimpleDocumentStore（leaf 入库用 + all 供 AutoMerging 用）：加载与持久化。"""

    def __init__(self, doc_dir, all_doc_dir) -> None:
        self.doc_dir = Path(doc_dir)
        self.all_doc_dir = Path(all_doc_dir)

    def exists(self) -> bool:
        return (self.doc_dir / "docstore.json").exists() and (self.all_doc_dir / "docstore.json").exists()

    def load(self, vector_store=None, collection_name: Optional[str] = None):
        if self.exists():
            try:
                if vector_store is not None and collection_name:
                    vector_store.client.load_collection(collection_name)
                return (
                    SimpleDocumentStore.from_persist_dir(str(self.doc_dir)),
                    SimpleDocumentStore.from_persist_dir(str(self.all_doc_dir)),
                )
            except Exception as e:  # noqa: BLE001
                logging.warning("load_collection skipped: %s", e)
        return SimpleDocumentStore(), SimpleDocumentStore()

    def persist_leaf(self, pipeline) -> None:
        self.doc_dir.mkdir(parents=True, exist_ok=True)
        pipeline.persist(persist_dir=str(self.doc_dir))

    def persist_all(self, all_docstore) -> None:
        self.all_doc_dir.mkdir(parents=True, exist_ok=True)
        all_docstore.persist(persist_path=str(self.all_doc_dir / "docstore.json"))


class MilvusVectorIndex:
    """编排：组合 MilvusStore + DocStoreManager 完成 ingest / load。

    懒连接、与切分解耦——``ingest`` 接收外部 nodes（如 StructuredDocChunker.run 的产出），
    ``load`` 是纯查询期加载。
    """

    def __init__(self, store: MilvusStore, docstores: DocStoreManager, embed_model) -> None:
        self.store = store
        self.docstores = docstores
        self.embed_model = embed_model

    def _vs(self):
        return self.store.get(overwrite=not self.docstores.exists())

    def _index(self, sc: StorageContext) -> VectorStoreIndex:
        return VectorStoreIndex.from_vector_store(
            vector_store=self.store.get(), storage_context=sc, embed_model=self.embed_model
        )

    def ingest(self, leaf_nodes, all_nodes):
        """增量 upsert 入库 + 持久化双 docstore；返回 ``(index, storage_context)``。"""
        vs = self._vs()
        leaf_docstore, all_docstore = self.docstores.load(vs, self.store.collection_name)
        sc = StorageContext.from_defaults(vector_store=vs, docstore=all_docstore)
        new_ids = {n.node_id for n in leaf_nodes}
        for orphan in set(leaf_docstore.get_all_document_hashes().values()) - new_ids:
            leaf_docstore.delete_ref_doc(orphan, raise_error=False)
            vs.delete(orphan)
        pipeline = IngestionPipeline(
            transformations=[self.embed_model],
            vector_store=vs,
            docstore=leaf_docstore,
            docstore_strategy=DocstoreStrategy.UPSERTS,
        )
        pipeline.run(nodes=leaf_nodes, show_progress=True)
        self.docstores.persist_leaf(pipeline)
        all_new = {n.node_id for n in all_nodes}
        for orphan in set(all_docstore.get_all_document_hashes().values()) - all_new:
            all_docstore.delete_document(orphan, raise_error=False)
        for r in {n.ref_doc_id for n in all_nodes if n.ref_doc_id}:
            all_docstore._kvstore.delete(r, collection=all_docstore._ref_doc_collection)
        all_docstore.add_documents(all_nodes)
        self.docstores.persist_all(all_docstore)
        return self._index(sc), sc

    def load(self):
        vs = self._vs()
        _, all_docstore = self.docstores.load(vs, self.store.collection_name)
        sc = StorageContext.from_defaults(vector_store=vs, docstore=all_docstore)
        return self._index(sc), sc
