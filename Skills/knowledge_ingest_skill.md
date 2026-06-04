---
tool: knowledge_ingest
description: 将 chunk JSON/JSONL 文件批量灌入本地 pgvector 知识库
---

# Knowledge Ingest Tool — SKILL.md

## 概览
`knowledge_ingest` 把一批**已切好的 chunk 文件**批量 embed 后写入 `chunks` 表，供 `knowledge_search` 使用。
检索侧的使用规则、跟 `tavily_search` 的职责划分请看 `skill_library(tool_name="knowledge_search")`。

---

## ⛔ 硬约束（调用前必读）

### 1. 输入必须是已切好的 chunk 文件
支持两种格式：

- `.json`：顶层 list，元素是 dict
- `.jsonl`：一行一条 dict

每条 dict 形如 `{"content": str, "metadata": dict}`：`content` 落到 `content` 列，`metadata` 整体落到 `metadata` JSONB 列，其它 key 一律忽略。`metadata` 可省略或为空 dict。

```json
[
  {"content": "段落正文...", "metadata": {"source": "doc.md", "heading": "第 3 节"}},
  {"content": "另一段..."}
]
```

`content` 为空的元素会被**静默跳过**；所以看到 `Ingested 0 chunks` 的返回，**99% 是格式问题**。

### 2. 切分是上游的活，不要在 agent 运行时造 chunk
长度控制、重叠、清洗、去 HTML 标签都应该在 Knowledge 模块的离线 pipeline 里完成。
**不要在对话里用 `terminal` cat 一段文字、即兴写成 JSON 再 ingest** —— 这会让后续检索质量直接劣化。

### 3. 重复 ingest = 重复记录
表是 append-only；同一份文件 ingest 两次就会产生双倍 chunk。
想重建就先到 Postgres 里 `TRUNCATE chunks;`（不在本工具职责内），然后重新 ingest。

### 4. 一次 ingest 可能很慢
embedding 走本地 `BAAI/bge-m3`（batch=64），文件大时秒级到分钟级都可能。
**不要在同一轮里连续 ingest 多个大 glob** —— 拆成几次让进度可观测，出错也好定位。

### 5. 首次调用会下载模型
首次运行会从 HuggingFace 拉 `BAAI/bge-m3`，数 G。
告知用户冷启动会慢，不要以为 hang 住然后反复重试。

---

## 📐 使用姿势

### 输入参数
| 参数 | 类型 | 说明 |
|---|---|---|
| `pattern` | `str` | glob，可匹配多文件；如 `"Knowledge/chunks/*.json"` 或 `"Knowledge/chunks/spec_*.jsonl"` |

- 没匹配到 → 立即返回 `No files matched: ...`，不会写任何数据
- 匹配到但所有元素都缺 `content` → 返回 `Ingested 0 chunks ...`，这是格式告警

### 返回值
```
Ingested <added> chunks from <N> file(s). Store: <before> -> <after>.
```

- `after - before == added` 才算真正写入
- `added == 0` 一定要回去检查 chunk 文件格式（用 `terminal` 的 `head` 看一眼）

---

## 🔁 错误处理

| 返回内容 | 含义 | 处理方式 |
|---|---|---|
| `No files matched: ...` | glob 没命中 | 用 `terminal` 的 `ls` 核实路径后再调一次 |
| `Ingested 0 chunks ...` | 文件存在但元素全部被跳过 | 检查是否带 `content` 字段；看首条即可 |
| `knowledge_ingest failed: OperationalError` | Postgres 连不上 / pgvector 未装 | 让用户检查 `config.yaml` 的 `dsn` 与 `CREATE EXTENSION vector;` |
| `knowledge_ingest failed: json.JSONDecodeError` | 文件格式坏 | `terminal head` 定位错误行；修好再 ingest |
| `knowledge_ingest failed: <模型下载相关>` | HuggingFace 拉模型失败 | 检查网络 / 代理 / 本地缓存，不要反复重试 |

---

## ✅ 典型工作流

```
1. 离线 pipeline: 把文档切成 chunk  →  Knowledge/chunks/<name>.json
2. terminal: head -1 Knowledge/chunks/<name>.json   # 验证格式
3. knowledge_ingest: pattern="Knowledge/chunks/<name>.json"
4. knowledge_search: 用一个已知答案的 query 验证召回
```

不要在第 1 步没做完就跳到第 3 步 —— chunk 文件若是空 list 或字段不全，会得到 "Ingested 0 chunks" 的假成功。

### 示例：批量入库多个文件
```
1. terminal: ls Knowledge/chunks/
2. knowledge_ingest: pattern="Knowledge/chunks/spec_*.json"
3. 观察 added 数量是否与文件条数大致匹配
4. knowledge_search: "spec 文档里关于 XX 的说明"
```

### 示例：入库后验证召回
```
1. knowledge_ingest: pattern="Knowledge/chunks/onboarding.json"
2. knowledge_search: "新员工 onboarding 第一步要做什么"   # 用文档里确定存在的知识点
3. top-1 score 高 → 入库成功；No results → 检查 chunk 内容是否为空
```

---

## 📌 与其他工具的协作
- 检索侧 → `knowledge_search`
- 切分 / 预处理 → 项目自己的离线脚本，不属于本工具职责
- chunk 文件存放建议放在 `Knowledge/chunks/`，便于 glob 管理
- 验证格式 / 查看文件 → `terminal` 的 `ls` / `head`

---

## ❌ 反模式

| 反模式 | 后果 | 改用 |
|---|---|---|
| 运行时 cat 一段文字写 JSON ingest | 切分质量差，检索噪声大 | 走离线 pipeline |
| 同一文件 ingest 两次 | 重复 chunk，召回重复 | 重建前先 TRUNCATE |
| 不验证就连续 ingest 大 glob | 出错难定位 | 逐文件 ingest + 验证 |
| ingest 完不 search 验证 | 假成功（0 chunks）未发现 | 用已知 query 测召回 |
| metadata 塞无关大字段 | embed 慢、存储膨胀 | metadata 只放 source/heading 等轻量信息 |

---

## 💡 metadata 建议

`metadata` 会原样存入 JSONB，检索结果里可引用，建议包含：
- `source`：原始文档路径或 URL
- `heading` / `section`：章节标题，方便引用
- `version` / `date`：文档版本或更新时间（如有）

不要在 metadata 里塞整篇原文——正文已在 `content` 字段。

---

## 探索经验
```
1. 应该避免做..., 否则会导致..., 应该做...
2. ...
```
