import asyncio
import sqlite3
from pathlib import Path
from typing import Optional, Union, overload
from langchain_core.tools import tool
from Memory._base import dumps, embeddings, ensure_conn, loads_list, open_db, rank_rows


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = PROJECT_ROOT / "SessionDB" / "long_memory.db"

_DDL: list[str] = [
    """
    CREATE TABLE IF NOT EXISTS long_memory (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        thread_id   TEXT    NOT NULL DEFAULT '',
        content     TEXT    NOT NULL,
        memory_type TEXT    NOT NULL,
        importance  INTEGER NOT NULL,
        context     TEXT    NOT NULL DEFAULT '',
        tags        TEXT    NOT NULL DEFAULT '[]',
        timestamp   TEXT    NOT NULL,
        embedding   TEXT    NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_long_memory_ts         ON long_memory(timestamp)",
    "CREATE INDEX IF NOT EXISTS idx_long_memory_type       ON long_memory(memory_type)",
    "CREATE INDEX IF NOT EXISTS idx_long_memory_importance ON long_memory(importance)",
    "CREATE INDEX IF NOT EXISTS idx_long_memory_thread     ON long_memory(thread_id)",
]


def init_db(db_path: str | Path = DB_PATH) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    return open_db(db_path, _DDL)


# store ========================================================================


def _insert_rows(
    conn: sqlite3.Connection, entries: list[dict], vecs: list[list[float]], thread_id: str,
) -> list[int]:
    ids: list[int] = []
    for e, vec in zip(entries, vecs):
        cur = conn.execute(
            "INSERT INTO long_memory "
            "(thread_id, content, memory_type, importance, context, tags, timestamp, embedding) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                thread_id,
                e["content"],
                e["memory_type"],
                int(e["importance"]),
                e.get("context", ""),
                dumps(e.get("tags", [])),
                e.get("timestamp", ""),
                dumps(vec),
            ),
        )
        ids.append(cur.lastrowid)
    conn.commit()
    return ids


@overload
async def store(
    entry: dict, *, thread_id: str = "", conn: Optional[sqlite3.Connection] = None,
) -> int: ...
@overload
async def store(
    entry: list[dict], *, thread_id: str = "", conn: Optional[sqlite3.Connection] = None,
) -> list[int]: ...


async def store(
    entry: Union[dict, list[dict]], *,
    thread_id: str = "", conn: Optional[sqlite3.Connection] = None,
) -> Union[int, list[int]]:
    single = isinstance(entry, dict)
    entries: list[dict] = [entry] if single else list(entry)

    conn, owned = await ensure_conn(init_db, conn)
    try:
        if not entries:
            return 0 if single else []
        vecs = await embeddings.aembed_documents([e["content"] for e in entries])
        ids = await asyncio.to_thread(_insert_rows, conn, entries, vecs, thread_id)
        return ids[0] if single else ids
    finally:
        if owned:
            await asyncio.to_thread(conn.close)


# search =======================================================================


def _load_rows(conn: sqlite3.Connection, thread_id: Optional[str]) -> list[dict]:
    if thread_id is None:
        cur = conn.execute(
            "SELECT id, content, memory_type, importance, context, "
            "tags, timestamp, embedding FROM long_memory"
        )
    else:
        cur = conn.execute(
            "SELECT id, content, memory_type, importance, context, "
            "tags, timestamp, embedding FROM long_memory WHERE thread_id=?",
            (thread_id,),
        )
    return [
        {
            "id": r[0],
            "content": r[1],
            "memory_type": r[2],
            "importance": int(r[3]),
            "context": r[4] or "",
            "tags": loads_list(r[5]),
            "timestamp": r[6] or "",
            "_vec": loads_list(r[7]),
        }
        for r in cur.fetchall()
    ]


async def search_neighbors(
    contents: Union[str, list[str]], *, k: int = 5,
    thread_id: Optional[str] = None, conn: Optional[sqlite3.Connection] = None,
) -> list[dict]:
    """召回最近邻；thread_id=None 时跨 thread，否则只在该 thread 里查。"""
    queries = [contents] if isinstance(contents, str) else list(contents)
    if not queries:
        return []
    conn, owned = await ensure_conn(init_db, conn)
    try:
        vecs = await embeddings.aembed_documents(queries)
        rows = await asyncio.to_thread(_load_rows, conn, thread_id)
        return rank_rows(rows, vecs, k)
    finally:
        if owned:
            await asyncio.to_thread(conn.close)


# update / delete ==============================================================


_UPDATABLE = ("content", "memory_type", "importance", "context", "tags", "timestamp")


def _update_row(conn: sqlite3.Connection, row_id: int, sets: list[tuple[str, object]]) -> int:
    sql = f"UPDATE long_memory SET {', '.join(f'{k}=?' for k, _ in sets)} WHERE id=?"
    params = [v for _, v in sets] + [int(row_id)]
    cur = conn.execute(sql, params)
    conn.commit()
    return cur.rowcount


async def update(row_id: int, fields: dict, *, conn: Optional[sqlite3.Connection] = None) -> int:
    sets: list[tuple[str, object]] = []
    new_content = fields.get("content")
    new_vec: Optional[list[float]] = None
    if new_content is not None:
        new_vec = (await embeddings.aembed_documents([new_content]))[0]

    for key in _UPDATABLE:
        v = fields.get(key)
        if v is None:
            continue
        if key == "tags":
            v = dumps(list(v))
        elif key == "importance":
            v = int(v)
        sets.append((key, v))
    if new_vec is not None:
        sets.append(("embedding", dumps(new_vec)))

    if not sets:
        return 0

    conn, owned = await ensure_conn(init_db, conn)
    try:
        return await asyncio.to_thread(_update_row, conn, row_id, sets)
    finally:
        if owned:
            await asyncio.to_thread(conn.close)


def _delete_row(conn: sqlite3.Connection, row_id: int) -> int:
    cur = conn.execute("DELETE FROM long_memory WHERE id=?", (int(row_id),))
    conn.commit()
    return cur.rowcount


async def delete(row_id: int, *, conn: Optional[sqlite3.Connection] = None) -> int:
    conn, owned = await ensure_conn(init_db, conn)
    try:
        return await asyncio.to_thread(_delete_row, conn, row_id)
    finally:
        if owned:
            await asyncio.to_thread(conn.close)


# tool =========================================================================


@tool
async def search_long_memory(query: str, k: int = 5) -> list[dict]:
    """
    向量检索当前会话（thread_id）的长期记忆库。
    返回 top-k 条，每条含 id / content / memory_type / importance / context /
    tags / timestamp / similarity。
    """
    from Tools.utils import current_thread_id
    return await search_neighbors(query, k=k, thread_id=current_thread_id())
