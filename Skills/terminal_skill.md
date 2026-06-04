---
tool: terminal
description: shell 命令执行的安全约束与使用策略
---

# SafeShell Tool — SKILL.md

## 概览
`terminal` 工具允许你在受限沙箱中执行 shell 命令。
**在调用前必须通读本文件的「硬约束」部分。**

---

## ⛔ 硬约束（调用前必读）

### 1. 命令白名单
只有 `config.yaml` 中 `shell_permissions` 列出的命令才被允许。  
调用前请先确认所需命令是否在白名单中；若不确定，**优先用最小命令试探**。

常见被拒绝的模式：
- `curl | bash`、`$(subshell)` 等命令替换
- `rm -rf`、`chmod 777` 等危险操作
- 白名单外的任何二进制

### 2. 调用次数上限
每个会话有 `shell_count_limit` 次调用上限（见返回值中的 `[Tool call X/N]`）。  
**不得为了绕过限制而将多条命令合并成一条复杂管道。**

### 3. LLM 二次审查
即使通过白名单，命令还会经过一个 checker agent 审查。  
意图不明确或副作用不可控的命令会被拒绝。

---

## 📐 使用策略（渐进式）

### 第一步：先探查，再操作
```bash
# ✅ 先确认环境
ls /
python --version

# ❌ 不要上来就写文件、安装依赖
pip install xxx && python run.py
```

### 第二步：单条命令优先，必要时才串联
```bash
# ✅ 拆分执行，便于定位错误
cd /project
ls -la

# ⚠️ 仅在步骤逻辑强依赖时才使用 &&
cd /project && python main.py
```

### 第三步：读取返回值中的剩余次数
每次调用返回格式：
```
<命令输出>

[Tool call 2/10, remaining: 8]
```
当 remaining ≤ 2 时，**停止探索性调用，直接用已有信息回答用户**。

### 第四步：遇到拒绝，不要绕过
返回 `Command denied` 时：
- 向用户说明该命令受限
- 提供**不需要该命令**的替代方案
- 不得尝试用编码、路径变换等方式绕过白名单

---

## 🔁 错误处理

| 返回内容 | 含义 | 处理方式 |
|---|---|---|
| `Command denied, contains unauthorized commands` | 白名单拦截 | 告知用户，换思路 |
| `Command denied by checker agent` | LLM 审查拒绝 | 简化命令意图后重试一次 |
| `[exit=1]` + stderr | 命令执行出错 | 读 stderr，修正后重试 |
| `Tool call limit reached` | 次数耗尽 | 立即停止工具调用，直接回答 |
| `Command timed out` | 超时（默认 30s） | 拆分任务或告知用户 |

---

## ✅ 典型工作流示例

**任务：检查项目依赖并运行测试**
```
1. ls /project               # 确认目录结构
2. cat requirements.txt      # 读取依赖列表（不执行安装）
3. python -m pytest tests/   # 运行测试
```
不要跳过第 1、2 步直接执行第 3 步。

**任务：定位源码中的符号**
```
1. grep -rn "class Manager" Agents/    # 或用 overview 工具的 grep
2. sed -n '1,50p' Agents/manager.py    # 只看头部
3. python -m py_compile Agents/manager.py  # 改完后验证
```

**任务：验证 ingest 文件格式**
```
1. ls Knowledge/chunks/
2. head -3 Knowledge/chunks/spec.json
3. python -c "import json; print(len(json.load(open('Knowledge/chunks/spec.json'))))"
```

---

## 📌 与其他工具的协作

| 场景 | 优先工具 | terminal 的角色 |
|---|---|---|
| 找符号 / 文件名 | `grep` / `glob` / `repo_map` | 仅在 overview 工具不够时用 |
| 写 / 改文件 | `edit` | terminal 只用于 `cat`/`sed -n` 定位，不用重定向写文件 |
| 公开资讯 | `tavily_search` | terminal 的 curl 仅在白名单内且需原始响应时用 |
| 内部文档 | `knowledge_search` | terminal 用于查看 chunk 文件、验证 ingest |
| 浏览器下载 | `browser` | 下载文件优先 terminal `curl`（白名单内） |

---

## ❌ 反模式

| 反模式 | 改用 |
|---|---|
| `cat > file.py << EOF` 写文件 | `edit(mode='create')` |
| `sed -i` 改文件 | `edit(mode='str_replace')` |
| 5 条独立命令拼成一条管道 | 拆分执行，便于定位错误 |
| 次数快用完还继续探索 | remaining ≤ 2 时停手 |
| 白名单被拒后换编码绕过 | 告知用户，换方案 |
| overview 能做的事用 terminal rg | 用 `grep`/`glob`/`repo_map` |

---

## 💡 常见命令场景

| 目的 | 示例 | 注意 |
|---|---|---|
| 看文件片段 | `sed -n '10,30p' file.py` | 定位用，不改文件 |
| 语法检查 | `python -m py_compile file.py` | edit 后必做 |
| 跑测试 | `python -m pytest tests/ -x` | `-x` 遇错即停 |
| 看 chunk 格式 | `head -3 Knowledge/chunks/x.json` | ingest 前验证 |
| 查包版本 | `pip show langchain` | 确认依赖，非安装 |

---

## 探索经验
```
1. 应该避免做..., 否则会导致..., 应该做...
2. ...
```
