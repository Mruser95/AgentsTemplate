---
name: vector_memory
description: 通用 sqlite 向量记忆组件：任意 schema 的存储 + 余弦近邻召回，支持按 thread 隔离或全局检索；嵌入器外部注入，无 env 绑定。一个完整功能：给 agent 加可检索记忆（长/短期、经验库）。
---

# vector_memory — MemoryStore

实现文件：`CompLib/vector_memory/vector_memory.py`（单文件，内含下列协作类）

## 用途
把任意结构的 entry 向量化存进 sqlite，按余弦相似度召回。schema 由参数描述，嵌入器注入，不绑定领域/env。
长期记忆=全局检索；短期记忆=按 thread_id 隔离。

## 接口
`from CompLib.vector_memory.vector_memory import MemoryStore, SqliteVectorStore`

- `SqliteVectorStore(db_path, table, ddl, columns)`：原始行存储 `connect/insert/load/update/delete`（表须含 id/thread_id/embedding + 业务列）
- `CosineRanker().rank(rows, query_vecs, k)`：余弦去重排序（默认排序器）
- `MemoryStore(backend, embedder, *, embed_field, to_row, from_row, ranker=None)`：编排
  - `embedder`：带 `async aembed_documents`；`to_row(entry)->{col:val}` / `from_row({col:val})->entry`
  - `await store(entry|[entry], *, thread_id="") -> id|[id]`
  - `await search_neighbors(query|[query], *, k=5, thread_id=None) -> [entry+id+similarity]`（None 跨线程）
  - `await update(row_id, fields) -> n` / `await delete(row_id) -> n`

## 依赖
标准库（sqlite3/json/math/asyncio）+ 注入的 embedder（如 llm_factory.EmbeddingFactory）

## 用法示例
```python
from CompLib.vector_memory.vector_memory import MemoryStore, SqliteVectorStore
from CompLib.llm_factory.llm_factory import EmbeddingFactory
ddl = ["CREATE TABLE IF NOT EXISTS mem(id INTEGER PRIMARY KEY AUTOINCREMENT, thread_id TEXT, content TEXT, embedding TEXT)"]
backend = SqliteVectorStore("mem.db", "mem", ddl, ("content",))
mem = MemoryStore(backend, EmbeddingFactory().build(), embed_field="content",
                  to_row=lambda e: {"content": e["content"]}, from_row=lambda r: {"content": r["content"]})
await mem.store({"content": "用户偏好 Python"}, thread_id="u1")
hits = await mem.search_neighbors("编程语言", k=3, thread_id="u1")
```
