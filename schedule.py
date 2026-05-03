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

import yaml

from Tools.utils import PROJECT_ROOT, is_summary_message, read_ckpt_msgs


_CFG = yaml.safe_load((PROJECT_ROOT / "config.yaml").read_text(encoding="utf-8")) or {}
_TURN_THRESHOLD = int(_CFG.get("collation_turn_threshold", 100))
_MAX_PARALLEL = int(_CFG.get("collation_max_parallel", 2))
_RETRY_COUNT = int(_CFG.get("collation_retry_count", 1))
_LONG_MEM_K = int(_CFG.get("collation_long_memory_k", 5))

_CUR_DB = PROJECT_ROOT / "SessionDB" / "collation.db"
_LOG_DIR = PROJECT_ROOT / "Logs" / "collation"

_CURSOR_DDL = """
CREATE TABLE IF NOT EXISTS collation_cursor (
    thread_id      TEXT PRIMARY KEY,
    last_msg_count INTEGER NOT NULL DEFAULT 0,
    last_run_at    TEXT    NOT NULL
)
"""

DEFAULT_ROUTES: tuple[tuple[str, str], ...] = (
    ("short",  "Memory.mem_agent:route_short"),
    ("long",   "Memory.mem_agent:route_long"),
    ("skills", "SkillTree.skill_agent:route_skills"),
)

RouteFn = Callable[..., Awaitable[Any]]


def _cur_conn() -> sqlite3.Connection:
    _CUR_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_CUR_DB))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(_CURSOR_DDL)
    return conn


def _load_cursor(tid: str) -> int:
    with closing(_cur_conn()) as c:
        row = c.execute("SELECT last_msg_count FROM collation_cursor WHERE thread_id=?", (tid,)).fetchone()
    return int(row[0]) if row else 0


def _save_cursor(tid: str, n: int) -> None:
    with closing(_cur_conn()) as c, c:
        c.execute(
            "INSERT INTO collation_cursor (thread_id, last_msg_count, last_run_at) VALUES (?,?,?) "
            "ON CONFLICT(thread_id) DO UPDATE SET "
            "last_msg_count=excluded.last_msg_count, last_run_at=excluded.last_run_at",
            (tid, int(n), datetime.now().isoformat(timespec="seconds")),
        )


def _resolve_route(spec: Any) -> RouteFn:
    if callable(spec):
        return spec
    mod, fn = str(spec).split(":")
    return getattr(importlib.import_module(mod), fn)


class CollationScheduler:
    def __init__(
        self,
        routes: tuple[tuple[str, Any], ...] = DEFAULT_ROUTES,
        *,
        turn_threshold: int = _TURN_THRESHOLD,
        max_parallel: int = _MAX_PARALLEL,
        retry_count: int = _RETRY_COUNT,
        long_memory_k: int = _LONG_MEM_K,
    ) -> None:
        self._routes_spec = routes
        self._routes: Optional[list[tuple[str, RouteFn]]] = None
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
            if n >= self.turn_threshold * 2:
                self._kick(tid)
        except Exception:
            self._log(tid, route="notify", ok=False, error=traceback.format_exc())

    def shutdown(self) -> None:
        for t in self._tasks.values():
            if not t.done():
                t.cancel()

    def _ensure_routes(self) -> list[tuple[str, RouteFn]]:
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
                msgs = await read_ckpt_msgs(tid)
                last = await asyncio.to_thread(_load_cursor, tid)
                # 过滤掉 short 之前压缩留下的 SUMMARY 占位：它们已经是浓缩内容，
                # 再喂进 long/skills 的抽取链 = 重复处理 + 把摘要文本当事实回写入库。
                # short 自读 checkpoint，并不消费这个 new。
                new = [m for m in msgs[last:] if not is_summary_message(m)]
                if not new:
                    self._counts[tid] = 0
                    return
                routes = self._ensure_routes()
                await asyncio.gather(*(
                    self._run(tid, name, fn, new, offset=last) for name, fn in routes
                ))
                # cursor 按 post_msgs 长度推进：下一轮 msgs[cursor:] 是按位置切片，必须把 SUMMARY 占位算进去。
                post_msgs = await read_ckpt_msgs(tid)
                await asyncio.to_thread(_save_cursor, tid, len(post_msgs))
                self._counts[tid] = 0
                self._log(tid, route="collate", ok=True, new_messages=len(new))
            except Exception:
                self._log(tid, route="collate", ok=False, error=traceback.format_exc())

    async def _run(self, tid: str, name: str, fn: RouteFn, new: list, *, offset: int) -> None:
        for attempt in range(1, self._retries + 2):
            try:
                await fn(tid, new, offset=offset, k=self._k)
            except BaseException:
                final = attempt > self._retries
                self._log(tid, route=name, ok=False, error=traceback.format_exc(),
                          attempt=attempt, retrying=not final)
                if final:
                    return
                continue
            self._log(tid, route=name, ok=True, attempt=attempt)
            return

    @staticmethod
    def _log(tid: str, *, route: str, ok: bool, error: Optional[str] = None, **extra: Any) -> None:
        try:
            _LOG_DIR.mkdir(parents=True, exist_ok=True)
            safe = re.sub(r'[^\w\-]', '_', tid)
            rec: dict[str, Any] = {
                "ts": datetime.now().isoformat(timespec="seconds"),
                "thread_id": tid, "route": route, "ok": bool(ok),
            }
            if error:
                rec["error"] = error
            rec.update({k: v for k, v in extra.items() if v is not None})
            (_LOG_DIR / f"{safe}.jsonl").open("a", encoding="utf-8").write(
                json.dumps(rec, ensure_ascii=False, default=str) + "\n"
            )
        except Exception:
            pass


scheduler = CollationScheduler()
