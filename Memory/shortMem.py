from pathlib import Path
from langchain_core.tools import tool
from Memory._base import MemoryStore, dumps, loads_list

DB_PATH = Path(__file__).resolve().parent.parent / "SessionDB" / "short_memory.db"

_DDL: list[str] = [
    """
    CREATE TABLE IF NOT EXISTS short_memory (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        thread_id       TEXT    NOT NULL DEFAULT '',
        summary         TEXT    NOT NULL,
        turn_start      INTEGER NOT NULL,
        turn_end        INTEGER NOT NULL,
        key_issues      TEXT    NOT NULL DEFAULT '[]',
        key_decisions   TEXT    NOT NULL DEFAULT '[]',
        key_errors      TEXT    NOT NULL DEFAULT '[]',
        resolutions     TEXT    NOT NULL DEFAULT '[]',
        open_tasks      TEXT    NOT NULL DEFAULT '[]',
        active_entities TEXT    NOT NULL DEFAULT '[]',
        timestamp       TEXT    NOT NULL,
        embedding       TEXT    NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_short_memory_ts     ON short_memory(timestamp)",
    "CREATE INDEX IF NOT EXISTS idx_short_memory_thread ON short_memory(thread_id)",
]

_COLS = (
    "summary", "turn_start", "turn_end",
    "key_issues", "key_decisions", "key_errors", "resolutions",
    "open_tasks", "active_entities", "timestamp",
)
_LIST_COLS = (
    "key_issues", "key_decisions", "key_errors", "resolutions",
    "open_tasks", "active_entities",
)


def _to_row(e: dict) -> dict:
    out: dict = {}
    if "summary" in e and e["summary"] is not None:
        out["summary"] = e["summary"]
    if "turn_range" in e and e["turn_range"] is not None:
        tr = e["turn_range"]
        out["turn_start"] = int(tr[0])
        out["turn_end"] = int(tr[1])
    for k in _LIST_COLS:
        if k in e and e[k] is not None:
            out[k] = dumps(list(e[k]))
    if "timestamp" in e and e["timestamp"] is not None:
        out["timestamp"] = e["timestamp"]
    return out


def _from_row(r: dict) -> dict:
    return {
        "summary": r["summary"],
        "turn_range": (int(r["turn_start"]), int(r["turn_end"])),
        "key_issues": loads_list(r["key_issues"]),
        "key_decisions": loads_list(r["key_decisions"]),
        "key_errors": loads_list(r["key_errors"]),
        "resolutions": loads_list(r["resolutions"]),
        "open_tasks": loads_list(r["open_tasks"]),
        "active_entities": loads_list(r["active_entities"]),
        "timestamp": r["timestamp"] or "",
    }


_store = MemoryStore(
    db_path=DB_PATH, table="short_memory", ddl=_DDL, columns=_COLS,
    embed_field="summary", to_row=_to_row, from_row=_from_row,
)

init_db = _store.init_db
store = _store.store
search_neighbors = _store.search_neighbors


@tool
async def search_short_memory(query: str, k: int = 5) -> list[dict]:
    """
    向量检索当前会话（thread_id）的短期记忆库（历次会话摘要）。
    返回 top-k 条，每条含 id / summary / turn_range / key_issues / key_decisions /
    key_errors / resolutions / open_tasks / active_entities / timestamp / similarity。
    """
    from Tools.utils import current_thread_id
    return await search_neighbors(query, k=k, thread_id=current_thread_id())
