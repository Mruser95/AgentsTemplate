from __future__ import annotations

from typing import Any
from langchain_core.runnables.config import ensure_config

DEFAULT_THREAD_ID = "_default"


def current_thread_id() -> str:
    cfg: dict[str, Any] = ensure_config()
    tid = (cfg.get("configurable") or {}).get("thread_id")
    return str(tid) if tid else DEFAULT_THREAD_ID


def bump_budget(counts: dict[str, int], thread_id: str, limit: int,) -> tuple[bool, int, int]:
    cur = counts.get(thread_id, 0)
    if cur >= limit:
        return False, cur, 0
    n = cur + 1
    counts[thread_id] = n
    return True, n, limit - n
