---
name: agent_factory
description: 装配 langchain agent 的组件：agent 组装器 + 可注入的上下文注入中间件 + 预算提醒中间件。把模型/工具/提示/中间件解耦组合。一个完整功能：拼装领域 agent 的收口层。
---

# agent_factory — AgentBuilder / 中间件

实现文件：`CompLib/agent_factory/agent_factory.py`（单文件，内含下列协作类）

## 用途
把 llm_factory 的模型、工具集、system_prompt、中间件组合成一个可运行 agent。
两个常用中间件做成独立可注入小类，省掉每个 agent 各写一套。

## 接口
`from CompLib.agent_factory.agent_factory import AgentBuilder, ContextInjectMiddleware, BudgetReminderMiddleware`

- `ContextInjectMiddleware(providers)`：`providers=[() -> str, ...]` 一组返回可变上下文文本的回调（如 workspace/plan/todo），注入到 messages 末尾 SystemMessage；空串跳过；不动静态 system_prompt（保缓存）
- `BudgetReminderMiddleware(run_limit, message=None)`：仅在本轮最后一次 LLM 调用前注入提醒
- `AgentBuilder(model, tools, system_prompt, *, middleware=None, checkpointer=None, response_format=None).build()`：仅把零件交给 `create_agent`

## 依赖
`langchain`（create_agent / AgentMiddleware）、`langchain-core`

## 用法示例
```python
from CompLib.agent_factory.agent_factory import AgentBuilder, ContextInjectMiddleware, BudgetReminderMiddleware
from CompLib.llm_factory.llm_factory import ChatModelFactory
agent = AgentBuilder(
    model=ChatModelFactory("agent").build(),
    tools=[...],
    system_prompt="你是…",
    middleware=[ContextInjectMiddleware([lambda: workspace_text()]), BudgetReminderMiddleware(run_limit=80)],
).build()
```
