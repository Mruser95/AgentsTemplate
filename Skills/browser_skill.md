---
tool: browser
description: Playwright 浏览器自动化工具的使用约束与策略
---

# Browser Tool — SKILL.md

## 概览
`browser` 工具基于 Playwright 驱动一个**持久的 Chromium 会话**。
同一次会话里 cookie、登录态、当前页面都会保留，直到你主动 `close` 或调用次数耗尽。
**在调用前必须通读本文件的「硬约束」与「动作选择」两节。**

---

## ⛔ 硬约束（调用前必读）

### 1. 调用次数上限
每个会话有 `browser_count_limit` 次调用上限（见 `config.yaml`，返回值里带 `[Tool call X/N]`）。
**不要把探索性步骤（反复截图/取文本）用光预算**，先 `get_links` 拿结构再精准操作。

### 2. 域名白名单
`browser_allowed_domains` 非空时，`navigate` 只放行列表内的域名及其子域名。
被拒时返回 `Navigation denied: Domain '...' is not in the allowed list`。
**不得用编码、重定向服务等方式绕过白名单**；告知用户该域名受限即可。

### 3. 持久会话意味着副作用
同一个 page 会累积状态（登录、表单草稿、滚动位置）。
涉及**登录态变更、提交表单、下单等不可逆操作**前，必须：
- 先向用户确认
- 用 `screenshot` 或 `get_text` 验证当前页面上下文

### 4. 返回被截断是正常的
- `get_text` 超过 5000 字符会截断
- `get_html` 超过 8000 字符会截断
- `get_links` 最多返回 80 个元素
看到 `...[truncated]` 时，**用更精确的 `selector` 再取一次**，不要盲目重复。

---

## 🧭 动作选择（决策顺序）

遇到一个网页任务，按这个优先级选 action：

```
1. navigate      → 打开页面
2. get_links     → 先看页面上有哪些可交互元素（拿 selector！）
3. click/type    → 根据上一步拿到的 selector 精准操作
4. press_key     → 提交/取消/快捷键
5. get_text      → 读取结果（可带 selector 缩小范围）
6. eval_js       → 以上都搞不定时的"万能钥匙"
7. close         → 任务结束
```

**不要跳过第 2 步直接猜 CSS selector**——几乎一定会失败，白白消耗预算。

---

## 📚 动作速查表

| action | 必填参数 | 可选参数 | 说明 |
|---|---|---|---|
| `navigate` | `url` | — | 受 `browser_allowed_domains` 约束；等待 `domcontentloaded` |
| `click` | `selector` | — | 点击元素 |
| `type` | `selector`, `text` | — | 等价于 `page.fill`，**会清空原内容**再输入 |
| `press_key` | `text`（键名） | `selector` | 如 `"Enter"`、`"Escape"`；给 `selector` 会先聚焦该元素 |
| `get_text` | — | `selector`（默认 `body`） | 读可见文本 |
| `get_html` | — | `selector`（默认 `body`） | 读 innerHTML |
| `get_links` | — | `selector`（限定范围） | 返回可交互元素列表（含建议 selector、文本、href） |
| `screenshot` | — | — | 截当前视口，返回 base64（前 200 字符预览） |
| `scroll` | — | `direction`（`up`/`down`，默认 `down`） | 滚动 600px |
| `select` | `selector`, `text` | — | `<select>` 元素按 **label 文本**选择 |
| `wait` | `selector` | — | 等待元素出现，超时走 `browser_timeout` |
| `eval_js` | `text`（JS 表达式） | — | 任意 JS，返回值会 JSON 化 |
| `close` | — | — | 关闭浏览器，释放资源 |

---

## 📐 使用策略

### 第一步：先看再动
```
action=navigate, url=https://example.com
action=get_links                              # 看有哪些入口
action=click, selector="#login"               # 用上一步返回的 selector
```

❌ 反模式：navigate 后直接 `click` 猜 selector，大概率失败。

### 第二步：表单填写的正确姿势
```
action=type, selector="input[name=q]", text="playwright"
action=press_key, text="Enter", selector="input[name=q]"
action=wait, selector=".results"
action=get_text, selector=".results"
```
注意：`type` 会清空原内容，想**追加**要先 `get_text` 读出再拼接。

### 第三步及以后
- **结构化数据**：用 `eval_js` 批量 map，超 5000 字截断。
- **预算**：`remaining > 5` 正常；`≤ 3` 只做关键操作；`== 0` 立即停手。
- **结束必 `close`**（也计一次调用），否则占资源。

---

## 🔁 错误处理与常见坑

| 返回 / 情况 | 处理 |
|---|---|
| `Navigation denied` | 告知用户换站或放行；**勿绕路** |
| `TimeoutError` | `screenshot` 看页面；SPA 刚 navigate 就先 `wait` |
| `Unknown action` / 缺参数 | 对照动作速查表 |
| `...[truncated]` | 收窄 `selector` 重取 |
| `Tool call limit reached` | 停手，用已有信息答 |
| `eval_js: null` | 查表达式或先 `wait` |
| 动态 class 不稳定 | 用 `get_links` 的 `[name]`/`#id`/`[href]` |
| iframe / 新 tab | 不支持跨 iframe；单一 page 不切换 tab |
| 同会话多账号 | cookie 冲突；先 `close` 再重开 |

---

## ✅ 典型工作流示例

### 示例 1：搜索并取首条结果
```
1. action=navigate,  url=https://example.com
2. action=get_links
3. action=type,      selector="input[name=q]", text="LangChain"
4. action=press_key, text="Enter", selector="input[name=q]"
5. action=wait,      selector=".result"
6. action=get_text,  selector=".result:first-child"
7. action=close
```

### 示例 2：列表结构化 / 登录敏感
```
# 结构化：navigate → wait article → eval_js map(...).slice(0,10) → close
# 登录敏感：navigate → screenshot 确认账号 → ⏸ 用户确认 → 再 click/提交
```

---

## 📌 与其他工具的协作

- **terminal**：下载优先 `curl`（白名单内）；browser 不支持写文件，落盘走 terminal 或 edit。
- **tavily_search（默认先 tavily 后 browser）**：不知 URL / Answer 够用 / 静态摘要 → 不开 browser；SPA、登录、交互、`eval_js` → 必须 browser。白名单外被拒 → 用 Answer 或告知需放行，**勿绕路**。

---

## 探索经验
```
1. 应该避免做..., 否则会导致..., 应该做...
2. ...
```
