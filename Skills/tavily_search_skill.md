---
tool: tavily_search
description: Tavily 网络搜索的使用边界与查询策略
---

# Tavily Search Tool — SKILL.md

## 概览
`tavily_search` 工具用于在公开互联网上做 LLM 友好的搜索。
**只在本地代码 / 已有上下文无法回答的问题上使用它。**

---

## ⛔ 硬约束（调用前必读）

### 1. 调用次数上限
每个会话有 `tavily_count_limit` 次调用上限（见返回值 `[Tool call X/N]`）。
搜索是有外部成本的——**不要把它当 grep 用**。

### 2. 每次只查一件事
Tavily 接受单个 `query` 字符串；查询前先想清楚要解决的具体问题。
**不要把多个问题合并成一句模糊的 query**，得到的结果会同时变差。

### 3. 不要用它做事实背书
Tavily 抓取的是网页快照，可能过时或带广告。
**关键决策前，至少交叉两个来源**，或者直接读官方文档 URL。

### 4. 该用本地工具时别用搜索
- 找仓库里的符号 → 用 `terminal` 的 `grep` / `find`
- 看本地文件内容 → 用 `terminal` 的 `cat`
- 验证某个包是否安装 → 用 `terminal` 的 `pip show` / `python -c`

---

## 📐 使用策略

### 何时该搜
- 不熟悉的第三方库 API、版本差异、deprecation 公告
- 报错信息里有专有名词，本地搜不到
- 需要最新的语言特性 / 框架行为
- 用户明确要求"找一下最新的 …"

### 何时不该搜
- 用户的需求已经在 prompt + 本地文件里讲清楚
- 你已经知道答案，只是想"再确认一下" → 这是 token 浪费
- 想看代码示例但仓库 `examples/` 里就有 → 先看本地

### 查询写法

```
# ✅ 具体、含版本/语言/错误码
"langchain create_agent system_prompt vs prompt 1.x migration"
"playwright python async page.goto wait_until options"
"ImportError: cannot import name 'X' from 'langchain.agents'"

# ❌ 太宽泛，结果会很水
"langchain agent"
"how to use python"
"playwright bug"
```

### 读返回值
返回结构（已格式化）：
```
Answer: <Tavily 合成的简短回答，可能为空>

[1] <title>
<url>
<content snippet, 截断到 800 字符>

[2] ...

[Tool call X/N, remaining: M]
```

- 如果 `Answer:` 已经回答了你的问题且来源 URL 看起来权威 → 直接用，不用再查。
- 如果 `Answer` 不靠谱 / 含糊 → 看 `[1] [2]` 的 url 域名，挑权威源（官方 docs、GitHub release、PEP 等）。
- 当 `remaining ≤ 1` 时，**停止搜索，用已有信息编码或如实告知用户知识缺口**。

---

## 🔁 错误处理

| 返回内容 | 含义 | 处理方式 |
|---|---|---|
| `TAVILY_API_KEY is not set` | 环境变量缺失 | 告诉用户在 `.env` 加 `TAVILY_API_KEY=...`，不要重试 |
| `Tavily search failed: ...` | 网络/限流/上游错误 | 同一 query 最多重试 1 次；仍失败就改用其他思路 |
| `Tool call limit reached` | 次数耗尽 | 立即停止搜索，用已有信息回答 |
| `No results.` | 关键词太冷门 | 换更通用的措辞重写 query；最多再查 1 次 |

---

## ✅ 典型工作流示例

**任务：实现 X 库的 Y 功能，但你不确定最新 API**

```
1. terminal: ls / cat 目标文件，确认现有代码风格
2. tavily_search: "X library Y feature usage example latest"
3. （可选）tavily_search: "X library Y migration v1 to v2"  # 只在 step 2 不够时
4. 写代码、用 terminal 验证
```

不要颠倒：先 search 再读本地代码 = 大概率写出和现有风格不一致的实现。

---

## 📌 与 browser 工具的协作

`tavily_search` 是**发现层**，`browser` 是**深入层**，两者不要混用。

### 分工
- **先 tavily 后 browser**：不知道目标 URL 时，用 `tavily_search` 定位候选 URL 和摘要；有必要再 `browser.navigate` 过去深读。**不要**直接用 browser 打开搜索引擎手动搜——浪费 browser 预算。
- **Answer 够用就收手**：Tavily 返回 `Answer:` 已回答问题且来源权威时，**不要**再开 browser 验证一遍（Playwright 是重资源，每次会话都会起 Chromium）。
- **只有 browser 能做的事**：JS 渲染的 SPA、需要登录态、需要点击 / 填表 / 截图——这些场景 Tavily 抓的快照一定缺数据，直接走 browser。

### 白名单协同（`browser_allowed_domains` 非空时）
- Tavily 搜回来的 URL 若不在 `browser_allowed_domains` 里，`browser.navigate` 会被拒 (`Navigation denied`)。
- 此时的正确反应：**用 tavily 的 `Answer` + 摘要回答用户**，或明确告知"该域名不在白名单，需要放行"。**不要**尝试镜像站、缓存、重定向服务绕过。
- 若你预期后续一定要交互，应先主动检查候选 URL 的域名，避免搜完才发现都打不开。

### 典型组合
```
1. tavily_search: "<要查的事>"       # 发现 URL + 看 Answer
2. Answer 够用？     → 直接回答，不开 browser
3. 需要完整内容？     → browser.navigate + get_text
4. 需要交互 / 登录？  → browser.get_links → click / type ...
```

---

## ❌ 反模式

| 反模式 | 后果 | 改用 |
|---|---|---|
| 本地 grep 能解决的还去搜 | 浪费 tavily 预算 | 先 terminal/overview |
| 多问题合并成一句 query | 结果水 | 每次只查一件事 |
| Answer 够用还开 browser | 烧 browser 预算 | 直接回答 |
| 关键决策只信一个 snippet | 可能过时/错误 | 交叉两源或读官方 docs |
| 白名单外 URL 尝试绕路 | 违规 | 用 Answer 或告知用户 |

---

## 探索经验
```
1. 应该避免做..., 否则会导致..., 应该做...
2. ...
```
