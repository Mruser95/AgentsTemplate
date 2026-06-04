---
tool: plan
description: 读写 SessionDB/<thread_id>/plan.json；update_subtask_status='done' 时强制触发 checker 硬 gate 并把 CheckerReport 嵌回返回值
---

# plan Tool — SKILL.md

## 概览
`plan` 是 manager 维护 `SessionDB/<thread_id>/plan.json` 的唯一通道。它**只能由 manager 使用**。plan 文件按会话 thread_id 隔离，每个会话独立一份。

最关键的特性：**`update_subtask_status` 当 `new_status='done'` 时，工具会同步触发 `checker_agent` 做对齐检查，并把完整的 `CheckerReport` 嵌入返回值**。这是 hard gate —— manager **绕不过**这次检查，必须读完报告再决定下一步。

```
manager: update_subtask_status(done) → 写 plan.json → checker_agent.invoke → 返回 CheckerReport +「下一步铁律」
```

---

## ⛔ 硬约束

1. **只有 manager 能用** —— 工具 description 已声明；其他 agent 的工具表里不要出现 `plan`。
2. **plan.json 是唯一可信事实源** —— 不要用 terminal 直接 `cat` / 写 plan.json，所有读写都走本工具。
3. **`update_subtask_status` 的 done 调用是 hard gate** —— 你**不能**在没拿到 CheckerReport 之前就开始下一个 subtask；不能"忽略报告里的 major_drift"。
4. **写入是覆盖式** —— `write` 会替换整个 plan.json。**先 read 拿到现状，再在内存里改完一次性 write 回去**，避免漏字段。
5. **`updated_at` 自动盖** —— 不要手动维护，工具每次写入都会刷新。
6. **`created_at` 首次自动盖** —— 后续 write 不会覆盖已存在的 `created_at`。

---

## Actions

| action | 必填参数 | 作用 |
|---|---|---|
| `read` | — | 返回 plan dict 的 JSON；空 / 不存在 / 非法时返回提示语 |
| `write` | `plan_json` | 用完整 plan JSON 文本覆盖整个 plan.json |
| `update_subtask_status` | `subtask_id`, `new_status` | 改某 subtask.status；**done 时触发 checker 硬 gate** |
| `set_milestone_status` | `milestone_id`, `new_status` | 改 milestone.status |
| `set_plan_status` | `new_status` | 改 plan.status（drafting/ready/executing/done/blocked） |
| `clear` | — | 清空 plan.json |

### 状态枚举

- **plan.status**：`drafting` / `ready` / `executing` / `done` / `blocked`
- **milestone.status / subtask.status**：`pending` / `in_progress` / `done` / `blocked`

### plan.json 字段 schema（必须遵守）

```json
{
  "goal": "一句话目标",
  "status": "drafting | ready | executing | done | blocked",
  "constraints": ["plan 执行期不得违反的硬约束"],
  "notes": ["重要注意事项 / 背景 / 假设"],
  "milestones": [
    {
      "id": "m1",
      "name": "里程碑名",
      "intent": "这个里程碑要达成什么",
      "status": "pending",
      "depends_on": [],
      "subtasks": [
        {
          "id": "m1-t1",
          "description": "做什么",
          "dispatch_to": "tasker_coder | tester | retriever | manager_self | none",
          "verification": "完成的可验证标准",
          "status": "pending",
          "result_summary": ""
        }
      ]
    }
  ],
  "created_at": "<auto>",
  "updated_at": "<auto>"
}
```

---

## 典型工作流

### 1) 第一次写入 plan（drafting → ready）
```
action=write
plan_json={
  "goal": "为 Report 对象增加 CSV 导出能力",
  "status": "drafting",
  "constraints": ["不得改 Report 既有字段命名"],
  "notes": ["Excel 默认 ANSI 编码 → 加 UTF-8 BOM"],
  "milestones": [...]
}
```
→ 返回 `plan.json 已写入。当前 status=drafting。`

向用户确认 plan，得到 OK 后：
```
action=set_plan_status
new_status=ready
```

### 2) 推进一个 subtask（pending → in_progress）
```
action=update_subtask_status
subtask_id=m1-t1
new_status=in_progress
```

### 3) 完成 subtask（触发 hard gate）
```
action=update_subtask_status
subtask_id=m1-t1
new_status=done
result_summary=新增 Tools/csv_exporter.py，3 条边界测试通过
```
返回值的结构（**必读**）：
```
=== subtask `m1-t1` 已写入 plan.json，状态 = done ===

=== Checker 强制对齐报告（hard gate） ===
{ ...CheckerReport JSON... }

=== 你下一步必须做的（铁律） ===
* on_track / minor_drift  → 继续下一 subtask
* major_drift / off_track   → 先纠偏，不得硬推进
```

### 4) 完成 milestone → 整个 plan
```
action=set_milestone_status, milestone_id=m1, new_status=done
action=set_plan_status, new_status=done
```

---

## ❌ 反模式

| 反模式 | 后果 | 改用 |
|---|---|---|
| terminal 直接改 plan.json | 绕过 checker / 字段损坏 | 只走 plan 工具 |
| done 后忽略 CheckerReport | major_drift 被掩盖 | 必读报告再推进 |
| write 时不 read 现状 | 漏字段 / 覆盖 milestones | 先 read 再 write |
| subtask 没 verification 就 dispatch | 无法验收 | 写清可验证标准 |

---

## 💡 dispatch_to 选用

`tasker_coder` 写代码改文件；`tester` 独立验证；`retriever` 检索汇总；`manager_self` manager 轻量自办；`none` 纯规划不需派子代理。

---

## 探索经验
```
1. 应该避免做..., 否则会导致..., 应该做...
2. ...
```
