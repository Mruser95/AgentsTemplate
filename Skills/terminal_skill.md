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

---

## 探索经验
```
1. 应该避免做..., 否则会导致..., 应该做...
2. ...
...
```