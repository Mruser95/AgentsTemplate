---
tool: working_todo
description: 维护 SessionDB/<thread_id>/workingTodo.md，承载"当前正在执行的 plan subtask"的步骤清单（markdown checkbox）
---

# working_todo Tool — SKILL.md

## 概览
`working_todo` 维护 `SessionDB/<thread_id>/workingTodo.md`，给当前正在执行的**单个** subtask 打底——把它进一步拆成 N 个可勾选的执行步骤，并在执行过程中实时勾掉。文件按会话 thread_id 隔离，各会话互不影响。

**只反映"现在"，不留历史**：每次切到新的 subtask 都要 `clear` + `write_steps` 重写；**workingTodo.md 不是日志**，是当前活动的镜像。

```
plan.json (整个项目级)              workingTodo.md (当前 subtask 级)
├─ milestone m1                     # Current Working Todo
│   ├─ subtask m1-t1 (in_progress) ──→ > subtask_id: m1-t1
│   ├─ subtask m1-t2 (pending)         > description: 实现 to_csv
│   └─ subtask m1-t3 (pending)         - [x] 阅读 Tools/report.py 字段
├─ milestone m2                        - [x] 创建 csv_exporter.py 骨架
                                       - [ ] 实现 to_csv 主体
                                       - [ ] 写 3 条边界测试
                                       - [ ] 跑 pytest 验证
```

---

## ⛔ 硬约束

1. **只有 manager 能用** —— 子代理（coder/tester/retriever/checker）不需要也不应使用。
2. **每个 subtask 一份清单** —— 切换 subtask 之前必须 `clear`，然后 `write_steps` 写入新的；**禁止**把多个 subtask 的步骤混塞在同一份清单里。
3. **`write_steps` 是覆盖式** —— 整文件替换，不是追加。所以 steps 必须一次列全。
4. **每完成一步立即 `mark_done`** —— 不要积攒多步再统一勾。理由：勾选是给"未来的自己 / 用户"看进度的，滞后勾相当于撒谎。
5. **subtask 通过 plan_io.update_subtask_status='done' 标完成后，下一个 subtask 开工前调 `clear`**——避免上一个 subtask 的步骤残留误导。

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
- **动词开头**（"阅读..." / "创建..." / "实现..." / "运行..."）
- **可独立勾选**（不要写"实现 + 测试"这种半步骤组合）
- 通常 3–7 步；少于 3 步说明 subtask 拆得太粗，多于 7 步说明 subtask 拆得太细

---

## 典型工作流

### 1) 切到新 subtask 时
```
action=clear
```
然后：
```
action=write_steps
subtask_id=m1-t1
description=实现 Tools/csv_exporter.py 的 to_csv 函数
steps=[
  "阅读 Tools/report.py 的 Report 字段签名",
  "创建 Tools/csv_exporter.py 骨架（导入 + 函数签名）",
  "实现 to_csv 主体逻辑（含 UTF-8 BOM）",
  "写 3 条边界测试到 tests/test_csv_exporter.py",
  "运行 pytest 验证全部通过"
]
```

### 2) 执行过程中每完成一步
```
action=mark_done
step_index=1
```
（紧接着派发 subagent 做第 2 步……）

### 3) 阶段性查看进度
```
action=view
```

### 4) subtask 完成（plan_io.update_subtask_status='done' 之后）
```
action=clear
```
（然后写下一个 subtask 的 steps）

---

## 探索经验
```
1. 应该避免做"一次写入 20 条小步骤当备忘录用"，否则会让 todo 退化成 to-implement
   清单、勾选失去意义；应该把过细的拆分留在 plan.json 的 subtask 层，
   workingTodo.md 只承载"当下这个 subtask 的 3-7 步执行流"。
2. 应该避免做"完成一批步骤后再统一 mark_done"，否则进度永远落后真实状态、
   被中断后无法知道自己做到哪了；应该每完成一步立即 mark_done。
3. 应该避免做"切换 subtask 时只 write_steps 不 clear"，工具是覆盖式的所以
   行为上不会出错，但显式 clear 让"当前清单已过时"的语义更清楚。
```
