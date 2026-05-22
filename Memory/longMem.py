from pathlib import Path
from langchain_core.tools import tool
from Memory._base import MemoryStore, dumps, loads_list

DB_PATH = Path(__file__).resolve().parent.parent / "SessionDB" / "long_memory.db"

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

_COLS = ("content", "memory_type", "importance", "context", "tags", "timestamp")


def _to_row(e: dict) -> dict:
    out: dict = {}
    for k in _COLS:
        if k not in e or e[k] is None:
            continue
        if k == "tags":
            out[k] = dumps(list(e[k]))
        elif k == "importance":
            out[k] = int(e[k])
        else:
            out[k] = e[k]
    return out


def _from_row(r: dict) -> dict:
    return {
        "content": r["content"],
        "memory_type": r["memory_type"],
        "importance": int(r["importance"]),
        "context": r["context"] or "",
        "tags": loads_list(r["tags"]),
        "timestamp": r["timestamp"] or "",
    }


_store = MemoryStore(
    db_path=DB_PATH, table="long_memory", ddl=_DDL, columns=_COLS,
    embed_field="content", to_row=_to_row, from_row=_from_row,
)

init_db = _store.init_db
store = _store.store
search_neighbors = _store.search_neighbors
update = _store.update
delete = _store.delete


@tool
async def search_long_memory(query: str, k: int = 5) -> list[dict]:
    """
    向量检索当前会话（thread_id）的长期记忆库。
    返回 top-k 条，每条含 id / content / memory_type / importance / context /
    tags / timestamp / similarity。
    """
    from Tools.utils import current_thread_id
    return await search_neighbors(query, k=k, thread_id=current_thread_id())
