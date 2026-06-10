from __future__ import annotations

import asyncio
import json
import math
import sqlite3
from pathlib import Path
from typing import Optional


def dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False)


def loads_list(s) -> list:
    return json.loads(s or "[]")


def cos(a: list[float], b: list[float]) -> float:
    if not a or not b:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


class CosineRanker:
    """余弦近邻排序：对每个 query 向量取 top-k，再按 id 去重保留最高分。"""

    def rank(self, rows, query_vecs, k, *, vec_key: str = "_vec", id_key: str = "id"):
        by_id: dict = {}
        for q in query_vecs:
            scored = [(r, cos(q, r[vec_key])) for r in rows]
            scored.sort(key=lambda x: x[1], reverse=True)
            for r, s in scored[:k]:
                prev = by_id.get(r[id_key])
                if prev is None or s > prev["similarity"]:
                    rec = {kk: vv for kk, vv in r.items() if kk != vec_key}
                    rec["similarity"] = float(s)
                    by_id[r[id_key]] = rec
        return list(by_id.values())


class SqliteVectorStore:
    """sqlite 持久化后端：建表 / 插入 / 读取 / 改 / 删的原始行操作，不含嵌入与排序。

    表须含 ``id`` / ``thread_id`` / ``embedding`` 三列 + ``columns`` 指定的业务列。
    """

    def __init__(self, db_path, table: str, ddl: list[str], columns) -> None:
        self.db_path = Path(db_path)
        self.table = table
        self.ddl = list(ddl)
        self.columns = tuple(columns)

    def connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        for stmt in self.ddl:
            conn.execute(stmt)
        conn.commit()
        return conn

    def insert(self, conn, rows: list[dict], vecs, thread_id) -> list[int]:
        ids: list[int] = []
        for row, vec in zip(rows, vecs):
            cols = ["thread_id", *row.keys(), "embedding"]
            params = [thread_id, *row.values(), dumps(vec)]
            cur = conn.execute(
                f"INSERT INTO {self.table} ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                params,
            )
            ids.append(cur.lastrowid)
        conn.commit()
        return ids

    def load(self, conn, thread_id) -> list[dict]:
        cols_sql = ",".join(["id", *self.columns, "embedding"])
        if thread_id is None:
            cur = conn.execute(f"SELECT {cols_sql} FROM {self.table}")
        else:
            cur = conn.execute(f"SELECT {cols_sql} FROM {self.table} WHERE thread_id=?", (thread_id,))
        rows: list[dict] = []
        for r in cur.fetchall():
            d = dict(zip(self.columns, r[1:-1]))
            d["id"] = r[0]
            d["_vec"] = loads_list(r[-1])
            rows.append(d)
        return rows

    def update(self, conn, row_id, sets) -> int:
        sql = f"UPDATE {self.table} SET {', '.join(f'{k}=?' for k, _ in sets)} WHERE id=?"
        cur = conn.execute(sql, [v for _, v in sets] + [int(row_id)])
        conn.commit()
        return cur.rowcount

    def delete(self, conn, row_id) -> int:
        cur = conn.execute(f"DELETE FROM {self.table} WHERE id=?", (int(row_id),))
        conn.commit()
        return cur.rowcount


async def _ensure_conn(factory, conn):
    if conn is not None:
        return conn, False
    return await asyncio.to_thread(factory), True


class MemoryStore:
    """向量记忆编排：嵌入器 + sqlite 后端 + 排序器 + schema 映射；自身不含 SQL 与相似度算法。

    ``embedder`` 注入（带 ``async aembed_documents``）；``to_row`` / ``from_row`` 做 entry↔行 映射。
    search_neighbors 的 thread_id=None 跨 thread 检索，否则仅在该 thread 内。
    """

    def __init__(self, backend: SqliteVectorStore, embedder, *, embed_field: str, to_row, from_row, ranker=None) -> None:
        self.backend = backend
        self.embedder = embedder
        self.embed_field = embed_field
        self.to_row = to_row
        self.from_row = from_row
        self.ranker = ranker or CosineRanker()

    async def _conn(self, conn):
        return await _ensure_conn(self.backend.connect, conn)

    async def store(self, entry, *, thread_id: str = "", conn=None):
        single = isinstance(entry, dict)
        entries = [entry] if single else list(entry)
        conn, owned = await self._conn(conn)
        try:
            if not entries:
                return 0 if single else []
            vecs = await self.embedder.aembed_documents([e[self.embed_field] for e in entries])
            rows = [self.to_row(e) for e in entries]
            ids = await asyncio.to_thread(self.backend.insert, conn, rows, vecs, thread_id)
            return ids[0] if single else ids
        finally:
            if owned:
                await asyncio.to_thread(conn.close)

    async def search_neighbors(self, contents, *, k: int = 5, thread_id: Optional[str] = None, conn=None):
        queries = [contents] if isinstance(contents, str) else list(contents)
        if not queries:
            return []
        conn, owned = await self._conn(conn)
        try:
            vecs = await self.embedder.aembed_documents(queries)
            rows = await asyncio.to_thread(self.backend.load, conn, thread_id)
            return [self._public(r) for r in self.ranker.rank(rows, vecs, k)]
        finally:
            if owned:
                await asyncio.to_thread(conn.close)

    def _public(self, row: dict) -> dict:
        out = self.from_row(row)
        out["id"] = row["id"]
        out["similarity"] = row["similarity"]
        return out

    async def update(self, row_id: int, fields: dict, *, conn=None) -> int:
        sets = list(self.to_row(fields).items())
        new_text = fields.get(self.embed_field)
        if new_text is not None:
            vec = (await self.embedder.aembed_documents([new_text]))[0]
            sets.append(("embedding", dumps(vec)))
        if not sets:
            return 0
        conn, owned = await self._conn(conn)
        try:
            return await asyncio.to_thread(self.backend.update, conn, row_id, sets)
        finally:
            if owned:
                await asyncio.to_thread(conn.close)

    async def delete(self, row_id: int, *, conn=None) -> int:
        conn, owned = await self._conn(conn)
        try:
            return await asyncio.to_thread(self.backend.delete, conn, row_id)
        finally:
            if owned:
                await asyncio.to_thread(conn.close)
