# AgentsTemplate

基于 **LangGraph + LangChain** 的多代理项目执行框架。`manager` 把自然语言需求拆成 `plan.json`，按拓扑顺序派发给专职子代理（`tasker_coder` → `coder` / `tester` / `retriver` / `checker`）；每个 subtask 完成时由 `checker` 做硬门评估，全程经 SQLite checkpoint 持久化；后台 **COLLATOR** 按节拍把对话沉淀到长/短期记忆、项目笔记与 skill 经验树。

## 架构总览

```
   COLLATOR（后台调度器）: longmem · shortmem · projmem · checkpoint · skill chain
                       │ notify(turn 阈值)
                       ▼
 WebUI/API ─human_input─► MANAGER ─hard gate─► CHECKER（对齐评估）
                        规划/派发/验收
            ┌────────────┼────────────┐
            ▼            ▼            ▼
        RETIRVER      TASKER        TESTER
        多源调研    (多模块编排)   (用例生成)
                        │            │
                        ▼            ▼
                      CODER      Test Runner
                     (单文件)    (执行/报告)
                        └──────┬─────┘
                               ▼
                   sandbox(workspace) ─► answer ─► project

工具层：web/browser · shell · edit · repo_map/grep/glob · read · rag
         schedule · lint · mcp · skill/component library
```

### 关键约定

- **plan.json 是唯一可信事实源**：所有阶段决定都落 plan，不靠临时记忆。
- **checker hard gate**：subtask 标 `done` 时自动调 `checker`，输出 `on_track / minor_drift / major_drift / off_track`，manager 必须按报告调整。
- **TestDatasets.json 硬约束**：派过 tester 的任务，验收 subtask 必须 `dispatch_test_runner` 跑全量 cases 并按 `judgment_criteria` 判 pass/fail（详见 [agents_prompt.py](agents_prompt.py) 的 `MANAGER_TESTER_POLICY`）。
- **会话级 workspace 隔离**：子代理文件读写锁在 `SessionDB/thread_<id>/workspace/`，自带专属 `.venv`。
- **工具规范常驻 description**：各工具使用规范精华直接写在 tool description 里（零往返），无需先查文档再调用；`skill_tree` 则保留**按需查询** COLLATOR 沉淀技能。

## 角色与职责

| 代理 | 文件 | 角色 |
|---|---|---|
| **MANAGER** | [Agents/manager.py](Agents/manager.py) | 项目经理：澄清需求 → 写 `plan.json` → 派发 → 收口；唯一被 SQLite checkpoint 持久化的代理。 |
| **TASKER (tasker_coder)** | [Agents/Tasker_coder.py](Agents/Tasker_coder.py) | 多模块编码调度：拆 step → `dispatch_coder` 逐条派给隔离 coder；维护 `workingTodo.md`。 |
| **CODER** | [Agents/coder.py](Agents/coder.py) | 单文件/单模块编码者；强制过 lint gate，结构化输出 `CoderReport`。 |
| **TESTER** | [Agents/tester.py](Agents/tester.py) | `dispatch_tester` 产 `TestDatasets.json`；`dispatch_test_runner` 跑全量用例出 `TestReport`。 |
| **RETIRVER** | [Agents/retriver.py](Agents/retriver.py) | 唯一深度搜索 agent，跨源融合：长/短期记忆、知识库、Tavily、Playwright。 |
| **CHECKER** | [Agents/checker.py](Agents/checker.py) | subtask done 时强制触发的对齐评估；`CheckerReport` 决定是否放行。 |
| **COLLATOR** | [schedule.py](schedule.py) + [Memory/](Memory/) + [SkillTree/](SkillTree/) | 后台调度器：按 `collation_turn_threshold` 触发 short / long / project / skills 四路。 |

## 工具一览

manager 与各子代理共享一套受预算约束的工具集（配额见 [config.yaml](config.yaml)）：

| 类别 | 工具 | 说明 |
|---|---|---|
| **代码读取** | `repo_map` / `grep` / `glob` / `read_file` | AST 签名+PageRank 摘要 / 文本搜索 / 通配列文件 / 按行读（offset 翻页） |
| **代码写入** | `edit` | `create / overwrite / str_replace / insert`，受 workspace 边界保护 |
| **执行** | `terminal` (SafeShell) | 锁定 cwd 在 workspace、超时 / 权限白名单，自动激活会话 venv |
| **网络** | `tavily_search` | 单点公网查证 |
| **浏览器** | `browser` | Playwright 动态页面 / SPA / 登录态（仅 retriever 使用） |
| **RAG** | `knowledge_search` | Milvus hybrid (dense + BM25 jieba) + QueryFusion RRF + AutoMerging + 远端 Rerank |
| **记忆** | `search_long_memory` / `search_short_memory` | 仅 retriever 调用；长期记忆全局共享（不分 thread），短期记忆按 thread_id 隔离 |
| **状态** | `plan` / `todo` | plan.json（manager 写）/ workingTodo.md（tasker_coder 写，manager 只读） |
| **质量门** | `linter` | py_compile / node --check / gcc -fsyntax-only / javac 等多语言语法关 |
| **调度** | `schedule` | 创建 / 列出 / 删除 / 回看定时任务（仅 manager） |
| **MCP** | `mcp` | 通过 streamable-http 接入外部 MCP server（terminal / browser 扩展） |

## RAG / 知识库管道

`Knowledge/` 提供一套**法条级**清洗→入库→检索样例（默认面向 `.docx` 法律文本，可替换 `read_documents`）：

- **清洗切分** ([cleanout.py](Knowledge/cleanout.py))：按 `第 N 编/章/节` 维护层级、`第 N 条` 切主节点；主节点 >512 字符再用 `HierarchicalNodeParser`（L0 1024 / L1 256）切子节点。
- **入库** ([createIndex.py](Knowledge/createIndex.py))：`MilvusVectorStore`（HNSW）+ 内建 `BM25BuiltInFunction`(jieba) 双路；双 docstore 支持 AutoMerging；`IngestionPipeline + UPSERTS` 增量更新。
- **检索** ([retriever.py](Knowledge/retriever.py))：`QueryFusionRetriever` 改写 4 路 RRF 融合 → `AutoMergingRetriever` 合回大块 → `RerankAPI` top-N 重排。

## 后台 COLLATOR

`CollationScheduler` 在 manager 每次工具调用后 `notify(thread_id, delta)` 累计活动消息；满 `collation_turn_threshold`（默认 20）即并发触发 4 路整理：

| route | 行为 |
|---|---|
| `short` | 读 checkpoint，自动压缩**较旧一半**消息为 `ShortMemoryEntry`（含 issues / decisions / errors / resolutions），并把对应 message 标记为 `SUMMARY_MARKER` 占位。 |
| `long` | 从增量 transcript 抽取 `LongMemoryEntry`，再调 `collate_long_memory` 做插入 / 更新 / 删除 / 跳过的决策化整理。 |
| `project` | 把"目标-上一步-这一步-效果-达成"以一句话追加到 `SessionDB/<thread_id>/projectKnow.md`（按用户线程隔离，分开不同项目）；任务切换时整体重置。 |
| `skills` | 一次 LLM 调用合并维护 SkillTree：用本批次 transcript 教训 + `projectKnow.md` 流程，update 改进已有技能为主，确有新技能才 insert，落 `SkillTree/<category>/<name>.md`。 |

并发受 `collation_max_parallel` 控制，失败按 `collation_retry_count` 重试，日志写 `Logs/collation/<tid>.jsonl`。任一路重试耗尽仍失败则**不推进 cursor**（short 也不压缩），下一轮从原位置重放增量，保证不丢记忆。

## 快速开始

```bash
# 起服务（API:8973 / WebUI:8080 / Milvus / PG）
docker compose -f Docker/docker-compose.yaml up -d --build

# 非交互构建一个成品 agent（服务没起会自动拉起），产物落 BuiltAgents/<slug>/
./build.sh "构建一个能查天气并总结成简报的 agent"
```

`build.sh` 自动选执行方式：Docker 容器在跑→进容器执行；否则本地服务(Milvus)已起且有 `.venv`→本地直跑；都没起→拉起 Docker 再执行。`BuiltAgents/` 已挂载到宿主机，重建容器也不丢。

## 调用预算

每个工具/代理在 [config.yaml](config.yaml) 配 run（单次 invoke）级调用上限，每轮重新计数、不跨 thread。预算见底时代理主动收口、压缩、报告，不会死循环。工具返回末尾附 `[Tool call X/N, remaining: R]` 提示剩余配额。

## 添加你自己的工具

1. 在 [Tools/](Tools/) 写一个 `BaseTool` 子类（参考 [Tools/edit.py](Tools/edit.py)）：要么自带 `bump_budget` 限流，要么在 `config.yaml` 里加上 `<tool>_count_limit`；把使用规范精华（硬约束 / 反模式 / 预算纪律）直接写进 `description`。
2. 在对应代理的 `_*_TOOLS` 列表（manager 在 [Agents/manager.py](Agents/manager.py)，coder 在 [Agents/coder.py](Agents/coder.py) 的 `build_coder_agent`，依此类推）里挂上即可被调用。
3. 如果工具需要在 [agents_prompt.py](agents_prompt.py) 中显式管控（例如硬约束、调用顺序），同步补一段说明。

## 扩展 MCP 工具

[Tools/mcp.py](Tools/mcp.py) 内置了一个 FastMCP server + `MultiServerMCPClient`，可以把项目内部工具或外部 MCP server 都聚合给 manager。

- 在 [config.yaml](config.yaml) 的 `mcp_server` 配本机 MCP 的监听地址；
- 在 `extensions` 里追加 `{name, url, timeout}` 来挂外部 MCP（terminal / browser 等扩展）；
- 启动外部 MCP server 后，调用 `Tools.mcp.get_tools()` 即可拿到合并好的工具列表，再追加到对应代理的 `_*_TOOLS`。

## 许可

内部模板，未附 LICENSE。
