---
tool: knowledge_search
description: 本地知识库（pgvector + BM25 + CrossEncoder rerank）混合检索的使用边界与查询策略
---

# Knowledge Search Tool — SKILL.md

## 概览
`knowledge_search` 在**本地 pgvector 知识库**里做 vector + BM25 + CrossEncoder 的混合检索。
配套工具 `knowledge_ingest` 负责把 chunk 文件灌入同一个库（见 `skill_library(tool_name="knowledge_ingest")`）。

| 工具 | 职责 | 一般频率 |
|---|---|---|
| `knowledge_ingest` | 把 chunk JSON/JSONL append 到 `chunks` 表 | 新数据到来时调用一次 |
| `knowledge_search` | 针对已入库内容做自然语言检索 | 每个相关问题 1 次 |

**首次调用会下载 embedding 与 reranker 模型（数 GB）并连 Postgres，冷启动可能很慢；不要在短超时内重复重试。**

---

## ⛔ 硬约束（调用前必读）

### 1. 先 ingest 后 search
库为空时 `knowledge_search` 返回 `No results.`。
如果你根本没做过 ingest 却反复搜，就是纯浪费调用次数 —— 先 `knowledge_ingest`，再查。

### 2. 不要用来代替网搜
`knowledge_search` 只能看到**已经 ingest 的内容**。
- 用户问公开 / 实时 / 互联网热点 → 走 `tavily_search`
- 用户问本仓库源码 / 配置 → 优先 `terminal`（`cat` / `grep`）
- 只有"我们内部文档里写过、且已切 chunk 入库"的问题才走 `knowledge_search`

### 3. 调用次数上限
每个会话有 `retrieve_call_limit` 次上限（见返回值 `[Tool call X/N]`）。
**不要把同一个问题改写成 5 个同义 query 分别搜** —— 一次写好 query，再根据结果决定是否补一次。

### 4. 不要自己造 chunk 喂给 ingest
chunk 切分（长度、重叠、清洗）是**离线的上游步骤**，由 Knowledge 模块之外的流程完成。
agent 运行时不要临时从 `terminal` 的输出里 cat 一段文字写成 JSON 再 ingest —— 切分策略会漂。

### 5. rerank 结果的 score 是相对量
- `score` 来自 CrossEncoder，**同一次查询内**的大小关系有意义
- **不同查询之间**的绝对阈值不通用，别拿 `score > 0.5` 作为硬判据
- 返回里 `[1]` 明显比 `[2]` 分高 → 认为 `[1]` 更可信；分差不大 → 综合看内容

---

## 📐 使用策略

### 何时该搜
- 私域文档 / 内部规范 / 团队 wiki 里的问题
- 用户明确说"我们内部的 …"、"项目文档里 …"
- 需要在多个候选方案之间找内部权威依据

### 何时不该搜
- 用户给的 prompt 已经包含答案
- 问题显然是"最新版本 / 公开资讯" → 走 tavily
- 问题是"代码里这个符号怎么用" → 直接 grep 源码

### 查询写法
用自然语言问完整问题，不要只甩关键词；中英混排 OK（jieba 负责中文分词）。

```
# ✅ 具体、含领域术语
"我们项目里 coder agent 的退出行为如何配置，默认值是什么"
"RAG pipeline 中文档切分用的是什么策略，chunk 大小"

# ❌ 太宽泛
"RAG"
"agent 怎么配"
```

### 读返回值
返回结构（已格式化）：
```
[1] id=42  score=0.7421
<chunk 内容，最多 500 字符>

[2] id=17  score=0.6123
...

[Tool call X/N, remaining: M]
```

- `id` 是库里的主键，跟用户沟通时可以引用
- `...[truncated]` 表示内容超过 500 字被截断 —— 降低 `k` 让单条展示更长，或改窄 query
- `remaining ≤ 1` 时立即停搜，用已有片段作答

---

## 🔁 错误处理

| 返回内容 | 含义 | 处理方式 |
|---|---|---|
| `No results.` | 库空 / query 命中率为 0 | 先确认是否已 ingest；再用更贴近文档措辞的 query 重写一次（最多 1 次） |
| `knowledge_search failed: OperationalError ...` | Postgres 连不上 / pgvector 未安装 | 告知用户检查 `config.yaml` 的 `dsn` 与 `CREATE EXTENSION vector;` |
| `knowledge_search failed: HFValidationError / ConnectionError` | HuggingFace 模型下载失败 | 提示用户配 `HF_ENDPOINT` 或本地离线模型路径，不要反复重试 |
| `Tool call limit reached` | 次数耗尽 | 停用 `knowledge_search`，基于现有片段回答 |
| `...[truncated]` | 单条内容被截断 | 降低 `k` 或重写更聚焦的 query |

---

## ✅ 典型工作流示例

### 示例 1：本地知识答题
```
1. knowledge_search: "coder agent 的 run_call_limit 默认值是多少，在哪里配置"
2. 观察 top-1 的 metadata.path / score
   - score 高且指向 config.yaml → 直接引用答题
   - score 普遍低 → terminal cat config.yaml 二次核对后再答
```

### 示例 2：先入库再查
```
1. terminal: ls Knowledge/chunks/   # 确认已切好 chunk
2. knowledge_ingest: "Knowledge/chunks/spec_v1.json"
3. knowledge_search: <带具体术语的 query>
```
不要把 2 和 3 的顺序颠倒 —— 没 ingest 就搜 = 一定 `No results.`。

### 示例 3：知识缺口的正确路径
```
用户：这个第三方库最新的 XX API 怎么用？

❌ knowledge_search: "XX API 最新用法"          # 库里根本没这个信息
✅ tavily_search:    "<library> <api> latest usage example"
```

---

## 📌 与其他工具的协作
- 公开互联网知识 → `tavily_search`（见其 skill）
- 本仓库源码 / 配置 → `terminal` 的 `cat` / `grep` / `ls`；或 overview 工具的 `grep`/`repo_map`
- 运行时新增数据到库 → `knowledge_ingest`（见其 skill）；切分本身不是它的职责

---

## ❌ 反模式

| 反模式 | 后果 | 改用 |
|---|---|---|
| 没 ingest 就连搜 | 浪费 retrieve 预算 | 先 ingest |
| 同义 query 连搜 5 次 | 预算耗尽 | 一次写好，最多补 1 次 |
| 用 score 绝对阈值判真伪 | 误判 | 同次查询内相对比较 |
| 运行时 cat 文字 ingest | 检索质量差 | 走离线切分 |
| 公开资讯走 knowledge_search | 一定 No results | 走 tavily |

---

## 探索经验
```
1. 应该避免做..., 否则会导致..., 应该做...
2. ...
```
