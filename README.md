# AgentsTemplate

一个基于 LangGraph + LangChain 的多代理项目执行框架。`manager` 把自然语言需求拆成 `plan.json`，按拓扑顺序派发给专职子代理（`tasker_coder` / `coder` / `tester` / `retriver` 等）执行；每个 subtask 完成时由 `checker` 做硬门评估，全过程通过 SQLite checkpoint 持久化。

## 架构

```
┌─────────────┐  HTTP/SSE  ┌─────────────┐
│  WebUI      │ ─────────► │ agent_api   │  FastAPI 入口（/chat /history /files ...）
└─────────────┘            └──────┬──────┘
                                  │
                                  ▼
                          ┌───────────────┐
                          │ Agents/manager│  规划 / 派发 / 验收（唯一持久化代理）
                          └──────┬────────┘
              ┌──────────┬───────┼──────────┬──────────┐
              ▼          ▼       ▼          ▼          ▼
        tasker_coder  coder   tester   retriver    manager_self
        (多模块代码) (单文件) (用例)   (多源调研)  (terminal/tavily/browser/schedule)
                                  │
                                  ▼
                          ┌───────────────┐
                          │ Agents/checker│  每次 subtask done 自动触发的 hard gate
                          └───────────────┘

Tools/   plan · todo · terminal · browser · tavily · schedule · skills · linter · edit · mcp
Memory/  shortMem (会话压缩) · longMem (跨会话偏好/事实) · projectKnow.md
Knowledge/  PG + pgvector + BM25 混合检索（refresh_from_agent / retriever）
SessionDB/  checkpoints.db（LangGraph）+ thread_<id>/{plan.json, workingTodo.md, workspace/}
Skills/  各工具的 SKILL.md，子代理首次调用前必须 skill_library 加载
```

关键约定：
- **plan.json 是唯一可信事实源**：所有阶段决定都落 plan，不靠临时记忆。
- **checker hard gate**：`plan` 在 subtask 标记 done 时自动调 `checker`，输出 `on_track / minor_drift / major_drift / off_track`。
- **TestDatasets.json 用例覆盖硬约束**：派过 tester 的任务，验收 subtask 必须逐一执行所有 cases 并按 `judgment_criteria` 判 pass/fail（详见 [agents_prompt.py](agents_prompt.py) 中 `MANAGER_TESTER_POLICY`）。
- **每个会话隔离 workspace**：子代理的文件读写都在 `SessionDB/thread_<id>/workspace/` 下。

## 目录速查

| 路径 | 作用 |
|---|---|
| [agent_api.py](agent_api.py) | FastAPI 入口，提供 `/chat /history /files /download /health` |
| [agents_prompt.py](agents_prompt.py) | 所有代理的 system prompt 与硬规则 |
| [config.yaml](config.yaml) | 工具预算、模型限速、MCP / Knowledge 配置 |
| [Agents/](Agents/) | manager / checker / tester / coder / tasker_coder / retriver |
| [Memory/](Memory/) | 长 / 短期记忆存储 + 整理 agent (`mem_agent.py`) |
| [SkillTree/](SkillTree/) | skill 经验沉淀 agent (`skill_agent.py`) |
| [schedule.py](schedule.py) | 后台 collation scheduler，定期调度记忆 / skill 整理 |
| [Tools/](Tools/) | 给代理用的工具实现 |
| [Skills/](Skills/) | 工具规范文档（被 skill_library 加载） |
| [WebUI/](WebUI/) | 静态前端（vanilla JS），由 agent_api 直接托管 |
| [Knowledge/](Knowledge/) | 项目知识库的 ingest 与检索 |

## 快速开始

### 1. 环境准备

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium    # 如果会用到 browser 工具
```

### 2. 配置 `.env`

在项目根新建 `.env`：

```dotenv
agent_llm_model=...
agent_llm_key=...
agent_llm_base_url=...
TAVILY_API_KEY=...
API_KEY=local-dev               # 可选；设了就要在请求头带 X-API-Key
```

需要知识库时再启 PostgreSQL + pgvector，并按 [config.yaml](config.yaml) 的 `dsn` 填好。

### 3. 启动服务

```bash
uvicorn agent_api:app --host 0.0.0.0 --port 8000
```

打开 `http://localhost:8000/` 进入 WebUI，或直接调用 API：

```bash
# 新对话（thread_id 自取）
curl -N -X POST http://localhost:8000/chat/thread_demo_001 \
  -H "X-API-Key: local-dev" \
  -H "Content-Type: application/json" \
  -d '{"message": "帮我写一个抓取网页图片的脚本"}'
```

`/chat` 返回 SSE 事件流；产物落在 `SessionDB/thread_demo_001/workspace/`。

### 4. （可选）Docker

```bash
docker compose -f Docker/docker-compose.yaml up --build
```

## 工作流

1. **Drafting**：manager 澄清需求 → 写 `plan.json` → 自审 → 等用户确认。
2. **Ready**：用户说"开始"才进入执行。
3. **Executing**：按 milestone/subtask 顺序派发；每个 subtask 完成读 checker gate；失败按"诊断 → 重派"自救最多 3 轮，仍不过才标 blocked。
4. 全 plan done 后压缩短期记忆并向用户交付。

## 调用预算

所有工具与代理都在 [config.yaml](config.yaml) 配 run/thread 双层调用上限。预算见底时代理会主动收口、压缩、报告。

## 许可

内部模板，未附 LICENSE。
