from __future__ import annotations

import asyncio
import importlib
import json
import re
import sqlite3
import traceback
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

_CURSOR_DDL = """
CREATE TABLE IF NOT EXISTS collation_cursor (
    thread_id      TEXT PRIMARY KEY,
    last_msg_count INTEGER NOT NULL DEFAULT 0,
    last_run_at    TEXT    NOT NULL
)
"""


class CursorStore:
    """记录每个 thread 已整理到的消息位置：sqlite 游标读写。"""

    def __init__(self, db_path) -> None:
        self.db_path = Path(db_path)

    def _conn(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(_CURSOR_DDL)
        return conn

    def load(self, tid: str) -> int:
        with closing(self._conn()) as c:
            row = c.execute("SELECT last_msg_count FROM collation_cursor WHERE thread_id=?", (tid,)).fetchone()
        return int(row[0]) if row else 0

    def save(self, tid: str, n: int) -> None:
        with closing(self._conn()) as c, c:
            c.execute(
                "INSERT INTO collation_cursor (thread_id, last_msg_count, last_run_at) VALUES (?,?,?) "
                "ON CONFLICT(thread_id) DO UPDATE SET "
                "last_msg_count=excluded.last_msg_count, last_run_at=excluded.last_run_at",
                (tid, int(n), datetime.now().isoformat(timespec="seconds")),
            )


class RunLogger:
    """整理任务的 jsonl 日志：每个 thread 一个文件，吞掉自身异常不影响主流程。"""

    def __init__(self, log_dir) -> None:
        self.log_dir = Path(log_dir)

    def log(self, tid: str, *, route: str, ok: bool, error: Optional[str] = None, **extra) -> None:
        try:
            self.log_dir.mkdir(parents=True, exist_ok=True)
            safe = re.sub(r"[^\w\-]", "_", tid)
            rec = {"ts": datetime.now().isoformat(timespec="seconds"), "thread_id": tid, "route": route, "ok": bool(ok)}
            if error:
                rec["error"] = error
            rec.update({k: v for k, v in extra.items() if v is not None})
            (self.log_dir / f"{safe}.jsonl").open("a", encoding="utf-8").write(
                json.dumps(rec, ensure_ascii=False, default=str) + "\n"
            )
        except Exception:
            pass


RouteFn = Callable[..., Awaitable[Any]]


def _resolve_route(spec: Any) -> RouteFn:
    if callable(spec):
        return spec
    mod, fn = str(spec).split(":")
    return getattr(importlib.import_module(mod), fn)


class CollationScheduler:
    """后台整理调度（编排节拍）：累计消息到阈值 → 并发跑各 route → 推进游标。

    完全解耦——消息来源 ``message_source``、保留过滤 ``keep``、游标 ``cursor_store``、日志 ``logger`` 全部注入；
    routes 既可传 callable 也可传 'module:func' 字符串，每个 ``fn(tid, new_msgs, *, offset, k)``（async）。
    """

    def __init__(
        self,
        routes,
        message_source: Callable[[str], Awaitable[list]],
        *,
        cursor_store: CursorStore,
        logger: RunLogger,
        keep: Optional[Callable[[Any], bool]] = None,
        turn_threshold: int = 50,
        max_parallel: int = 2,
        retry_count: int = 1,
        long_memory_k: int = 5,
    ) -> None:
        self._routes_spec = tuple(routes)
        self._routes: Optional[list] = None
        self.message_source = message_source
        self.keep = keep or (lambda m: True)
        self.cursor = cursor_store
        self.logger = logger
        self.turn_threshold = max(int(turn_threshold), 1)
        self._max_parallel = max(int(max_parallel), 1)
        self._retries = max(int(retry_count), 0)
        self._k = int(long_memory_k)
        self._sem: Optional[asyncio.Semaphore] = None
        self._counts: dict[str, int] = {}
        self._tasks: dict[str, asyncio.Task] = {}

    def notify(self, tid: str, delta: int = 1) -> None:
        try:
            n = self._counts.get(tid, 0) + max(int(delta), 0)
            self._counts[tid] = n
            if n >= self.turn_threshold:
                self._kick(tid)
        except Exception:
            self.logger.log(tid, route="notify", ok=False, error=traceback.format_exc())

    def shutdown(self) -> None:
        for t in self._tasks.values():
            if not t.done():
                t.cancel()

    def _ensure_routes(self) -> list:
        if self._routes is None:
            self._routes = [(name, _resolve_route(spec)) for name, spec in self._routes_spec]
        return self._routes

    def _kick(self, tid: str) -> None:
        t = self._tasks.get(tid)
        if t is not None and not t.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        if self._sem is None:
            self._sem = asyncio.Semaphore(self._max_parallel)
        self._tasks[tid] = loop.create_task(self._collate(tid))

    async def _collate(self, tid: str) -> None:
        assert self._sem is not None
        async with self._sem:
            try:
                msgs = await self.message_source(tid)
                last = await asyncio.to_thread(self.cursor.load, tid)
                new = [m for m in msgs[last:] if self.keep(m)]
                if not new:
                    self._counts[tid] = 0
                    return
                routes = self._ensure_routes()
                await asyncio.gather(*(self._run(tid, name, fn, new, offset=last) for name, fn in routes))
                post = await self.message_source(tid)
                await asyncio.to_thread(self.cursor.save, tid, len(post))
                self._counts[tid] = 0
                self.logger.log(tid, route="collate", ok=True, new_messages=len(new))
            except Exception:
                self.logger.log(tid, route="collate", ok=False, error=traceback.format_exc())

    async def _run(self, tid: str, name: str, fn: RouteFn, new: list, *, offset: int) -> None:
        for attempt in range(1, self._retries + 2):
            try:
                await fn(tid, new, offset=offset, k=self._k)
            except BaseException:
                final = attempt > self._retries
                self.logger.log(tid, route=name, ok=False, error=traceback.format_exc(),
                                attempt=attempt, retrying=not final)
                if final:
                    return
                continue
            self.logger.log(tid, route=name, ok=True, attempt=attempt)
            return
