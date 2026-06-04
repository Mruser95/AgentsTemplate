from pathlib import Path
from typing import Literal, Optional, Type
from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field

import sys

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from Tools.utils import current_thread_id  # noqa: E402


def _todo_path(thread_id: str) -> Path:
    d = PROJECT_ROOT / "SessionDB" / thread_id
    d.mkdir(parents=True, exist_ok=True)
    return d / "workingTodo.md"


TodoAction = Literal["view", "write_steps", "mark_done", "clear"]


class TodoInput(BaseModel):
    action: TodoAction = Field(
        description="view / write_steps / mark_done / clear"
    )
    subtask_id: Optional[str] = Field(
        default=None,
        description="action=write_steps 必填：本次工作对应的 plan subtask id（如 'm1-t1'）",
    )
    description: Optional[str] = Field(
        default=None,
        description="action=write_steps 必填：本 subtask 的一句话描述",
    )
    steps: Optional[list[str]] = Field(
        default=None,
        description=(
            "action=write_steps 必填：分解后的执行步骤清单（按执行顺序）；"
            "每条 ≤ 80 字、动词开头、可独立勾选完成。"
        ),
    )
    step_index: Optional[int] = Field(
        default=None,
        description="action=mark_done 必填：要标记完成的步骤 1-based 索引",
    )


def _read_text(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _render(subtask_id: str, description: str, steps: list[tuple[str, bool]]) -> str:
    lines = [
        "# Current Working Todo",
        "",
        f"> subtask_id: {subtask_id}",
        f"> description: {description}",
        "",
    ]
    for s, done in steps:
        mark = "x" if done else " "
        lines.append(f"- [{mark}] {s}")
    lines.append("")
    return "\n".join(lines)


def _parse_steps(text: str) -> list[tuple[str, bool]]:
    out: list[tuple[str, bool]] = []
    for line in text.splitlines():
        s = line.rstrip()
        if len(s) >= 6 and s.startswith("- [") and s[4] == "]":
            out.append((s[5:].strip(), s[3].lower() == "x"))
    return out


def _parse_meta(text: str) -> tuple[str, str]:
    subtask_id = ""
    description = ""
    for line in text.splitlines():
        if line.startswith("> subtask_id:"):
            subtask_id = line.split(":", 1)[1].strip()
        elif line.startswith("> description:"):
            description = line.split(":", 1)[1].strip()
    return subtask_id, description


class Todo(BaseTool):
    name: str = "todo"
    description: str = (
        "管理 SessionDB/<thread_id>/workingTodo.md（当前 subtask 的派单清单 / markdown checkbox）。"
        "actions: view（查看当前清单） / write_steps（用一份新清单覆盖文件） / "
        "mark_done（把第 N 步 checkbox 改为 [x]） / clear（清空文件）。"
        "**写权限归 tasker_coder**：每接到一个 subtask，先 write_steps 把派单清单落盘，"
        "每派完一个 dispatch_coder 就立刻 mark_done(对应索引)；任务结束前调 clear。"
        "**manager 只能 view**——用来观察 tasker_coder 当前的派发进度。"
    )
    args_schema: Type[BaseModel] = TodoInput
    read_only: bool = False

    def _run(
        self, action: str, subtask_id: Optional[str] = None, description: Optional[str] = None,
        steps: Optional[list[str]] = None, step_index: Optional[int] = None,
    ) -> str:
        if self.read_only and action != "view":
            return (
                f"todo 当前为只读模式，action='{action}' 被拒绝；只支持 view。"
                "派单清单的写入由 tasker_coder 负责，不要绕过。"
            )

        path = _todo_path(current_thread_id())

        if action == "view":
            text = _read_text(path)
            return text or "(workingTodo.md 为空 / 未创建)"

        if action == "clear":
            _write_text(path, "")
            return "workingTodo.md 已清空。"

        if action == "write_steps":
            if not subtask_id or not description or not steps:
                return "write_steps 需要 subtask_id + description + steps 三者俱全。"
            content = _render(subtask_id, description, [(s.strip(), False) for s in steps])
            _write_text(path, content)
            return content

        if action == "mark_done":
            text = _read_text(path)
            if not text:
                return "workingTodo.md 为空，无法 mark_done。请先 write_steps。"
            if step_index is None or step_index < 1:
                return "mark_done 需要 step_index（1-based）。"
            steps_list = _parse_steps(text)
            if step_index > len(steps_list):
                return f"step_index {step_index} 超出范围（共 {len(steps_list)} 步）。"
            steps_list[step_index - 1] = (steps_list[step_index - 1][0], True)
            sid, desc = _parse_meta(text)
            content = _render(sid, desc, steps_list)
            _write_text(path, content)
            return content

        return f"未知 action '{action}'。可选: view / write_steps / mark_done / clear。"

    async def _arun(
        self, action: str, subtask_id: Optional[str] = None, description: Optional[str] = None,
        steps: Optional[list[str]] = None, step_index: Optional[int] = None,
    ) -> str:
        return self._run(
            action,
            subtask_id=subtask_id,
            description=description,
            steps=steps,
            step_index=step_index,
        )


def todo_inject_text() -> str:
    text = _read_text(_todo_path(current_thread_id())).strip()
    body = text or "(workingTodo.md 为空 / 未创建)"
    return (
        "## 当前 workingTodo.md（自动注入 · 权威 · 必须遵守）\n\n"
        "严格按清单 dispatch_coder（step_index 1:1）；先 write_steps 再派发；"
        "无需 todo view。\n\n"
        f"```markdown\n{body}\n```"
    )
