---
name: collation_scheduler
description: 后台整理调度组件：累计消息到阈值后并发跑多条整理 route（长/短记忆、笔记、技能等）。消息来源/过滤/游标/日志全注入，与具体记忆框架解耦。一个完整功能：给 agent 配后台沉淀节拍。
---

# collation_scheduler — CollationScheduler

实现文件：`CompLib/collation_scheduler/collation_scheduler.py`（单文件，内含下列协作类）

## 用途
给 agent 配一个「攒够 N 条消息就并发跑若干整理任务」的后台调度器。节拍逻辑通用，
消息从哪来、哪些算数、游标存哪、日志写哪全部注入。

## 接口
`from CompLib.collation_scheduler.collation_scheduler import CollationScheduler, CursorStore, RunLogger`

- `CursorStore(db_path)`：`load(tid)->int` / `save(tid, n)` 记录已整理位置
- `RunLogger(log_dir)`：`log(tid, *, route, ok, error=None, **extra)` 写 jsonl
- `CollationScheduler(routes, message_source, *, cursor_store, logger, keep=None, turn_threshold=50, max_parallel=2, retry_count=1, long_memory_k=5)`
  - `routes`：`[(name, fn_or_'mod:func'), ...]`，每个 `fn(tid, new_msgs, *, offset, k)`（async）
  - `message_source(tid) -> await list[msg]`；`keep(msg)->bool`（默认全保留）
  - `notify(tid, delta=1)`：累计，满阈值自动并发触发；`shutdown()` 取消在跑任务

## 依赖
仅标准库（asyncio/sqlite3/importlib）

## 用法示例
```python
from CompLib.collation_scheduler.collation_scheduler import CollationScheduler, CursorStore, RunLogger
async def source(tid): return await read_my_messages(tid)
async def long_route(tid, new, *, offset, k): ...
sch = CollationScheduler([("long", long_route)], source,
                         cursor_store=CursorStore("collation.db"), logger=RunLogger("logs/"), turn_threshold=20)
sch.notify("t1", delta=5)   # 攒够即后台触发
```
