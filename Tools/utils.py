from __future__ import annotations

import os
import venv
from pathlib import Path
from typing import Any

from langchain_core.runnables.config import ensure_config


# Workspace 路径 =======================================================
PROJECT_ROOT = Path(__file__).resolve().parent.parent
_BIN = "Scripts" if os.name == "nt" else "bin"
_PY = "python.exe" if os.name == "nt" else "python"


def workspace_dir(thread_id: str) -> Path:
    if not thread_id:
        raise ValueError("thread_id 不能为空")
    return PROJECT_ROOT / "SessionDB" / thread_id / "workspace"


def venv_dir(thread_id: str) -> Path:
    # 与 workspace 同级（SessionDB/<tid>/.venv），避免被用户下载或误删
    return workspace_dir(thread_id).parent / ".venv"


def ensure_workspace(thread_id: str) -> Path:
    wd = workspace_dir(thread_id)
    wd.mkdir(parents=True, exist_ok=True)
    return wd


def is_inside(child: Path | str, parent: Path | str) -> bool:
    try:
        Path(child).resolve().relative_to(Path(parent).resolve())
        return True
    except (ValueError, OSError):
        return False


# 私有 venv =============================================================
def workspace_env(thread_id: str) -> dict:
    vd = venv_dir(thread_id)
    if not (vd / _BIN / _PY).exists():
        vd.parent.mkdir(parents=True, exist_ok=True)
        venv.EnvBuilder(with_pip=True, symlinks=(os.name != "nt")).create(str(vd))
    env = os.environ.copy()
    env["VIRTUAL_ENV"] = str(vd)
    env["PATH"] = f"{vd / _BIN}{os.pathsep}{env.get('PATH', '')}"
    env.pop("PYTHONHOME", None)
    env.pop("PYTHONPATH", None)
    return env


def workspace_info(thread_id: str) -> str:
    wd = ensure_workspace(thread_id)
    vd = venv_dir(thread_id)
    return (
        "## 工作目录（Workspace）\n"
        f"- thread_id：`{thread_id}`；工作目录：`{wd}`\n"
        "- terminal 工具的 cwd 已锁在这里，写文件请用相对路径。\n"
        "- **写**只能落 workspace 内（禁止 `cd ..`、绝对路径、`>` / `tee` / `mv` 越界）；"
        "**读**允许跨目录（参考代码用）。\n"
        "- 用户能下载的也只有这里的文件 / 文件夹。\n"
        "- **路径翻译铁律**：用户消息或上级 prompt 里出现的"
        "项目根 / 绝对路径（含 `/Users/.../AgentsTemplate/...`、"
        "或相对项目根的 `./xxx.py`），**必须**在派给下游子代理 / 自己动手前"
        f"翻译为相对此 workspace（`{wd}`）的路径，否则文件落在 workspace "
        "之外，用户根本拿不到。即使上游原文写了绝对路径，也要按本规则改写。\n"
        "\n"
        "## 私有虚拟环境（Per-workspace venv）\n"
        f"- 本会话 venv：`{vd}`（terminal 已自动激活，跨会话不共享）。\n"
        "- 装包直接 `pip install <pkg>`；**不要** `--user`、`python -m venv` / `conda create`、"
        "或用绝对路径调宿主 Python（`/usr/bin/python3` 之类会绕过隔离）。\n"
    )


# 线程上下文 =============================================================
DEFAULT_THREAD_ID = "_default"


def current_thread_id() -> str:
    cfg: dict[str, Any] = ensure_config()
    tid = (cfg.get("configurable") or {}).get("thread_id")
    return str(tid) if tid else DEFAULT_THREAD_ID


# 调用配额 ===============================================================
def bump_budget(counts: dict[str, int], thread_id: str, limit: int) -> tuple[bool, int, int]:
    cur = counts.get(thread_id, 0)
    if cur >= limit:
        return False, cur, 0
    n = cur + 1
    counts[thread_id] = n
    return True, n, limit - n


def reset_tool_budgets(tools: list) -> None:
    """清零一组工具的 per-thread 调用计数。被复用的单例 agent（manager / tasker_coder /
    retriever）在每次进入（一次 invoke）前调用，使工具配额变成 per-run 而非整个 thread 累计。"""
    for t in tools or []:
        reset = getattr(t, "reset", None)
        if callable(reset):
            try:
                reset()
            except Exception:
                pass


# LLM 运行时参数（每个 agent 独立：超时 / 重试 / 思考开关）===============
def llm_runtime_kwargs(agent: str, config: dict | None = None) -> dict:
    """按 agent 名（如 'coder' / 'manager'）从 config 读取该 agent 的 LLM 运行时旋钮，返回可展开给 ChatOpenAI 的 kwargs。
    读取 <agent>_timeout（秒）/ <agent>_max_retries / <agent>_reasoning。
    reasoning=False 时注入 extra_body 关闭模型思考；stream_chunk_timeout 与 timeout 对齐（仅流式 agent 生效）。"""
    cfg = config or {}
    timeout = cfg.get(f"{agent}_timeout", 120)
    kwargs: dict[str, Any] = {
        "timeout": timeout,
        "max_retries": int(cfg.get(f"{agent}_max_retries", 2)),
        "stream_chunk_timeout": timeout,
    }
    if not bool(cfg.get(f"{agent}_reasoning", False)):
        kwargs["extra_body"] = {"reasoning": {"enabled": False}}
    return kwargs


# 子代理 checkpoint 持久化开关（调试用）=================================
def subagent_checkpointer(config: dict | None = None):
    """子代理（coder/tasker/tester/retriever/checker）是否把自身 state 落盘到共享 checkpoints.db。
    返回值直接喂给 create_agent(checkpointer=...)：
      - False（默认）：命中 LangGraph run 路径里 `if self.checkpointer is False` 短路，不继承 manager
        的 saver、不写嵌套 checkpoint（生产/干净行为）。
      - None（config.subagent_persist_checkpoint=true）：回到框架默认继承——子代理跑在 manager 工具内
        时会捡起父 config 的 saver 把全量 state 逐步写盘（写放大，**仅调试**：想在 DB 里回看子代理轨迹时开）。"""
    cfg = config or {}
    return None if bool(cfg.get("subagent_persist_checkpoint", False)) else False


# Checkpoint 读取 ========================================================
CHECKPOINT_DB = PROJECT_ROOT / "SessionDB" / "checkpoints.db"


async def read_ckpt_msgs(thread_id: str) -> list:
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    CHECKPOINT_DB.parent.mkdir(parents=True, exist_ok=True)
    async with AsyncSqliteSaver.from_conn_string(str(CHECKPOINT_DB)) as saver:
        tup = await saver.aget_tuple({"configurable": {"thread_id": thread_id}})
    msgs = (getattr(tup, "checkpoint", None) or {}).get("channel_values", {}).get("messages") if tup else None
    return list(msgs) if isinstance(msgs, list) else []


# Short-memory 压缩摘要标记 ===========================================
SUMMARY_MARKER = "[compressed earlier conversation]"


def is_summary_message(msg: Any) -> bool:
    """判断一条消息是否是 short_mem 压缩生成的摘要 SystemMessage。"""
    from langchain_core.messages import SystemMessage  # 局部导入避免顶层重复
    if not isinstance(msg, SystemMessage):
        return False
    content = getattr(msg, "content", None)
    return isinstance(content, str) and content.startswith(SUMMARY_MARKER)


def _tool_call_groups(msgs: list) -> dict[str, set[str]]:
    """member_id -> 同组全部成员 ids；AIMessage(tool_calls) 与子 ToolMessage 不可拆。"""
    from langchain_core.messages import AIMessage, ToolMessage
    groups: dict[str, set[str]] = {}
    by_tc: dict[str, str] = {}
    for m in msgs:
        if isinstance(m, AIMessage) and m.tool_calls:
            groups[m.id] = {m.id}
            for tc in m.tool_calls:
                by_tc[tc["id"]] = m.id
        elif isinstance(m, ToolMessage) and m.tool_call_id in by_tc:
            groups[by_tc[m.tool_call_id]].add(m.id)
    return {mid: g for g in groups.values() for mid in g}


async def compress_ckpt_messages(thread_id: str, removed_ids: list[str], summary_text: str) -> None:
    """删除指定消息并追加摘要 SystemMessage；同组（AIMessage + 子 ToolMessage）整体进出。"""
    ids = {i for i in (removed_ids or []) if i}
    if not ids or not summary_text:
        return
    from langchain_core.messages import RemoveMessage, SystemMessage
    from Agents.manager import open_manager_agent  # 局部导入避免循环

    flat = _tool_call_groups(await read_ckpt_msgs(thread_id))
    safe_ids = ids | {x for mid in ids for x in flat.get(mid, ())}
    ops = [RemoveMessage(id=i) for i in safe_ids]
    ops.append(SystemMessage(content=f"{SUMMARY_MARKER}\n{summary_text}"))
    async with open_manager_agent(thread_id=thread_id) as agent:
        await agent.aupdate_state({"configurable": {"thread_id": thread_id}}, {"messages": ops})


# 可变上下文注入（messages 末尾 SystemMessage，静态 system_prompt 保缓存）==========

from langchain.agents.middleware import AgentMiddleware  # noqa: E402
from langchain_core.messages import SystemMessage  # noqa: E402


def project_know_text() -> str:
    try:
        from Memory.proj_agent import read_notes
        notes = read_notes(current_thread_id())
    except Exception:
        notes = ""
    if not notes:
        return ""
    return "## 项目记忆（projectKnow：历史执行流程 + 坑/方法/知识）\n" + notes


class ContextInjectMiddleware(AgentMiddleware):
    """workspace / projectKnow / plan / todo / extra 等可变块 → messages 末尾 SystemMessage。"""

    def __init__(
        self,
        *,
        workspace: bool = False,
        project_know: bool = False,
        plan: bool = False,
        todo: bool = False,
        extra: str = "",
    ) -> None:
        super().__init__()
        self.workspace = workspace
        self.project_know = project_know
        self.plan = plan
        self.todo = todo
        self.extra = extra

    def _parts(self) -> list[str]:
        parts: list[str] = []
        if self.workspace:
            parts.append(workspace_info(current_thread_id()))
        if self.project_know:
            t = project_know_text()
            if t:
                parts.append(t)
        if self.plan:
            from Tools.plan import plan_inject_text
            parts.append(plan_inject_text())
        if self.todo:
            from Tools.todo import todo_inject_text
            parts.append(todo_inject_text())
        if self.extra.strip():
            parts.append(self.extra.strip())
        return parts

    def wrap_model_call(self, request, handler):  # type: ignore[override]
        return handler(self._inject(request))

    async def awrap_model_call(self, request, handler):  # type: ignore[override]
        return await handler(self._inject(request))

    def _inject(self, request):
        parts = self._parts()
        if not parts:
            return request
        return request.override(
            messages=list(request.messages) + [SystemMessage(content="\n\n---\n\n".join(parts))]
        )


