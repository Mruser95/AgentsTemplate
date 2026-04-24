from __future__ import annotations

import asyncio
import json
import sqlite3
import traceback
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

import yaml
from langchain_core.messages import BaseMessage, get_buffer_string

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CHECKPOINT_DB = PROJECT_ROOT / "SessionDB" / "checkpoints.db"
COLLATOR_DB = PROJECT_ROOT / "SessionDB" / "collator.db"
COLLATOR_LOG_DIR = PROJECT_ROOT / "Logs" / "collator"

with open(PROJECT_ROOT / "config.yaml", "r", encoding="utf-8") as _f:
    _config = yaml.safe_load(_f) or {}

collator_unsettled_threshold: int = int(_config.get("collator_unsettled_threshold", 30))
collator_max_parallel: int = int(_config.get("collator_max_parallel", 2))
collator_retry_count: int = int(_config.get("collator_retry_count", 1))
collator_long_memory_k: int = int(_config.get("collator_long_memory_k", 5))

_CURSOR_DDL = """
CREATE TABLE IF NOT EXISTS collation_cursor (
    thread_id      TEXT PRIMARY KEY,
    last_msg_count INTEGER NOT NULL DEFAULT 0,
    last_run_at    TEXT    NOT NULL
)
"""


# cursor sqlite ==================================================================


def _cursor_conn() -> sqlite3.Connection:
    COLLATOR_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(COLLATOR_DB))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(_CURSOR_DDL)
    return conn


def _load_cursor_sync(thread_id: str) -> int:
    with closing(_cursor_conn()) as conn:
        row = conn.execute(
            "SELECT last_msg_count FROM collation_cursor WHERE thread_id=?",
            (thread_id,),
        ).fetchone()
    return int(row[0]) if row else 0


def _save_cursor_sync(thread_id: str, n: int) -> None:
    # `with conn:` 负责提交/回滚；`closing` 负责关闭
    with closing(_cursor_conn()) as conn, conn:
        conn.execute(
            "INSERT INTO collation_cursor (thread_id, last_msg_count, last_run_at) "
            "VALUES (?, ?, ?) "
            "ON CONFLICT(thread_id) DO UPDATE SET "
            "last_msg_count=excluded.last_msg_count, "
            "last_run_at=excluded.last_run_at",
            (thread_id, int(n), datetime.now().isoformat(timespec="seconds")),
        )


# checkpoint read ================================================================


async def _read_checkpoint_messages(thread_id: str) -> list[BaseMessage]:
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    CHECKPOINT_DB.parent.mkdir(parents=True, exist_ok=True)
    async with AsyncSqliteSaver.from_conn_string(str(CHECKPOINT_DB)) as saver:
        tup = await saver.aget_tuple({"configurable": {"thread_id": thread_id}})
    if tup is None:
        return []
    messages = (
        (getattr(tup, "checkpoint", None) or {})
        .get("channel_values", {})
        .get("messages")
        or []
    )
    return list(messages) if isinstance(messages, list) else []


def _collect_used_tools(messages: list[BaseMessage]) -> set[str]:
    used: set[str] = set()
    for msg in messages:
        for tc in getattr(msg, "tool_calls", None) or []:
            name = tc.get("name") if isinstance(tc, dict) else getattr(tc, "name", None)
            if name:
                used.add(str(name))
    return used


# scheduler ======================================================================


class CollationScheduler:
    def __init__(self, *, unsettled_threshold: int = collator_unsettled_threshold,
        max_parallel: int = collator_max_parallel, retry_count: int = collator_retry_count,
        long_memory_k: int = collator_long_memory_k,
    ) -> None:
        self.unsettled_threshold = int(unsettled_threshold)
        self._max_parallel = int(max_parallel)
        self._retry_count = max(int(retry_count), 0)
        self._long_memory_k = int(long_memory_k)
        self._sem: Optional[asyncio.Semaphore] = None
        self._unsettled: dict[str, int] = {}
        self._tasks: dict[str, asyncio.Task] = {}

    def notify(self, thread_id: str, delta: int = 2) -> None:
        try:
            n = self._unsettled.get(thread_id, 0) + max(int(delta), 0)
            self._unsettled[thread_id] = n
            if n >= self.unsettled_threshold:
                self._kick(thread_id)
        except Exception:
            self._log(thread_id, route="notify", ok=False, error=traceback.format_exc())

    def shutdown(self) -> None:
        for task in self._tasks.values():
            if not task.done():
                task.cancel()

    def _kick(self, thread_id: str) -> None:
        task = self._tasks.get(thread_id)
        if task is not None and not task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        if self._sem is None:
            self._sem = asyncio.Semaphore(self._max_parallel)
        self._tasks[thread_id] = loop.create_task(self._collate(thread_id))

    async def _collate(self, thread_id: str) -> None:
        assert self._sem is not None
        async with self._sem:
            try:
                messages = await _read_checkpoint_messages(thread_id)
                last_count = await asyncio.to_thread(_load_cursor_sync, thread_id)
                new_messages = messages[last_count:]
                if not new_messages:
                    self._unsettled[thread_id] = 0
                    return

                routes: tuple[tuple[str, Callable[[], Awaitable[Any]]], ...] = (
                    ("short",  lambda: self._run_short(thread_id, new_messages, offset=last_count)),
                    ("long",   lambda: self._run_long(thread_id, new_messages)),
                    ("skills", lambda: self._run_skills(thread_id, new_messages)),
                )
                await asyncio.gather(
                    *(self._run_route(thread_id, name, factory) for name, factory in routes)
                )
                await asyncio.to_thread(_save_cursor_sync, thread_id, len(messages))
                self._unsettled[thread_id] = 0
                self._log(thread_id, route="collate", ok=True, new_messages=len(new_messages))
            except Exception:
                self._log(thread_id, route="collate", ok=False, error=traceback.format_exc())

    async def _run_route(self, thread_id: str, name: str,
        factory: Callable[[], Awaitable[Any]],
    ) -> None:
        for attempt in range(1, self._retry_count + 2):
            try:
                await factory()
            except BaseException:
                final = attempt > self._retry_count
                self._log(thread_id, route=name, ok=False,
                          error=traceback.format_exc(),
                          attempt=attempt, retrying=not final)
                if final:
                    return
                continue
            self._log(thread_id, route=name, ok=True, attempt=attempt)
            return

    # ---- three collation routes ----

    async def _run_short(self, thread_id: str, new_messages: list[BaseMessage], *, offset: int) -> None:
        from Agents.remember import short_memory_chain
        from Memory import shortMem

        entry = await short_memory_chain.ainvoke(
            {"transcript": get_buffer_string(new_messages)}
        )
        payload: dict[str, Any] = entry.model_dump()

        # chain 里 turn_range 是相对增量（1-based），外层修正为全局位置
        tr = payload.get("turn_range") or [1, len(new_messages)]
        try:
            local_start, local_end = int(tr[0]), int(tr[1])
        except (TypeError, ValueError, IndexError):
            local_start, local_end = 1, len(new_messages)
        payload["turn_range"] = (offset + local_start, offset + local_end)
        payload.setdefault("timestamp", datetime.now().isoformat())

        await shortMem.store(payload, thread_id=thread_id)

    async def _run_long(self, thread_id: str, new_messages: list[BaseMessage]) -> None:
        from Agents.remember import long_memory_chain
        from Agents.collator import collate_long_memory

        batch = await long_memory_chain.ainvoke(
            {"transcript": get_buffer_string(new_messages)}
        )
        entries = getattr(batch, "long_memories", None) or []
        if not entries:
            return

        candidates: list[dict] = []
        for e in entries:
            d = e.model_dump() if hasattr(e, "model_dump") else dict(e)
            d.setdefault("timestamp", datetime.now().isoformat())
            candidates.append(d)

        await collate_long_memory.ainvoke(
            {"candidates": candidates, "k": self._long_memory_k},
            config={"configurable": {"thread_id": thread_id}},
        )

    async def _run_skills(self, thread_id: str, new_messages: list[BaseMessage]) -> None:
        from Agents.collator import collate_tool_skill
        from Tools.skills import index as skill_index

        used_tools = _collect_used_tools(new_messages)
        targets: list[tuple[str, Path]] = []
        for tool_name in used_tools:
            path = (skill_index.get(tool_name) or {}).get("path")
            if path and Path(path).exists():
                targets.append((tool_name, Path(path)))
        if not targets:
            return

        errors: list[BaseException] = []
        for tool_name, path in targets:
            try:
                rel = path.relative_to(PROJECT_ROOT)
            except ValueError:
                rel = path
            try:
                await collate_tool_skill.ainvoke(
                    {
                        "skill_path": str(rel),
                        "tool_name": tool_name,
                        "messages": new_messages,
                    },
                    config={"configurable": {"thread_id": thread_id}},
                )
            except BaseException as e:
                errors.append(e)

        if errors:
            raise RuntimeError(
                "skill curation errors: " + "; ".join(repr(e) for e in errors)
            )

    @staticmethod
    def _log(thread_id: str, *, route: str, ok: bool,
             error: Optional[str] = None, **extra: Any) -> None:
        try:
            COLLATOR_LOG_DIR.mkdir(parents=True, exist_ok=True)
            safe_id = "".join(c if c.isalnum() or c in "-_" else "_" for c in thread_id)
            record: dict[str, Any] = {
                "ts": datetime.now().isoformat(timespec="seconds"),
                "thread_id": thread_id,
                "route": route,
                "ok": bool(ok),
            }
            if error:
                record["error"] = error
            record.update({k: v for k, v in extra.items() if v is not None})
            line = json.dumps(record, ensure_ascii=False, default=str)
            with (COLLATOR_LOG_DIR / f"{safe_id}.jsonl").open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass


scheduler = CollationScheduler()
