---
tool: schedule
description: 创建 / 列出 / 删除 / 回看定时任务；到点时启动新进程并把上下文交给 manager 恢复会话状态
---

# Schedule Tool — SKILL.md

## 概览
`schedule` 让 **manager** 预约**未来的自己**。它不是"到点跑一条 shell 命令"这么简单 —— 它让系统调度器在到点时：

1. 启动一个**全新的 Python 进程**；
2. 该进程读回登记时写下的元数据（含**制定任务时的 JSON 上下文**）；
3. 实例化 `Agents/manager.py` 里的 `manager_agent`（**固定**，本工程里唯一被允许执行定时任务的 agent）；
4. 以一条带上下文的 `HumanMessage` 把 manager 叫醒：
   > 【定时任务 · 每日磁盘检查】
   > 你（manager）在 2026-04-21T09:15:22 登记了这条定时任务，现在到点被自动唤醒。
   > 发起者：user（用户明确要求 manager 制定）
   > 原始意图：检查 C: 剩余空间并总结。
   > 制定任务时记录下来的会话上下文：`{"background": "...", "purpose": "...", "constraints": [...]}`

manager 到点醒来时，既知道**为什么被唤醒、要干什么**，也能**恢复制定任务时的会话状态**继续往下做。

底层依然用 OS 调度器（Windows `schtasks` / Linux `crontab`），因此**进程重启、机器重启后依然有效**。

---

## 硬性约束：执行者与发起者

- **执行者固定为 manager**：`Tools/schedule.py` 的 runner 段永远 `from Agents.manager import manager_session` 来执行到点任务。其他 agent（coder / checker / …）**不允许**被 schedule 直接唤醒。
- **`schedule` 工具只挂在 manager 的工具表里**。其他 agent 不得调用 schedule。
- **发起者 `creator` 只有三种合法取值**：
  - `user`：**用户明确要求** manager 制定这条定时任务。
  - `agent`：manager 在会话中**自主判断**需要制定。
  - `unknown`：无法判断来源 / 不想下定论。

  其他取值会被 `create` 拒绝。

---

## Actions

| action | 必填参数 | 作用 |
|---|---|---|
| `create`  | `name`、`intent`、`time`、`creator`（可选 `context`） | 预约一个定时唤醒 |
| `list`    | —                                                     | 列出系统调度器里的所有任务 + 本项目登记的 agent 任务 |
| `delete`  | `name`                                                | 删除任务 + 清理元数据 / 包装脚本 |
| `history` | `name`                                                | 回看该任务最近 5 次的执行日志（OK / 错误都在里面） |

### 参数说明

- **`intent`**：**自然语言**描述到点要做的事，这段话会原样塞进未来 manager 的第一条消息里。越具体越好。
  - 好：`"检查 C: 剩余空间，若低于 10GB 则列出最大的 5 个目录"`
  - 差：`"跑每日检查"`（醒来的 manager 根本不知道检查什么）
- **`time`**：
  - Windows `schtasks` → `HH:MM`（24 小时制），如 `"09:00"`
  - Linux/macOS `crontab` → 标准 5 段 cron，如 `"0 9 * * *"`
- **`creator`**：`"user"` / `"agent"` / `"unknown"` 三选一，**必填**。只表示「谁发起了这条任务的制定」，不表示谁去执行（执行者永远是 manager）。
- **`context`**：**JSON 字符串**，承载**制定任务时的会话上下文**——背景、目的、关键事实、不得违反的约束。到点时它会和 `intent` 一起塞给 manager，用于"恢复制定任务时的会话状态"。
  - 例：`'{"background":"用户最近在排查 C 盘容量告急","purpose":"每日自检并告警","constraints":["不得自动清理文件"]}'`
  - 不是合法 JSON 时会被落盘为 `{"raw": "<原文>"}`，manager 到点仍能读到，但**结构化信息会丢**。强烈建议写成合法 JSON。
  - 可选；但若不填，manager 到点时只有一行 `intent`，会话状态几乎丢失——**除非任务语义真的自包含，否则必填**。

---

## ⛔ 硬约束

1. **有副作用**：`create` / `delete` 会真的改系统调度器。参数核对后再调。
2. **`name` 要干净**：不要含 `"`、`/`、`|` 等 shell 特殊字符 —— 命令是拼接生成的。
3. **manager 必须可 import**：`Agents/manager.py` 必须导出 `manager_agent`，否则到点才会失败，事后 manager 不在场。
4. **权限**：Windows 对系统级任务需要管理员；失败会返回 `Access is denied`。
5. **不是秒级调度**：每日粒度。需要每分钟 / 每小时，本工具不支持。
6. **冷启动开销**：每次触发是全新进程，要重新加载模型、连 API。一天几次没事，高频不行。
7. **异步失败不自愈**：到点 manager 挂了你不在场。`.schedule/<task_id>/*.log` 里会记录栈，下次对话**主动 `history` 回看是个好习惯**。
8. **`creator` 不得伪造**：不知道就填 `unknown`。禁止为"显得规范"随便填 `user` / `agent`。

---

## 典型工作流

### 1) 用户明确要求 manager 制定
```
action=create
name=daily-disk-check
intent=检查 C: 盘剩余空间，若低于 10GB 则用 terminal 找出最大的 5 个目录并列出
time=09:00        # Windows；Linux 用 "0 9 * * *"
creator=user
context={"background":"用户今天提到 C 盘快满了","purpose":"每日自检","constraints":["不得自动清理文件"]}
```
返回 `[created id=ab12cd34, creator=user, executor=manager]` 说明登记成功。

### 2) manager 自主决定制定
```
action=create
name=weekly-log-summary
intent=...
time=0 9 * * 1
creator=agent
context={"background":"本次会话中用户反复要求追踪 error 日志，manager 判断值得每周自动汇总","purpose":"周一早会前一小时给出日志摘要"}
```

### 3) 来源不明（不要伪造）
```
action=create
...
creator=unknown
context={"note":"从历史消息无法确认是用户要求还是 manager 自己提出的"}
```

### 4) 确认创建成功
```
action=list
```
会同时看到：
- 系统调度器里的原生任务（schtasks/crontab 原样输出）
- 本项目登记的 agent 任务（含 id / creator / executor / time / intent 摘要）

### 5) 不再需要时清理
```
action=delete
name=daily-disk-check
```
`.schedule/<id>.json` 和 `.schedule/<id>.bat|sh` 也会被一并删除。

### 6) 回看过去的执行结果
```
action=history
name=daily-disk-check
```
返回最近 5 次 log。出错时会有 `[ERR]` 前缀 + Python traceback。

---

## 返回值
- `create`：`[created id=..., creator=..., executor=manager]` + `schtasks`/`crontab` 的 stdout + stderr。
- `delete`：透传 `schtasks` / `crontab` 的 stdout + stderr。
- `list`：系统调度器原文 + 本项目任务一览（含 `by=<creator>` 与 `exec=manager`）。
- `history`：近 5 次 log 拼接。

---

## 实现细节（出问题时排查用）

- 元数据：`.schedule/<task_id>.json` —— 含 `id / name / intent / time / creator / executor / context / created_at`。`executor` 固定为 `"manager"`。
- 包装脚本：`.schedule/<task_id>.bat`（Win）或 `.sh`（Linux）—— 只做 `cd` + `python -m Tools.schedule --task <id>`。用包装脚本是为了躲开 `schtasks /TR` 的嵌套引号规则。
- 运行日志：`.schedule/<task_id>/<yyyymmdd_HHMMSS>.log` —— 运行器执行完 / 挂掉都会写。
- 运行器：`Tools/schedule.py` 的「模块 3：Runner」段 —— **固定** `from Agents.manager import manager_session`，把 `intent + context` 拼成一条 `HumanMessage` 塞给它。不再根据 creator 字段动态 import 任何 agent。

---

## 探索经验
```
1. 应该避免做..., 否则会导致..., 应该做...
2. ...
```
