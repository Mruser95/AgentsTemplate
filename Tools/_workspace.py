from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def workspace_dir(thread_id: str) -> Path:
    if not thread_id:
        raise ValueError("thread_id 不能为空")
    return PROJECT_ROOT / "SessionDB" / thread_id / "workspace"


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


def workspace_info(thread_id: str) -> str:
    wd = ensure_workspace(thread_id)
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
    )
