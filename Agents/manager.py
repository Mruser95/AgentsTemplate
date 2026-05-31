from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, AsyncIterator, Awaitable, Callable, Optional, Tuple
from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage
from langchain.agents import create_agent
from langchain.agents.middleware import ModelCallLimitMiddleware
from pathlib import Path
from dotenv import load_dotenv
import os
import sys
import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from Tools.terminal import SafeShell  # noqa: E402
from Tools.skills import SkillLibrary  # noqa: E402
from Tools.read import Read  # noqa: E402
from Tools.schedule import Schedule  # noqa: E402
from Tools.tavily import TavilySearch  # noqa: E402
from Tools.overview import Glob, Grep, RepoMap  # noqa: E402
from Tools.plan import Plan  # noqa: E402
from Tools.todo import Todo  # noqa: E402
from Tools.utils import is_summary_message, llm_runtime_kwargs, reset_tool_budgets, workspace_info  # noqa: E402
from Agents.retriver import retrieve  # noqa: E402
from Agents.Tasker_coder import dispatch_tasker_coder  # noqa: E402
from Agents.tester import dispatch_tester, dispatch_test_runner  # noqa: E402
from agents_prompt import manager_prompt  # noqa: E402

load_dotenv(PROJECT_ROOT / ".env")

with open(PROJECT_ROOT / "config.yaml", "r", encoding="utf-8") as f:
    _config = yaml.safe_load(f) or {}

manager_run_call_limit: int = _config.get("manager_run_call_limit", 80)
manager_exit_behavior: str = _config.get("manager_exit_behavior", "end")
manager_recursion_limit: int = int(_config.get("manager_recursion_limit", 100))
manager_max_tokens: int = int(_config.get("manager_max_tokens", 4096))


CHECKPOINT_DB = PROJECT_ROOT / "SessionDB" / "checkpoints.db"


llm = ChatOpenAI(
    model=os.getenv("agent_llm_model"),
    api_key=os.getenv("agent_llm_key"),
    base_url=os.getenv("agent_llm_base_url"),
    max_tokens=manager_max_tokens,
    temperature=os.getenv("agent_llm_temperature", 0.7),
    **llm_runtime_kwargs("manager", _config),
)


# Manager Agent ========================================================================


_MANAGER_TOOLS = [
    SkillLibrary(),
    SafeShell(),
    Read(),
    RepoMap(),
    Grep(),
    Glob(),
    TavilySearch(),
    Schedule(),
    Plan(),
    Todo(read_only=True),
    retrieve,
    dispatch_tasker_coder,
    dispatch_tester,
    dispatch_test_runner,
]

_MANAGER_MIDDLEWARE = [
    ModelCallLimitMiddleware(
        run_limit=manager_run_call_limit,
        exit_behavior=manager_exit_behavior,
    ),
]

def _build_manager_agent(checkpointer: Any = None, thread_id: str | None = None):
    sp = manager_prompt
    if thread_id:
        sp = sp + "\n\n---\n\n" + workspace_info(thread_id)
    try:
        from Memory.proj_agent import read_notes
        notes = read_notes()
    except Exception:
        notes = ""
    if notes:
        sp = sp + "\n\n---\n\n## 项目进展记忆（projectKnow）\n" + notes
    return create_agent(
        model=llm,
        tools=_MANAGER_TOOLS,
        system_prompt=sp,
        middleware=_MANAGER_MIDDLEWARE,
        checkpointer=checkpointer,
    )


# 持久化入口 ========================================================================


@asynccontextmanager
async def open_manager_agent(thread_id: str | None = None) -> AsyncIterator[Any]:
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    CHECKPOINT_DB.parent.mkdir(parents=True, exist_ok=True)
    async with AsyncSqliteSaver.from_conn_string(str(CHECKPOINT_DB)) as saver:
        yield _build_manager_agent(checkpointer=saver, thread_id=thread_id)


@dataclass
class ManagerSession:
    thread_id: str
    ainvoke: Callable[..., Awaitable[dict]]
    astream: Callable[..., AsyncIterator[Tuple[str, Any]]]


@asynccontextmanager
async def manager_session(thread_id: str) -> AsyncIterator[ManagerSession]:
    if not thread_id or not isinstance(thread_id, str):
        raise ValueError("manager_session 需要非空字符串 thread_id（多用户隔离依赖它）")

    async with open_manager_agent(thread_id=thread_id) as agent:
        config: dict = {
            "configurable": {"thread_id": thread_id},
            "recursion_limit": manager_recursion_limit,
        }

        def _merged_config(extra_config: Optional[dict] = None) -> dict:
            merged_config = dict(config)
            if extra_config:
                merged_cfg = dict(merged_config.get("configurable") or {})
                merged_cfg.update(extra_config.get("configurable") or {})
                merged_config["configurable"] = merged_cfg
            return merged_config

        def _count_active_messages(messages: list) -> int:
            return sum(1 for m in messages if not is_summary_message(m))

        async def _state_active_count(merged_config: dict) -> int:
            snap = await agent.aget_state(merged_config)
            return _count_active_messages((snap.values or {}).get("messages") or [])

        def _notify_delta(merged_config: dict, before: int, after: int) -> None:
            delta = max(after - before, 0)
            if not delta:
                return
            notify_tid = str((merged_config.get("configurable") or {}).get("thread_id") or thread_id)
            try:
                from schedule import scheduler
                scheduler.notify(notify_tid, delta=delta)
            except Exception:
                pass

        async def _repair_orphans(merged_config: dict) -> None:
            from langchain_core.messages import AIMessage, ToolMessage
            snap = await agent.aget_state(merged_config)
            msgs = list((snap.values or {}).get("messages") or [])
            answered = {m.tool_call_id for m in msgs if isinstance(m, ToolMessage)}
            orphans = [tc["id"] for m in msgs if isinstance(m, AIMessage)
                       for tc in (m.tool_calls or []) if tc["id"] not in answered]
            if orphans:
                dummies = [ToolMessage(content="[auto-repair] 上一轮被中断，该工具未产出结果。", tool_call_id=tc)
                           for tc in orphans]
                await agent.aupdate_state(merged_config, {"messages": dummies})

        async def _ainvoke(message: str, *, extra_config: Optional[dict] = None) -> dict:
            reset_tool_budgets(_MANAGER_TOOLS)  # 工具配额按"每轮用户消息"重置，不跨整个 thread 累计
            merged_config = _merged_config(extra_config)
            await _repair_orphans(merged_config)
            last = await _state_active_count(merged_config)
            result: dict = {}
            async for event in agent.astream_events(
                {"messages": [HumanMessage(content=message)]},
                config=merged_config,
                version="v2",
            ):
                if event.get("event") == "on_tool_end":
                    cur = await _state_active_count(merged_config)
                    _notify_delta(merged_config, last, cur)
                    last = cur
                elif event.get("event") == "on_chain_end" and event.get("name") == "LangGraph":
                    out = (event.get("data") or {}).get("output")
                    if isinstance(out, dict):
                        result = out
            cur = await _state_active_count(merged_config)
            _notify_delta(merged_config, last, cur)
            return result

        async def _astream(message: str, *, extra_config: Optional[dict] = None) -> AsyncIterator[Tuple[str, Any]]:
            """流式产出归一化事件 (name, payload)：token / tool_start / tool_end。"""
            reset_tool_budgets(_MANAGER_TOOLS)  # 工具配额按"每轮用户消息"重置，不跨整个 thread 累计
            merged_config = _merged_config(extra_config)
            await _repair_orphans(merged_config)
            last = await _state_active_count(merged_config)
            try:
                async for event in agent.astream_events(
                    {"messages": [HumanMessage(content=message)]},
                    config=merged_config,
                    version="v2",
                ):
                    name, data = event.get("event"), event.get("data") or {}
                    if name == "on_chat_model_stream":
                        text = getattr(data.get("chunk"), "content", "")
                        if isinstance(text, str) and text:
                            yield "token", text
                    elif name == "on_tool_start":
                        yield "tool_start", {"name": event.get("name"), "args": data.get("input")}
                    elif name == "on_tool_end":
                        yield "tool_end", {"name": event.get("name"), "output": str(data.get("output"))}
                        try:
                            cur = await _state_active_count(merged_config)
                            _notify_delta(merged_config, last, cur)
                            last = cur
                        except Exception:
                            pass
            finally:
                try:
                    cur = await _state_active_count(merged_config)
                    _notify_delta(merged_config, last, cur)
                except Exception:
                    pass

        yield ManagerSession(thread_id=thread_id, ainvoke=_ainvoke, astream=_astream)
