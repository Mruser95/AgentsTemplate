# AgentsTemplate

一个基于 **LangGraph + LangChain** 的多代理项目执行框架。`manager` 把自然语言需求拆成 `plan.json`，按拓扑顺序派发给专职子代理（`tasker_coder` → `coder` / `tester` / `retriver` / `checker`）执行；每个 subtask 完成时由 `checker` 做硬门评估，全过程通过 SQLite checkpoint 持久化；后台 **COLLATOR** 调度器按节拍把对话流转沉淀到长/短期记忆、项目笔记与 skill 经验树。

## 架构总览

```
                       ┌──────────────────────────┐
                       │  COLLATOR（后台调度器）  │
                       │  longmem · shortmem      │
                       │  projmem · checkpoint    │
                       │  skill chain             │
                       └─────────────┬────────────┘
                                     │ notify(turn 阈值)
                                     ▼
┌──────────────┐  human_input  ┌───────────────┐         ┌────────────┐
│  WebUI / API ├──────────────►│    MANAGER    │────────►│  CHECKER   │
└──────────────┘               │ 规划/派发/验收│  hard   │  对齐评估  │
                               └──┬────┬────┬──┘  gate   └──────┬─────┘
                                  │    │    │                   │
                          ┌───────┘    │    └────────┐          │
                          ▼            ▼             ▼          │
                     RETIRVER       TASKER         TESTER       │
                     多源调研     (多模块编排)    (用例生成)    │
                                      │              │          │
                                      ▼              ▼          │
                                    CODER       Test Runner     │
                                   (单文件)    (执行/出报告)    │
                                      │              │          │
                                      └──────┬───────┘          │
                                             ▼                  │
                                       ┌─────────────┐          │
                                       │   sandbox   │◄─────────┘
                                       │ (workspace) │
                                       └──────┬──────┘
                                              ▼
                                       answer ──► project

工具层：
  web/browser · shell · edit · repo_map/grep/glob · rag · schedule
  lint · mcp · skill library · summary
```

### 关键约定

- **plan.json 是唯一可信事实源**：所有阶段决定都落 plan，不靠临时记忆。
- **checker hard gate**：`plan` 在 subtask 标记 `done` 时自动调 `checker`，输出 `on_track / minor_drift / major_drift / off_track`，manager 必须按报告调整。
- **TestDatasets.json 用例覆盖硬约束**：派过 tester 的任务，验收 subtask 必须 `dispatch_test_runner` 跑全量 cases 并按 `judgment_criteria` 判 pass/fail（详见 [agents_prompt.py](agents_prompt.py) 中 `MANAGER_TESTER_POLICY`）。
- **每个会话隔离 workspace**：子代理的文件读写都锁在 `SessionDB/thread_<id>/workspace/`，并自带专属 `.venv`。
- **skill_library 强约束**：任何子代理首次调用某工具前必须先 `skill_library(tool_name="<name>")` 加载规范，避免参数误用。

## 角色与职责

| 代理 | 文件 | 角色 |
|---|---|---|
| **MANAGER** | [Agents/manager.py](Agents/manager.py) | 项目经理：澄清需求 → 写 `plan.json` → 派发 → 收口；唯一被持久化（SQLite checkpoint）的代理。 |
| **TASKER (tasker_coder)** | [Agents/Tasker_coder.py](Agents/Tasker_coder.py) | 多模块编码调度：把综合编码任务拆成独立 step，再用 `dispatch_coder` 逐条派给隔离的 coder 子代理；维护 `workingTodo.md`。 |
| **CODER** | [Agents/coder.py](Agents/coder.py) | 单文件 / 单模块实际编码者；强制走 lint gate，结构化输出 `CoderReport`。 |
| **TESTER** | [Agents/tester.py](Agents/tester.py) | `dispatch_tester` 产 `TestDatasets.json`；`dispatch_test_runner` 跑全量用例输出 `TestReport`。 |
| **RETIRVER** | [Agents/retriver.py](Agents/retriver.py) | 唯一深度搜索 agent，跨源融合：长/短期记忆、项目知识库、Tavily 公网、Playwright 浏览器。 |
| **CHECKER** | [Agents/checker.py](Agents/checker.py) | subtask done 时强制触发的对齐评估；输出 `CheckerReport` 决定是否允许继续。 |
| **COLLATOR** | [schedule.py](schedule.py) + [Memory/](Memory/) + [SkillTree/](SkillTree/) | 后台调度器：按 `collation_turn_threshold` 触发 short / long / project / skills / skill_tree 五条 route。 |


## 工具一览

manager 与各子代理共享一套受预算约束的工具集（配额详见 [config.yaml](config.yaml)）：

| 类别 | 工具 | 说明 |
|---|---|---|
| **代码浏览** | `repo_map` / `grep` / `glob` | AST 签名 + PageRank 摘要 / 文本搜索 / 通配符列文件 |
| **代码写入** | `edit` | `create / overwrite / str_replace / insert`，受 workspace 边界保护 |
| **执行** | `terminal` (SafeShell) | 锁定 cwd 在 workspace、超时 / 权限白名单，自动激活会话 venv |
| **网络** | `tavily_search` | 单点公网查证 |
| **浏览器** | `browser` | Playwright 动态页面 / SPA / 登录态（仅 retriever 使用） |
| **RAG** | `knowledge_search` | Milvus hybrid (dense + BM25 jieba) + QueryFusion RRF + AutoMerging + 远端 Rerank |
| **记忆** | `search_long_memory` / `search_short_memory` | 仅 retriever 调用；按 thread_id 隔离 |
| **状态** | `plan` / `todo` | plan.json（manager 写）/ workingTodo.md（tasker_coder 写，manager 只读） |
| **质量门** | `linter` | py_compile / node --check / gcc -fsyntax-only / javac 等多语言语法关 |
| **调度** | `schedule` | 创建 / 列出 / 删除 / 回看定时任务（仅 manager） |
| **MCP** | `mcp` | 通过 streamable-http 接入外部 MCP server（terminal / browser 扩展） |
| **元能力** | `skill_library` | 加载工具规范文档（首次用前必查） |

## RAG / 知识库管道

`Knowledge/` 提供一套**法条级**清洗 + 入库 + 检索的样例（默认面向 `.docx` 法律文本，可按需替换 `read_documents`）：

1. **清洗与切分** ([cleanout.py](Knowledge/cleanout.py))
   - 去目录、按 `第 N 编/章/节` 维护层级 ctx；按 `第 N 条` 切成主节点；
   - 主节点 > 512 字符时再用 `HierarchicalNodeParser`（L0 1024 / L1 256）做子节点切分。
2. **入库** ([createIndex.py](Knowledge/createIndex.py))
   - 后端：`MilvusVectorStore`（HNSW，dim 默认 4096）+ 内建 `BM25BuiltInFunction` (jieba 分词) 双路；
   - 持久化双 docstore（叶子节点 + 全量节点）以支持 AutoMerging；
   - 通过 `IngestionPipeline + DocstoreStrategy.UPSERTS` 做增量更新。
3. **检索** ([retriever.py](Knowledge/retriever.py))
   - `QueryFusionRetriever` 把原 query 改写成 4 路并行做 RRF 融合；
   - `AutoMergingRetriever` 把碎片叶子节点合并回大块上下文；
   - `RerankAPI`（httpx 调用 `rerank_base_url` + `rerank_model`）做最后 top-N 重排。


## 后台 COLLATOR

`CollationScheduler` 在 manager 每次工具调用结束后 `notify(thread_id, delta)` 累计活动消息数；累计满 `collation_turn_threshold`（默认 20）即并发触发 5 路整理：

| route | 行为 |
|---|---|
| `short` | 读 checkpoint，自动压缩**较旧一半**消息为 `ShortMemoryEntry`（含 issues / decisions / errors / resolutions），并把对应 message 标记为 `SUMMARY_MARKER` 占位。 |
| `long` | 从增量 transcript 抽取 `LongMemoryEntry`，再调 `collate_long_memory` 做插入 / 更新 / 删除 / 跳过的决策化整理。 |
| `project` | 把"目标-上一步-这一步-效果-达成"以一句话追加到 `Memory/projectKnow.md`；任务切换时整体重置。 |
| `skills` | 扫描本批次用过的工具，更新 `Skills/<tool>_skill.md` 的 `## 探索经验` 列表（add / update / replace / remove）。 |
| `skill_tree` | 从 `projectKnow.md` 提炼可复用技能，落到 `SkillTree/<category>/<name>.md`。 |

并发受 `collation_max_parallel` 控制，失败按 `collation_retry_count` 重试，日志写 `Logs/collation/<tid>.jsonl`。

## 快速开始

```bash
docker compose -f Docker/docker-compose.yaml up -d --build
```

## 调用预算

每个工具与代理都在 [config.yaml](config.yaml) 配 run（单次 invoke）/ thread（跨多轮）双层调用上限。预算见底时代理会主动收口、压缩、报告，不会无限循环。每条工具返回末尾都会附 `[Tool call X/N, remaining: R]` 提示剩余配额。

## 添加你自己的工具 / 技能

1. 在 [Tools/](Tools/) 写一个 `BaseTool` 子类（参考 [Tools/edit.py](Tools/edit.py)）：要么自带 `bump_budget` 限流，要么在 `config.yaml` 里加上 `<tool>_count_limit`。
2. 在 [Skills/](Skills/) 加一份 `<name>_skill.md`，frontmatter 写：
   ```yaml
   ---
   tool: <tool_name>
   description: 一句话描述（被 skill_library list 时展示）
   ---
   ```
   正文里留好 `## 探索经验` 的 fenced code block，COLLATOR 会自动累积经验条目。
3. 在对应代理的 `_*_TOOLS` 列表（manager 在 [Agents/manager.py](Agents/manager.py)，coder 在 [Agents/coder.py](Agents/coder.py) 的 `build_coder_agent`，依此类推）里挂上即可被调用。
4. 如果工具需要在 [agents_prompt.py](agents_prompt.py) 中显式管控（例如硬约束、调用顺序），同步补一段说明。

## 扩展 MCP 工具

[Tools/mcp.py](Tools/mcp.py) 内置了一个 FastMCP server + `MultiServerMCPClient`，可以把项目内部工具或外部 MCP server 都聚合给 manager。

- 在 [config.yaml](config.yaml) 的 `mcp_server` 配本机 MCP 的监听地址；
- 在 `extensions` 里追加 `{name, url, timeout}` 来挂外部 MCP（terminal / browser 等扩展）；
- 启动外部 MCP server 后，调用 `Tools.mcp.get_tools()` 即可拿到合并好的工具列表，再追加到对应代理的 `_*_TOOLS`。

## 许可

内部模板，未附 LICENSE。
