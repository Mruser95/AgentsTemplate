import argparse
import asyncio
import json
import locale
import platform
import subprocess
import sys
import traceback
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

from Tools.utils import current_thread_id  # noqa: E402

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


# 模块 1：通用工具 ================================================================


def _decode(b: bytes) -> str:
    if not b:
        return ""
    try:
        return b.decode("utf-8")
    except UnicodeDecodeError:
        return b.decode(locale.getpreferredencoding(False), errors="replace")


# 模块 2：Schedule 工具（manager 调用入口） =========================================


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
            "不得违反的约束等。到点时本模块的 runner 会连同 intent 一起塞给 "
            "manager_agent，用于恢复制定任务时的会话状态。"
            "示例：'{\"background\":\"...\",\"purpose\":\"...\",\"constraints\":[...]}'"
        ),
    )


def _sh(cmd: str) -> str:
    p = subprocess.run(cmd, shell=True, capture_output=True, timeout=30)
    out = (_decode(p.stdout) + _decode(p.stderr)).strip()
    return out or f"[exit={p.returncode}]"


def _write_runner(task_id: str) -> Path:
    # 绕开 schtasks /TR 的嵌套引号规则。
    if IS_WINDOWS:
        p = SCHEDULE_DIR / f"{task_id}.bat"
        p.write_text(
            f'@echo off\ncd /d "{PROJECT_ROOT}"\n"{PYTHON}" -m Tools.schedule --task {task_id}\n',
            encoding="utf-8",
        )
    else:
        p = SCHEDULE_DIR / f"{task_id}.sh"
        p.write_text(
            f'#!/bin/sh\ncd "{PROJECT_ROOT}" && "{PYTHON}" -m Tools.schedule --task {task_id}\n',
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
        "发起者（creator）必须是 'user' / 'agent' / 'unknown' 三者之一，不得伪造。"
        "create/delete 会真实改系统调度器（Windows schtasks / Unix crontab），参数核对后再调："
        "intent 写具体可执行的事（醒来的 manager 只看得到 intent+context）；"
        "context 必须是合法 JSON 字符串（否则降级 {raw} 丢结构）；"
        "name 仅字母数字连字符；time 为 Windows 'HH:MM' / Unix 5 段 cron。"
        "到点失败不自愈——下次对话主动 history 回看执行日志。"
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


# 模块 3：Runner（被 cron / schtasks 启动的子进程入口） ====================================


_CREATOR_LABEL = {
    "user": "用户明确要求 manager 制定",
    "agent": "manager 在会话中自主决定制定",
    "unknown": "来源未知",
}

def _build_prompt(tid: str, m: dict) -> str:
    cr = m.get("creator", "unknown")
    ctx_json = json.dumps(m.get("context") or {}, ensure_ascii=False, indent=2)
    return (
        f"【定时任务 · {m.get('name', '(未命名)')}】\n"
        f"你（manager）在 {m.get('created_at', '(未知时间)')} 登记了这条定时任务，"
        f"现在到点被自动唤醒。\n"
        f"任务 ID：{tid}\n"
        f"发起者：{cr}（{_CREATOR_LABEL.get(cr, '(非约定枚举值)')}）\n"
        f"执行者：manager（本工程里唯一被允许执行定时任务的 agent）\n"
        f"\n---\n"
        f"原始意图（到点要做什么）：\n{m.get('intent', '')}\n"
        f"\n制定任务时记录下来的会话上下文（背景 / 目的 / 约束，JSON）：\n"
        f"```json\n{ctx_json}\n```\n"
        f"---\n\n"
        f"请把上面的上下文视作\u201c制定这条任务时的会话状态\u201d，据此恢复当时的思路并"
        f"按原始意图执行；完成后给出简短总结。"
    )


async def _run_task(tid: str) -> None:
    # 延迟 import：manager 模块会加载 checkpointer，避免在参数解析阶段就强制初始化
    from Agents.manager import manager_session

    meta = json.loads((SCHEDULE_DIR / f"{tid}.json").read_text(encoding="utf-8"))
    log_dir = SCHEDULE_DIR / tid
    log_dir.mkdir(exist_ok=True)
    log_path = log_dir / f"{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    stamp = datetime.now().isoformat(timespec="seconds")
    try:
        prompt = _build_prompt(tid, meta)
        thread_id = meta.get("thread_id") or f"schedule:{tid}"
        async with manager_session(thread_id=thread_id) as sess:
            result = await sess.ainvoke(prompt)
        last = result["messages"][-1]
        log_path.write_text(
            f"[OK] {stamp} thread_id={thread_id}\n\n{getattr(last, 'content', str(last))}",
            encoding="utf-8",
        )
    except Exception:
        log_path.write_text(f"[ERR] {stamp}\n\n{traceback.format_exc()}", encoding="utf-8")
        raise


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", required=True, help="创建任务时生成的 task_id")
    asyncio.run(_run_task(ap.parse_args().task))


if __name__ == "__main__":
    main()
