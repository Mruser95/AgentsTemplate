---
tool: remember
description: 对话记忆整理 agents 的使用约束与调用策略（短期摘要 agent + 长期抽取 agent）
---

# Remember Agents — SKILL.md

## 概览
`remember` 模块提供**两个独立的写入侧 agent**，它们不共享 prompt、也不共享调用节奏：

| Agent | 输出 | 调用频率 | 用途 |
|---|---|---|---|
| `short_memory_agent` | 一条 `ShortMemoryEntry` | 较高频 | 压缩当前对话，替换上下文窗口里的旧轮次 |
| `long_memory_agent` | `LongMemoryEntry[]`（可为空） | 较低频 | 跨会话沉淀用户画像 + 通用知识 |

两者都只产出**结构化 JSON**，由 `Agents/remember.py` 中的 Pydantic schema 校验。
**落盘（sqlite / 向量库）由外层代码完成，agent 本身没有任何工具权限。**

> 在调用任何一个 agent 之前，必须通读本文件的「硬约束」与「何时调用」两节。

---

## ⛔ 硬约束（调用前必读）

### 1. 两个 agent 不要合并使用
不要把同一段 transcript 同时喂给两个 agent 期望得到"一条龙"结果。
它们是**独立触发**的：短期摘要每段对话跑一次；长期抽取按你自己的策略决定。
合并调用会让 prompt 失焦、结果质量下降。

### 2. 输入必须是完整对话，不是单条消息
喂给它们的 `messages` 应是一段**多轮 user / assistant 的消息流（list[BaseMessage]）**，按顺序排列。
单条消息没有摘要价值，也无法生成有意义的 `turn_range`。
> 工具内部会用 `get_buffer_string` 统一序列化成 transcript 文本，**调用方不需要自己拼字符串**。

### 3. 短期摘要"每段对话只做一次"
不要把同一段对话反复喂给 `short_memory_agent`，也不要让它去"合并多条旧摘要"。
**多条短期摘要的后续合并是外层的事**：
- 当短期摘要累积变多、开始撑大上下文时，外层把最久远的若干条**向量化写入 sqlite 向量库**，
- 下次会话用检索的方式召回相关摘要，而不是再让 agent 重新总结。

### 4. 长期抽取结果可能是空列表
`long_memories: []` 是合法且常见的输出（闲聊、工具调试、无新信息时）。
**不要因为列表为空就重试**，那通常是正确答案。

### 5. 不能用它们做"事实问答"
这两个 agent 都是**写入侧**的，不负责从既有记忆库里检索。
问"用户上周说了什么" → 去查记忆库，不要再跑一次 remember。

### 6. 不要给它们加 shell / 文件工具
写库是确定性的数据通路，应该由外层代码用 sqlite / 向量库 SDK 直接完成。
让 LLM 通过 shell 拼写入命令既不安全、也不稳定、还贵。
**保持它们的 `tools=[]`**。

---

## 📐 何时调用

### `short_memory_agent`
| 场景 | 是否调用 | 说明 |
|---|---|---|
| 会话结束 / 主动退出 | ✅ | 最常见的触发点 |
| 上下文窗口接近上限 | ✅ | 用 summary 替换旧轮次，释放 token |
| 用户刚发了一句问候 | ❌ | 无信息量，跳过 |
| 仅有工具调用日志 | ❌ | 没有对话语义 |

### `long_memory_agent`
| 场景 | 是否调用 | 说明 |
|---|---|---|
| 会话结束时，且对话有实质内容 | ✅ | 常规触发点 |
| 用户明确说"记住这个" | ✅ | 提示该条大概率落到 importance ≥ 4 |
| 产出了可复用的方案 / 教训 | ✅ | 以 `memory_type=knowledge` 形式沉淀 |
| 纯闲聊 / 纯工具调试 | ❌ | 跳过，避免污染长期库 |
| 上下文刚刚已跑过一次 | ❌ | 幂等但非无代价，重复调用会产生重复候选 |

---

## 🎯 输出字段使用指南

### ShortMemoryEntry —— 下一个 agent 读
- `summary`：拿去做 system prompt 的上下文前缀。
- `open_tasks`：驱动下一轮 planning；为空则不要自造任务。
- `active_entities`：做检索 / 高亮 / 自动补全的锚点。
- `turn_range`：配合原始 transcript 做回溯；存库时一并持久化。

**级联策略**：
```
turn -> short_memory_agent -> ShortMemoryEntry（一段一条）
                                    ↓
                               会话级缓存（最近 N 条保留在内存 / 进程）
                                    ↓
                          超过阈值后最旧的若干条
                                    ↓
                   外层 embedding + sqlite 向量库（由调用方实现）
```

### LongMemoryEntry —— 长期记忆库落盘
按 `importance` 做分层存储的建议：

| importance | 处置 |
|---|---|
| 1 | 可直接丢弃，或仅保留 7 天 |
| 2–3 | 进入常规记忆库，参与向量召回 |
| 4 | 置顶召回 + 进入用户画像 |
| 5 | 核心身份 / 关键事件 / 关键知识，常驻 system prompt |

关于 `memory_type`：
- `fact` / `preference` / `event` / `emotion` / `skill` / `relationship` 都是**关于用户本人**的记忆；
- `knowledge` 是**与用户无关**但可复用的领域知识（某个库的坑、某个方案的结论）。
  二者检索时可以合并召回，也可以按类型分索引，外层自行决定。

`tags` 字段请在外层**归一化**（小写、去空格、同义词合并），否则召回效果会被拼写差异稀释。

---

## 🔁 错误与边界处理

| 现象 | 可能原因 | 处理方式 |
|---|---|---|
| 返回非合法 JSON | 小模型格式漂移 | 重试一次；仍失败则降级到"只要 summary" |
| `long_memories` 全是废话 | 输入本身就是闲聊 | 正常，直接丢弃该次结果 |
| 同一事实反复出现 | 增量窗口和全量窗口混用 | 统一策略；或落库前做语义去重 |
| `knowledge` 条目被误判成 `fact` | prompt 区分不够清晰 | 检查"是否只与用户本人绑定"，若不是则改 `knowledge` |
| 虚构事实 | 输入里有"可能 / 也许 / 下次" | 该模型倾向保守，若仍出现请降低 temperature |

---

## ✅ 典型工作流示例

**任务：一次多轮编码协作会话结束后整理记忆**

```
1. 收集原始 messages（list[BaseMessage]，含 user / assistant 轮次，按顺序）
2. 并行调用：
   2a. short_memory(messages) -> ShortMemoryEntry
   2b. long_memory(messages)  -> {long_memories: [...]}
3. 外层处理：
   3a. ShortMemoryEntry 写入会话级缓存；超过阈值的旧条目 embed 后写入 sqlite 向量库
   3b. long_memories 逐条做向量化 + 去重 + 合并到长期库（后续单独实现）
4. 丢弃原始 messages（或冷存储）
```

不要在第 2 步之前做"预摘要"——那会让 agent 失去原始语境。

---

## 探索经验
```
1. 应该避免做..., 否则会导致..., 应该做...
2. ...
...
```
