import json
import locale
import platform
import subprocess
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, Optional, Type
from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field

IS_WINDOWS = platform.system() == "Windows"
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
SCHEDULE_DIR = PROJECT_ROOT / ".schedule"
SCHEDULE_DIR.mkdir(exist_ok=True)

from Tools._context import current_thread_id  # noqa: E402

_VENV_PYTHON = PROJECT_ROOT / (
    ".venv/Scripts/python.exe" if IS_WINDOWS else ".venv/bin/python"
)
if not _VENV_PYTHON.exists():
    raise RuntimeError(
        f"未找到项目虚拟环境解释器：{_VENV_PYTHON}，"
        f"请先在项目根目录创建 .venv 并安装依赖。"
    )
PYTHON = str(_VENV_PYTHON)

EXECUTOR = "manager"

CreatorType = Literal["user", "agent", "unknown"]
_ALLOWED_CREATORS: tuple[str, ...] = ("user", "agent", "unknown")


def _decode(b: bytes) -> str:
    if not b:
        return ""
    try:
        return b.decode("utf-8")
    except UnicodeDecodeError:
        return b.decode(locale.getpreferredencoding(False), errors="replace")


class ScheduleInput(BaseModel):
    action: str = Field(description="create / list / delete / history")
    name: str = Field(
        default="",
        description="任务名；create/delete/history 必填，避免 \" / | 等 shell 特殊字符",
    )
    intent: str = Field(
        default="",
        description="自然语言意图：到点了让 manager 做什么（create 必填）",
    )
    time: str = Field(
        default="",
        description="Windows: 'HH:MM'；Linux/macOS: 5 段 cron 表达式（create 必填）",
    )
    creator: CreatorType = Field(
        default="unknown",
        description=(
            "这条定时任务由谁发起，三选一："
            "'user'（用户明确要求 manager 制定）、"
            "'agent'（manager 在会话中自主决定制定）、"
            "'unknown'（无法判断来源）。"
        ),
    )
    context: str = Field(
        default="",
        description=(
            "制定任务时的会话上下文，JSON 字符串；记录当时的背景、目的、关键事实、"
            "不得违反的约束等。到点时 Tasker_schedule.py 会连同 intent 一起塞给 "
            "manager_agent，用于恢复制定任务时的会话状态。"
            "示例：'{\"background\":\"...\",\"purpose\":\"...\",\"constraints\":[...]}'"
        ),
    )


# Helper Functions ==================================================================


def _sh(cmd: str) -> str:
    p = subprocess.run(cmd, shell=True, capture_output=True, timeout=30)
    out = (_decode(p.stdout) + _decode(p.stderr)).strip()
    return out or f"[exit={p.returncode}]"


def _write_runner(task_id: str) -> Path:
    # 绕开 schtasks /TR 的嵌套引号规则。
    if IS_WINDOWS:
        p = SCHEDULE_DIR / f"{task_id}.bat"
        p.write_text(
            f'@echo off\ncd /d "{PROJECT_ROOT}"\n"{PYTHON}" -m Tools.Tasker_schedule --task {task_id}\n',
            encoding="utf-8",
        )
    else:
        p = SCHEDULE_DIR / f"{task_id}.sh"
        p.write_text(
            f'#!/bin/sh\ncd "{PROJECT_ROOT}" && "{PYTHON}" -m Tools.Tasker_schedule --task {task_id}\n',
            encoding="utf-8",
        )
        p.chmod(0o755)
    return p


def _find_meta(name: str) -> Optional[Path]:
    for p in SCHEDULE_DIR.glob("*.json"):
        if json.loads(p.read_text(encoding="utf-8")).get("name") == name:
            return p
    return None


def _normalize_context(context: str) -> Any:
    if not context:
        return {}
    text = context.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"raw": text}


def _create(name: str, intent: str, time: str, creator: str, context: str, thread_id: str) -> str:
    if not (name and intent and time):
        return "create 需要同时提供 name、intent、time。"
    if creator not in _ALLOWED_CREATORS:
        return (
            f"creator 必须是 {'/'.join(_ALLOWED_CREATORS)} 三者之一，"
            f"收到的是 '{creator}'。"
        )
    task_id = uuid.uuid4().hex[:8]
    meta = {
        "id": task_id,
        "name": name,
        "intent": intent,
        "time": time,
        "creator": creator,
        "executor": EXECUTOR,
        "thread_id": thread_id,
        "context": _normalize_context(context),
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }
    (SCHEDULE_DIR / f"{task_id}.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    runner = _write_runner(task_id)
    if IS_WINDOWS:
        cmd = f'schtasks /Create /TN "{name}" /TR "{runner}" /SC DAILY /ST {time} /F'
    else:
        cmd = f'(crontab -l 2>/dev/null; echo "{time} {runner} # {name}") | crontab -'
    return (
        f"[created id={task_id}, creator={creator}, executor={EXECUTOR}, thread_id={thread_id}]\n"
        f"{_sh(cmd)}"
    )


def _delete(name: str) -> str:
    if not name:
        return "delete 需要提供 name。"
    meta = _find_meta(name)
    if meta is not None:
        (SCHEDULE_DIR / f"{meta.stem}.bat").unlink(missing_ok=True)
        (SCHEDULE_DIR / f"{meta.stem}.sh").unlink(missing_ok=True)
        meta.unlink(missing_ok=True)
    cmd = (
        f'schtasks /Delete /TN "{name}" /F'
        if IS_WINDOWS
        else f"crontab -l 2>/dev/null | grep -v '# {name}$' | crontab -"
    )
    return _sh(cmd)


def _list() -> str:
    sys_tasks = _sh("schtasks /Query /FO TABLE" if IS_WINDOWS else "crontab -l 2>/dev/null")
    lines = []
    for p in sorted(SCHEDULE_DIR.glob("*.json")):
        d = json.loads(p.read_text(encoding="utf-8"))
        lines.append(
            f"- {d['name']} (id={p.stem}, "
            f"by={d.get('creator', 'unknown')}, "
            f"exec={d.get('executor', EXECUTOR)}, "
            f"time={d['time']}): {d['intent'][:80]}"
        )
    local = "\n".join(lines) or "  (无)"
    return f"[系统调度器]\n{sys_tasks}\n\n[本项目创建的 agent 任务]\n{local}"


def _history(name: str) -> str:
    if not name:
        return "history 需要提供 name。"
    meta = _find_meta(name)
    if meta is None:
        return f"未找到名为 '{name}' 的任务（可能未曾创建，或已被 delete）。"
    log_dir = SCHEDULE_DIR / meta.stem
    logs = sorted(log_dir.glob("*.log"))[-5:] if log_dir.exists() else []
    if not logs:
        return f"任务 '{name}' 还没有执行记录。"
    return "\n\n".join(
        f"===== {p.name} =====\n{p.read_text(encoding='utf-8')}" for p in logs
    )


class Schedule(BaseTool):
    name: str = "schedule"
    description: str = (
        "创建 / 列出 / 删除 / 回看定时任务。**只有 manager 能使用此工具**；"
        "到点后会自动启动一个新 Python 进程，把当时记录的 intent 与 JSON 上下文"
        "以 HumanMessage 形式交给 manager_agent 执行，用于恢复制定任务时的会话状态。"
        "发起者（creator）必须是 'user' / 'agent' / 'unknown' 三者之一。"
    )
    args_schema: Type[BaseModel] = ScheduleInput

    def _run(self, action: str, name: str = "", intent: str = "", time: str = "",
        creator: str = "unknown", context: str = "",
    ) -> str:
        if action == "create":
            return _create(name, intent, time, creator, context, current_thread_id())
        if action == "list":
            return _list()
        if action == "delete":
            return _delete(name)
        if action == "history":
            return _history(name)
        return f"Unknown action '{action}'. Use create / list / delete / history."

    async def _arun(self, action: str, name: str = "", intent: str = "", time: str = "",
        creator: str = "unknown", context: str = "",
    ) -> str:
        return self._run(action, name, intent, time, creator, context)
