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
| `press_key` | `text`（键名） | `selector` | 如 `"Enter"`、`"Escape"`、`"Control+A"`；给 `selector` 会先聚焦该元素 |
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

❌ 反模式：
```
action=navigate, url=...
action=click, selector="button.login-btn"     # 猜 selector，大概率失败
```

### 第二步：表单填写的正确姿势
```
action=type, selector="input[name=q]", text="playwright"
action=press_key, text="Enter", selector="input[name=q]"
action=wait, selector=".results"
action=get_text, selector=".results"
```

注意：`type` 会清空原内容，想**追加**要先 `get_text` 读出再拼接。

### 第三步：需要结构化数据时用 eval_js
```
# 抓所有文章标题 + 链接
action=eval_js, text="Array.from(document.querySelectorAll('article h2 a')).map(a => ({title: a.innerText, href: a.href}))"
```

结果会被 JSON 化返回，超过 5000 字符会截断。

### 第四步：盯住剩余预算
返回里的 `[Tool call X/N, remaining: K]`：
- `remaining > 5` → 正常探索
- `remaining ≤ 3` → 只做关键操作，准备收尾
- `remaining == 0` → **立即停手**，直接用已有信息回答

### 第五步：任务结束 close
完成后调用 `action=close`，否则浏览器会一直占着。
`close` 本身也计一次调用。

---

## 🔁 错误处理

| 返回内容 | 含义 | 处理方式 |
|---|---|---|
| `Navigation denied: Domain '...'` | 域名白名单拦截 | 告知用户，换站点或让用户放行 |
| `Browser action 'X' failed: TimeoutError` | 等待元素超时（默认 30s） | 先 `screenshot` 看页面状态，可能 selector 错了或页面还在加载 |
| `Unknown action: X` | 动作名拼错 | 对照本文动作速查表 |
| `press_key requires 'text'` | 没传键名 | 补上 `text="Enter"` 之类 |
| `...[truncated]` | 返回被截断 | 用更窄的 `selector` 再取 |
| `Tool call limit reached` | 次数耗尽 | 立即停止调用，基于已有信息回答 |
| `eval_js result: null` / `undefined` | JS 没拿到数据 | 检查表达式、或页面还没加载完，先 `wait` |

---

## ⚠️ 常见坑

1. **SPA 页面刚 navigate 完，DOM 还没渲染完** → `navigate` 后如果立刻 `click` 失败，先 `wait` 关键元素。
2. **selector 带动态 class**（如 `css-a8xz9f`）→ 换成 `get_links` 拿到的 `[name=...]` / `#id` / `[href=...]` 稳定得多。
3. **iframe 内容拿不到** → 当前工具**不支持跨 iframe 操作**。遇到 iframe 要用 `eval_js` + `contentWindow.document` 兜底，或告知用户该场景受限。
4. **下载、新标签页、弹窗** → 当前工具绑定在**单一 page** 上，新开的 tab 不会自动切换。能避则避。
5. **输入中文/特殊字符** → `type` 可以，`press_key` 只接受键名不是字符；要按单个字符用 `page.keyboard.type`（目前未暴露）。
6. **登录态在会话内有效** → 不要在同一会话里"登录 A 账号 → 登录 B 账号"，cookie 会打架；需要时先 `close` 再重开。

---

## ✅ 典型工作流示例

### 示例 1：搜索并取首条结果摘要
```
1. action=navigate,  url=https://example.com
2. action=get_links                                    # 找到搜索框 selector
3. action=type,      selector="input[name=q]", text="LangChain"
4. action=press_key, text="Enter", selector="input[name=q]"
5. action=wait,      selector=".result"
6. action=get_text,  selector=".result:first-child"
7. action=close
```

### 示例 2：抓一个列表页的结构化数据
```
1. action=navigate, url=https://news.site/list
2. action=wait,     selector="article"
3. action=eval_js,  text="Array.from(document.querySelectorAll('article')).slice(0,10).map(a => ({title: a.querySelector('h2')?.innerText, url: a.querySelector('a')?.href}))"
4. action=close
```

### 示例 3：登录态敏感操作（必须先确认）
```
1. action=navigate, url=<后台页面>
2. action=screenshot                  # 让用户或自己确认当前账号
3. ⏸ 暂停，向用户确认是否继续
4. 确认后再执行点击/提交
```

---

## 📌 与 terminal 工具的协作
- 需要**下载文件**时，优先让 `terminal` 用 `curl`（若在白名单内），而不是在浏览器里硬拖。
- 需要**解析 JSON/HTML 后再处理**时，用 `eval_js` 在浏览器里就地提取，减少文本往返。
- 浏览器拿到的内容要落盘，当前 skill **不支持写文件**；通过 `terminal` 或专门的文件工具完成。

---

## 📌 与 tavily_search 工具的协作

`browser` 是重资源（每会话起 Chromium），`tavily_search` 是轻资源（API 调用）。**默认先 tavily 后 browser**，browser 只用在 tavily 够不到的地方。

### 什么时候**不**开 browser
- 还不知道目标 URL → 先 `tavily_search` 拿候选 URL。**不要**用 browser 打开搜索引擎手动搜，白白烧 `browser_count_limit`。
- tavily 的 `Answer` 已直接回答问题且来源权威 → 直接用，不要再开 browser 做"二次确认"。
- 只需要静态页面的正文摘要 → tavily 返回的 content 片段通常够用。

### 什么时候**只能**用 browser
- 页面是 SPA，内容由 JS 渲染（Tavily 抓的是快照，动态部分缺失）。
- 需要登录态 / cookie 才看得到的内容。
- 需要**交互**：点击、填表、`press_key`、滚动触发加载、截图验证。
- 需要在页面里**即时提取结构化数据**（用 `eval_js`），比来回粘贴正文更准。

### 白名单协同（`browser_allowed_domains` 非空时）
- tavily 搜回来的 URL 若不在白名单里，`navigate` 会直接被拒。
- **不要为此绕路**（镜像站、缓存、重定向服务都禁止）。正确做法：
  1. 用 tavily 的 `Answer` + 摘要回答；或
  2. 明确告诉调用方"该域名不在 `browser_allowed_domains`，需要放行"。
- 进入 browser 前最好先瞄一眼候选 URL 的域名，避免 `navigate` 之后才发现被拒。

### 典型组合
```
1. tavily_search: "<具体问题>"                    # 轻量发现
2. Answer / 摘要够用？ → 直接回答，不开 browser
3. 不够、且目标域名在白名单内：
   4. browser.navigate   url=<选中的 URL>
   5. browser.get_links                           # 看可交互元素
   6. browser.click / type / get_text / eval_js   # 精准操作
   7. browser.close
```

---

## 探索经验
```
1. 应该避免做..., 否则会导致..., 应该做...
2. ...
...
```
