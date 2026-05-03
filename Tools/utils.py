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


async def compress_ckpt_messages(thread_id: str, removed_ids: list[str], summary_text: str) -> None:
    ids = [i for i in (removed_ids or []) if i]
    if not ids or not summary_text:
        return
    from langchain_core.messages import RemoveMessage, SystemMessage
    from Agents.manager import open_manager_agent  # 局部导入避免循环

    async with open_manager_agent(thread_id=thread_id) as agent:
        ops: list = [RemoveMessage(id=i) for i in ids]
        ops.append(SystemMessage(content=f"{SUMMARY_MARKER}\n{summary_text}"))
        await agent.aupdate_state(
            {"configurable": {"thread_id": thread_id}},
            {"messages": ops},
        )


