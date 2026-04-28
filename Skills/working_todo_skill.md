---
tool: working_todo
description: 维护 SessionDB/<thread_id>/workingTodo.md，承载 tasker_coder 当前 subtask 的"派单清单"（每条对应一次 dispatch_coder）；写权限归 tasker_coder，manager 只读
---

# working_todo Tool — SKILL.md

## 概览
`working_todo` 维护 `SessionDB/<thread_id>/workingTodo.md`，给 tasker_coder 当前正在执行的**单个** subtask 打底——把它进一步拆成 N 条**派单清单**（每条对应一次后续 `dispatch_coder` 调用），派完一条勾一条。文件按会话 thread_id 隔离，各会话互不影响。

**只反映"现在"，不留历史**：每次切到新的 subtask 都要 `clear` + `write_steps` 重写；**workingTodo.md 不是日志**，是当前活动的镜像。

```
plan.json (manager 视角，整个项目级)   workingTodo.md (tasker_coder 视角，当前 subtask 级)
├─ milestone m1                     # Current Working Todo
│   ├─ subtask m1-t1 (in_progress) ──→ > subtask_id: m1-t1
│   ├─ subtask m1-t2 (pending)         > description: 实现 csv 导出能力
│   └─ subtask m1-t3 (pending)         - [x] csv-exporter: 写 Tools/csv_exporter.py
├─ milestone m2                        - [x] cli-wire: 把 --format=csv 接到 cli.py
                                       - [ ] tests-csv: 写 3 条边界测试
                                       - [ ] verify: 跑 pytest 验证
```

---

## ⛔ 硬约束

1. **写权限归 tasker_coder，manager 只能 view** —— `manager` 实例化此工具时传 `read_only=True`，其它 action 会被拒绝；coder / tester / retriever / checker 等更下层子代理不接触此工具。
2. **每个 subtask 一份清单** —— tasker_coder 接到一个 subtask 就 `write_steps` 写入派单清单；下一次进入新 subtask 前 `clear`。**禁止**把多个 subtask 的步骤混塞在同一份清单里。
3. **`write_steps` 是覆盖式** —— 整文件替换，不是追加。所以 steps 必须一次列全（=你这一轮预计调几次 `dispatch_coder`）。
4. **mark_done 主要由框架自动调用** —— `dispatch_coder(step_index=N)` 返回子代理 `status=DONE` 时框架会自动 `mark_done(N)`，tasker_coder **不需要**手动勾；只在自动勾选失败 / 需要人工修正时才主动调 `mark_done`。
5. **产出 TaskerReport 之前调 `clear`** —— 避免本轮派单清单残留污染下一个 subtask。

---

## Actions

| action | 必填参数 | 作用 |
|---|---|---|
| `view` | — | 读取当前 workingTodo.md 全文；为空时返回提示语 |
| `write_steps` | `subtask_id`, `description`, `steps` (list[str]) | 用一份新 subtask 的步骤清单**覆盖**整个文件 |
| `mark_done` | `step_index` (1-based) | 把第 N 步 checkbox 改为 `[x]` |
| `clear` | — | 清空文件（写入空字符串） |

### 步骤的写法（影响可读性，请遵守）

- 每条 ≤ 80 字
- **格式：`<task_name>: <一句话目标>`** —— `task_name` 与你后续 `dispatch_coder` 时填的 `task_name` 严格对齐，方便对账
- **可独立勾选**（一条 = 一次 `dispatch_coder` 调用）
- 通常 1–6 条；单文件耦合任务可能只有 1 条，这是合法的；超过 6 条说明 subtask 拆得过细，回去合一些

---

## 典型工作流

### 1) 接到一个 subtask 时（**第一次 `dispatch_coder` 之前**）
```
action=write_steps
subtask_id=m1-t1
description=实现 csv 导出能力（Tools/csv_exporter.py + cli 接线 + 测试）
steps=[
  "csv-exporter: 写 Tools/csv_exporter.py，提供 to_csv(report) 接口（UTF-8 BOM）",
  "cli-wire: 在 cli.py 增加 --format=csv 分派到 csv_exporter.to_csv",
  "tests-csv: 写 3 条边界测试到 tests/test_csv_exporter.py",
  "verify: 跑 pytest tests/test_csv_exporter.py 全 pass"
]
```

### 2) 派发时带上 `step_index`（框架会自动勾选）
```
dispatch_coder(
  task_name="csv-exporter",
  task_prompt="...",
  step_index=1,
)
# 返回末尾会出现：[auto-mark] 已自动勾选 step 1。
```
独立子任务可以在同一条回复里并列发多条，每条不同 `step_index`。

### 3) 阶段性查看进度
```
action=view
```

### 4) 需要人工修正勾选（fallback）
只在下述情况才手动 `mark_done`：
- `dispatch_coder` 返回里出现 `[auto-mark] 失败：...` 或 `跳过：status=...`，你确认仅仅是 
  step_index 取错或者要人工接受“带疑虑的 DONE”；
- 补派了一个修复子任务后，原始 step 已被新派发覆盖勾选。

### 5) 全部步骤完成、即将产出 TaskerReport 之前
```
action=clear
```

---

## manager 视角（**只读**）

manager 在派出 `dispatch_tasker_coder` 之后想看进度，可以调：
```
action=view
```
返回的就是 tasker_coder 当前的派单清单 + 已勾选状态。试图 `write_steps` / `mark_done` / `clear` 会被工具拒绝并返回提示。

---

## 探索经验
```
1. 应该避免做"清单条数 ≠ dispatch_coder 调用次数"，否则 step_index 会错位、
   框架自动勾选也会错阶；应该让每条 step 严格对应一次 dispatch_coder。
2. 应该避免做"手动 mark_done 所有步骤"，除非自动勾选失败；并行派发时框架
   会按返回顺序逐个勾，manager 看到的进度才不会骗人。
3. 应该避免做"为了凑步数把单文件硬拆给多个 coder"，会因为同文件互相覆盖而冲突；
   单文件耦合任务清单 1 条即可，这是合法的、最常见的情况。
4. 应该避免做"为了推进手动 mark_done 一个未真正完成的 step"，勾上 = DONE 且核过
   verification；DONE_WITH_CONCERNS / NEEDS_CONTEXT / BLOCKED 不要勾。
```
