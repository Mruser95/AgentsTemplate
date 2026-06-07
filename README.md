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
- **skill_library 强约束**：子代理首次调用某工具前必须先 `skill_library(tool_name="<name>")` 加载规范，避免参数误用。

## 角色与职责

| 代理 | 文件 | 角色 |
|---|---|---|
| **MANAGER** | [Agents/manager.py](Agents/manager.py) | 项目经理：澄清需求 → 写 `plan.json` → 派发 → 收口；唯一被 SQLite checkpoint 持久化的代理。 |
| **TASKER (tasker_coder)** | [Agents/Tasker_coder.py](Agents/Tasker_coder.py) | 多模块编码调度：拆 step → `dispatch_coder` 逐条派给隔离 coder；维护 `workingTodo.md`。 |
| **CODER** | [Agents/coder.py](Agents/coder.py) | 单文件/单模块编码者；强制过 lint gate，结构化输出 `CoderReport`。 |
| **TESTER** | [Agents/tester.py](Agents/tester.py) | `dispatch_tester` 产 `TestDatasets.json`；`dispatch_test_runner` 跑全量用例出 `TestReport`。 |
| **RETIRVER** | [Agents/retriver.py](Agents/retriver.py) | 唯一深度搜索 agent，跨源融合：长/短期记忆、知识库、Tavily、Playwright。 |
| **CHECKER** | [Agents/checker.py](Agents/checker.py) | subtask done 时强制触发的对齐评估；`CheckerReport` 决定是否放行。 |
| **COLLATOR** | [schedule.py](schedule.py) + [Memory/](Memory/) + [SkillTree/](SkillTree/) | 后台调度器：按 `collation_turn_threshold` 触发 short / long / project / skills / skill_tree 五路。 |

## 工具一览

manager 与各子代理共享一套受预算约束的工具集（配额见 [config.yaml](config.yaml)）：

| 类别 | 工具 | 说明 |
|---|---|---|
| **代码读取** | `repo_map` / `grep` / `glob` / `read_file` | AST 签名+PageRank 摘要 / 文本搜索 / 通配列文件 / 按行读（offset 翻页） |
| **代码写入** | `edit` | `create / overwrite / str_replace / insert`，受 workspace 边界保护 |
| **执行** | `terminal` (SafeShell) | 锁定 cwd、超时 / 权限白名单，自动激活会话 venv |
| **网络/浏览器** | `tavily_search` / `browser` | 公网查证 / Playwright 动态页（仅 retriever） |
| **RAG/记忆** | `knowledge_search` / `search_long_memory` / `search_short_memory` | Milvus hybrid+RRF+AutoMerging+Rerank；长记忆全局共享，短记忆按 thread 隔离（仅 retriever） |
| **状态** | `plan` / `todo` | plan.json（manager 写）/ workingTodo.md（tasker 写，manager 只读） |
| **质量门** | `linter` | py_compile / node --check / gcc / javac 等多语言语法关 |
| **调度** | `schedule` | 增删查回看定时任务（仅 manager） |
| **MCP** | `mcp` | 经 streamable-http 接外部 MCP server |
| **元能力** | `skill_library` / `skill_tree` / `component_library` | 分别加载工具规范（首用必查）/ 查 COLLATOR 沉淀的复用技能 / 检索 CompLib 通用组件供 manager 拼装领域 agent |

## RAG / 知识库管道

`Knowledge/` 提供一套**法条级**清洗→入库→检索样例（默认面向 `.docx` 法律文本，可替换 `read_documents`）：

- **清洗切分** ([cleanout.py](Knowledge/cleanout.py))：按 `第 N 编/章/节` 维护层级、`第 N 条` 切主节点；主节点 >512 字符再用 `HierarchicalNodeParser`（L0 1024 / L1 256）切子节点。
- **入库** ([createIndex.py](Knowledge/createIndex.py))：`MilvusVectorStore`（HNSW）+ 内建 `BM25BuiltInFunction`(jieba) 双路；双 docstore 支持 AutoMerging；`IngestionPipeline + UPSERTS` 增量更新。
- **检索** ([retriever.py](Knowledge/retriever.py))：`QueryFusionRetriever` 改写 4 路 RRF 融合 → `AutoMergingRetriever` 合回大块 → `RerankAPI` top-N 重排。

## 后台 COLLATOR

`CollationScheduler` 在 manager 每次工具调用后 `notify(thread_id, delta)` 累计活动消息；满 `collation_turn_threshold`（默认 20）即并发触发 5 路整理：

| route | 行为 |
|---|---|
| `short` | 压缩**较旧一半**消息为 `ShortMemoryEntry`（issues/decisions/errors/resolutions），原 message 标 `SUMMARY_MARKER` 占位。 |
| `long` | 从增量 transcript 抽 `LongMemoryEntry`，再 `collate_long_memory` 做增删改/跳过决策。 |
| `project` | 把"目标-上一步-这一步-效果-达成"一句话追加到 `SessionDB/<tid>/projectKnow.md`（线程隔离）；任务切换时重置。 |
| `skills` | 扫本批次用过的工具，更新 `Skills/<tool>_skill.md` 的 `## 探索经验`（add/update/replace/remove）。 |
| `skill_tree` | 从 `projectKnow.md` 提炼复用技能落 `SkillTree/<category>/<name>.md`（带 frontmatter），供 `skill_tree` 工具按需查阅。 |

并发受 `collation_max_parallel` 控制，失败按 `collation_retry_count` 重试，日志写 `Logs/collation/<tid>.jsonl`。

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

## 扩展：工具 / 组件 / MCP

- **加工具**：在 [Tools/](Tools/) 写 `BaseTool` 子类（参考 [Tools/edit.py](Tools/edit.py)），自带 `bump_budget` 或在 `config.yaml` 配 `<tool>_count_limit`；在 [Skills/](Skills/) 加 `<name>_skill.md`（frontmatter 写 `tool` + `description`，正文留 `## 探索经验` 代码块）；最后挂到对应代理的 `_*_TOOLS`（manager 见 [Agents/manager.py](Agents/manager.py)）。
- **复用组件**：[CompLib/](CompLib/) 下每个组件 = 实现 + `SKILL.md` 接口规范；manager 用 `component_library` 检索后 import 组合，[build_agent.py](build_agent.py) 据此非交互拼装成品 agent 到 `BuiltAgents/`。
- **MCP**：[Tools/mcp.py](Tools/mcp.py) 内置 FastMCP server + `MultiServerMCPClient`；在 [config.yaml](config.yaml) 配 `mcp_server` 地址，`extensions` 追加 `{name, url, timeout}` 挂外部 MCP，`Tools.mcp.get_tools()` 取合并工具列表再追加到代理。

## 许可

内部模板，未附 LICENSE。
