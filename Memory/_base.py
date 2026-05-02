import asyncio
import json
import math
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional, Union, overload
from dotenv import load_dotenv
from langchain_openai import OpenAIEmbeddings

load_dotenv(Path(__file__).resolve().parent.parent / ".env")


embeddings = OpenAIEmbeddings(
    model=os.getenv("embedding_model", "text-embedding-3-small"),
    api_key=os.getenv("small_llm_key"),
    base_url=os.getenv("small_llm_base_url"),
)


def cos(a: list[float], b: list[float]) -> float:
    if not a or not b:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


# sqlite =======================================================================


def open_db(db_path: str | Path, ddl: list[str]) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    for stmt in ddl:
        conn.execute(stmt)
    conn.commit()
    return conn


async def ensure_conn(
    factory: Callable[[], sqlite3.Connection], conn: Optional[sqlite3.Connection],
) -> tuple[sqlite3.Connection, bool]:
    if conn is not None:
        return conn, False
    new_conn = await asyncio.to_thread(factory)
    return new_conn, True


def rank_rows(
    rows: list[dict], query_vecs: list[list[float]], k: int, *, vec_key: str = "_vec", id_key: str = "id",
) -> list[dict]:
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


def loads_list(s: Optional[str]) -> list:
    return json.loads(s or "[]")


def dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False)


# generic vector memory store ==================================================


@dataclass
class MemoryStore:
    db_path: Path
    table: str
    ddl: list[str]
    columns: tuple[str, ...]                 # 持久化列名（不含 id / thread_id / embedding）
    embed_field: str                         # entry 中用于生成向量的字段名
    to_row: Callable[[dict], dict[str, Any]] # entry -> {col: sql_value}; 跳过 None 字段
    from_row: Callable[[dict], dict]         # {col: sql_value} -> 公开 entry dict

    def init_db(self) -> sqlite3.Connection:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        return open_db(self.db_path, self.ddl)

    async def _conn(self, conn):
        return await ensure_conn(self.init_db, conn)

    def _insert(self, conn, entries, vecs, thread_id):
        ids: list[int] = []
        for e, vec in zip(entries, vecs):
            row = self.to_row(e)
            cols = ["thread_id", *row.keys(), "embedding"]
            params = [thread_id, *row.values(), dumps(vec)]
            cur = conn.execute(
                f"INSERT INTO {self.table} ({','.join(cols)}) "
                f"VALUES ({','.join('?' * len(cols))})",
                params,
            )
            ids.append(cur.lastrowid)
        conn.commit()
        return ids

    @overload
    async def store(self, entry: dict, *, thread_id: str = "",
                    conn: Optional[sqlite3.Connection] = None) -> int: ...
    @overload
    async def store(self, entry: list[dict], *, thread_id: str = "",
                    conn: Optional[sqlite3.Connection] = None) -> list[int]: ...

    async def store(self, entry, *, thread_id: str = "", conn=None):
        single = isinstance(entry, dict)
        entries: list[dict] = [entry] if single else list(entry)
        conn, owned = await self._conn(conn)
        try:
            if not entries:
                return 0 if single else []
            vecs = await embeddings.aembed_documents([e[self.embed_field] for e in entries])
            ids = await asyncio.to_thread(self._insert, conn, entries, vecs, thread_id)
            return ids[0] if single else ids
        finally:
            if owned:
                await asyncio.to_thread(conn.close)

    def _load(self, conn, thread_id):
        cols_sql = ",".join(["id", *self.columns, "embedding"])
        if thread_id is None:
            cur = conn.execute(f"SELECT {cols_sql} FROM {self.table}")
        else:
            cur = conn.execute(
                f"SELECT {cols_sql} FROM {self.table} WHERE thread_id=?", (thread_id,),
            )
        rows: list[dict] = []
        for r in cur.fetchall():
            d = self.from_row(dict(zip(self.columns, r[1:-1])))
            d["id"] = r[0]
            d["_vec"] = loads_list(r[-1])
            rows.append(d)
        return rows

    async def search_neighbors(
        self, contents: Union[str, list[str]], *, k: int = 5,
        thread_id: Optional[str] = None, conn=None,
    ) -> list[dict]:
        """召回最近邻；thread_id=None 时跨 thread 检索，否则仅在该 thread 里检索。"""
        queries = [contents] if isinstance(contents, str) else list(contents)
        if not queries:
            return []
        conn, owned = await self._conn(conn)
        try:
            vecs = await embeddings.aembed_documents(queries)
            rows = await asyncio.to_thread(self._load, conn, thread_id)
            return rank_rows(rows, vecs, k)
        finally:
            if owned:
                await asyncio.to_thread(conn.close)

    def _update(self, conn, row_id, sets):
        sql = f"UPDATE {self.table} SET {', '.join(f'{k}=?' for k, _ in sets)} WHERE id=?"
        cur = conn.execute(sql, [v for _, v in sets] + [int(row_id)])
        conn.commit()
        return cur.rowcount

    async def update(self, row_id: int, fields: dict, *, conn=None) -> int:
        sets = list(self.to_row(fields).items())
        new_text = fields.get(self.embed_field)
        if new_text is not None:
            vec = (await embeddings.aembed_documents([new_text]))[0]
            sets.append(("embedding", dumps(vec)))
        if not sets:
            return 0
        conn, owned = await self._conn(conn)
        try:
            return await asyncio.to_thread(self._update, conn, row_id, sets)
        finally:
            if owned:
                await asyncio.to_thread(conn.close)

    def _delete(self, conn, row_id):
        cur = conn.execute(f"DELETE FROM {self.table} WHERE id=?", (int(row_id),))
        conn.commit()
        return cur.rowcount

    async def delete(self, row_id: int, *, conn=None) -> int:
        conn, owned = await self._conn(conn)
        try:
            return await asyncio.to_thread(self._delete, conn, row_id)
        finally:
            if owned:
                await asyncio.to_thread(conn.close)
