---
name: vector_index
description: 通用 Milvus 向量索引组件：dense(HNSW)+可选 BM25 稀疏+双 docstore(支持 AutoMerging)，UPSERTS 增量入库；懒连接、与切分解耦（ingest 收外部 nodes）。一个完整功能：把节点入库并供查询期加载。
---

# vector_index — MilvusVectorIndex

实现文件：`CompLib/vector_index/vector_index.py`（单文件，内含下列协作类）

## 用途
把切分好的 nodes 入 Milvus（dense + 可选 BM25），并持久化双 docstore 供 AutoMerging。
collection/dim/路径/稀疏开关全部参数化，无领域绑定。配合 `doc_chunking` 产 nodes、`hybrid_retrieval` 查询。

## 接口
`from CompLib.vector_index.vector_index import MilvusVectorIndex, MilvusStore, DocStoreManager`

- `MilvusStore(*, collection_name, dim=4096, uri=None, token="", enable_sparse=True, sparse_tokenizer="jieba", hnsw_m=16, hnsw_ef_construction=256, search_ef=64, output_fields=("text","doc_id"))`：`get(*, overwrite=False)` 懒构造向量库
- `DocStoreManager(doc_dir, all_doc_dir)`：`exists()` / `load()` / `persist_leaf()` / `persist_all()`
- `MilvusVectorIndex(store, docstores, embed_model)`：
  - `ingest(leaf_nodes, all_nodes) -> (index, storage_context)`：增量 upsert + 持久化
  - `load() -> (index, storage_context)`：纯查询期加载（传给 HybridRetriever 的 index_loader）

## 依赖
`llama-index-core`、`llama-index-vector-stores-milvus`（MilvusVectorStore / BM25BuiltInFunction）

## 用法示例
```python
from CompLib.vector_index.vector_index import MilvusVectorIndex, MilvusStore, DocStoreManager
from CompLib.llm_factory.llm_factory import EmbeddingFactory
emb = EmbeddingFactory(framework="llama_index").build()
idx = MilvusVectorIndex(MilvusStore(collection_name="kb"), DocStoreManager("doc_store", "all_doc_store"), emb)
idx.ingest(leaves, all_nodes)        # 写入；查询期 index, sc = idx.load()
```
