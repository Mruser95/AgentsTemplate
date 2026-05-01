import asyncio
import sqlite3
from pathlib import Path
from typing import Optional, Union, overload
from langchain_core.tools import tool
from Memory._base import dumps, embeddings, ensure_conn, loads_list, open_db, rank_rows


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = PROJECT_ROOT / "SessionDB" / "short_memory.db"

_DDL: list[str] = [
    """
    CREATE TABLE IF NOT EXISTS short_memory (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        thread_id       TEXT    NOT NULL DEFAULT '',
        summary         TEXT    NOT NULL,
        turn_start      INTEGER NOT NULL,
        turn_end        INTEGER NOT NULL,
        key_decisions   TEXT    NOT NULL DEFAULT '[]',
        open_tasks      TEXT    NOT NULL DEFAULT '[]',
        active_entities TEXT    NOT NULL DEFAULT '[]',
        timestamp       TEXT    NOT NULL,
        embedding       TEXT    NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_short_memory_ts     ON short_memory(timestamp)",
    "CREATE INDEX IF NOT EXISTS idx_short_memory_thread ON short_memory(thread_id)",
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
        turn_range = e["turn_range"]
        cur = conn.execute(
            "INSERT INTO short_memory "
            "(thread_id, summary, turn_start, turn_end, key_decisions, "
            " open_tasks, active_entities, timestamp, embedding) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                thread_id,
                e["summary"],
                int(turn_range[0]),
                int(turn_range[1]),
                dumps(e.get("key_decisions", [])),
                dumps(e.get("open_tasks", [])),
                dumps(e.get("active_entities", [])),
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
    entry: Union[dict, list[dict]],
    *,
    thread_id: str = "",
    conn: Optional[sqlite3.Connection] = None,
) -> Union[int, list[int]]:
    single = isinstance(entry, dict)
    entries: list[dict] = [entry] if single else list(entry)

    conn, owned = await ensure_conn(init_db, conn)
    try:
        if not entries:
            return 0 if single else []
        vecs = await embeddings.aembed_documents([e["summary"] for e in entries])
        ids = await asyncio.to_thread(_insert_rows, conn, entries, vecs, thread_id)
        return ids[0] if single else ids
    finally:
        if owned:
            await asyncio.to_thread(conn.close)


# search =======================================================================


def _load_rows(conn: sqlite3.Connection, thread_id: Optional[str]) -> list[dict]:
    if thread_id is None:
        cur = conn.execute(
            "SELECT id, summary, turn_start, turn_end, key_decisions, "
            "open_tasks, active_entities, timestamp, embedding FROM short_memory"
        )
    else:
        cur = conn.execute(
            "SELECT id, summary, turn_start, turn_end, key_decisions, "
            "open_tasks, active_entities, timestamp, embedding FROM short_memory "
            "WHERE thread_id=?",
            (thread_id,),
        )
    return [
        {
            "id": r[0],
            "summary": r[1],
            "turn_range": (int(r[2]), int(r[3])),
            "key_decisions": loads_list(r[4]),
            "open_tasks": loads_list(r[5]),
            "active_entities": loads_list(r[6]),
            "timestamp": r[7] or "",
            "_vec": loads_list(r[8]),
        }
        for r in cur.fetchall()
    ]


async def search_neighbors(
    contents: Union[str, list[str]],
    *,
    k: int = 5,
    thread_id: Optional[str] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> list[dict]:
    """召回最近邻；thread_id=None 时跨 thread 检索，否则仅在该 thread 里检索。"""
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


# tool =========================================================================


@tool
async def search_short_memory(query: str, k: int = 5) -> list[dict]:
    """
    向量检索当前会话（thread_id）的短期记忆库（历次会话摘要）。
    返回 top-k 条，每条含 id / summary / turn_range / key_decisions / open_tasks /
    active_entities / timestamp / similarity。
    """
    from Tools.utils import current_thread_id
    return await search_neighbors(query, k=k, thread_id=current_thread_id())
