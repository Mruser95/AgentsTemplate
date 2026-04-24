# Project Know — 仍需跟进的清单

> 本文件只记录**当前版本尚未处理**但面向生产值得做的事项。已完成项不保留在这里。
> 每次动工前先读一遍；动完工请更新本文件。

---

## 1. Browser session 的清理策略

- [Tools/browser.py](../Tools/browser.py) 现在是 `_sessions: dict[thread_id, BrowserSession]`，每个 thread 一个 Playwright 会话。功能上已经隔离，但**没有回收策略**：
  - 用户开了会话但不调 `action=close` → session 永远留着，Chromium 进程也留着。
  - 长期运行的服务会慢慢堆出一堆浏览器实例。
- 改法：
  - LRU + TTL：用 `OrderedDict` + 每个 session 记 `last_used_ts`；后台任务每 N 分钟扫一遍，把空闲超过阈值的关掉。
  - 或者给 `manager_session` 的 `__aexit__` 里加一个 hook：会话结束时强制 close 对应 thread 的 browser session。
  - 简单版：暴露一个 `Browser.aclose(thread_id)`，由 `manager_session` 退出时调用。

## 2. checkpointer 和记忆库的归档 / 清理

- `SessionDB/checkpoints.db` 会随会话轮次线性增长，`SessionDB/long_memory.db` 也一样。
- 目前没有任何 GC / 归档机制。
- 改法：
  - `AsyncSqliteSaver.adelete_thread(thread_id)`：对于长期不活跃（比如 30 天没 `collation_cursor.last_run_at`）的 thread，定时清掉 checkpoint。
  - long_memory：`importance=1` 的条目 > 7 天自动清（符合 `remember_skill.md` 里的分层存储约定）。
  - 用现有的 `schedule` 工具本身起定时任务：一个 `schedule:_gc` 的系统级 thread，每天跑一次清理。

## 3. Knowledge 单例

- [Knowledge/retriever.py](../Knowledge/retriever.py) 的 `_retriever` 是进程全局单例（读多写少，没按 thread 分）。
- 这个单例**是对的**——知识库本来就是全局的领域知识，不该按用户切。
- 但 `KnowledgeIngest` 写入时会让 `_load_bm25` 失效，其他 thread 正在 `search` 会短暂读到空 BM25 索引。
- 改法：`KnowledgeIngest` 加 `asyncio.Lock`，search 前等 ingest 完。低频操作，工程量小。

## 4. `retriever_agent` 无 checkpointer

- [Agents/retriver.py](../Agents/retriver.py) 的 `retrieve(query)` 是一次性 ainvoke，没 checkpointer。
- 这是**正确的选择**：检索是无状态的，记录 checkpoint 反而会让后续 retrieve 带上之前查询的上下文（污染结果）。
- 这条**不是坑**，只是记录一下设计决策，后续别有人"好心"给它加 checkpointer。

## 5. 工具预算在极端场景下的行为

- 所有工具现在都是 `_call_counts: dict[thread_id, int]`，按 thread 分预算。
- 极端场景：同一个 thread_id 在很长生命周期里被反复使用（比如一个"共享"的 `_default` thread）会持续累加，最终耗尽预算。
- 改法（可选）：
  - 每个 thread 的 `_call_counts` 带 TTL，或者跟 checkpointer 归档一起 reset。
  - 或者给工具加一个 `reset_thread(thread_id)` 方法，由 `manager_session.__aexit__` 调。
