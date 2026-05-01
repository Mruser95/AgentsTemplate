from __future__ import annotations

import sys
from pathlib import Path
from typing import Literal, Type

from pydantic import BaseModel, Field, PrivateAttr
from langchain_core.tools import BaseTool

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from Tools.utils import bump_budget, current_thread_id, ensure_workspace, is_inside  # noqa: E402


Mode = Literal["create", "overwrite", "append"]


class EditInput(BaseModel):
    path: str = Field(
        description=(
            "目标文件路径，**必须是相对当前 workspace 的相对路径**（如 'image_spider.py'、"
            "'pkg/__init__.py'）；禁止绝对路径或 '../' 越界。父目录不存在会自动创建。"
        )
    )
    content: str = Field(
        description="要写入的完整文本内容，原样落盘，不做任何转义 / 拼接 / shell quoting。",
    )
    mode: Mode = Field(
        default="create",
        description=(
            "create: 仅当目标不存在时写入（已存在则报错）；"
            "overwrite: 无论是否存在都用 content 覆盖整文件；"
            "append: 追加到文件末尾（不存在时新建）。"
        ),
    )


class Edit(BaseTool):
    name: str = "edit"
    description: str = (
        "把指定文本内容直接写入 workspace 内的文件。**写新文件 / 整文件覆盖时优先用本工具，"
        "不要再用 `cat > x << EOF` / `python3 -c \"open(...).write(...)\"` 等 shell 写法**——"
        "那些写法极易因引号 / 换行 / 嵌套字符串被截断或转义导致语法错误（已多次发生）。"
        "参数：path（workspace 内相对路径）+ content（原样落盘的全文）+ "
        "mode（create / overwrite / append）。"
        "**用本工具写过 / 改过的文件，必须出现在你 CoderReport.file_changes 里**，"
        "上层 lint gate 会据此跑语法检查。"
    )
    args_schema: Type[BaseModel] = EditInput
    max_tool_calls: int = Field(default=50)
    _call_counts: dict[str, int] = PrivateAttr(default_factory=dict)

    def reset(self) -> None:
        self._call_counts.clear()

    def _budget_response(self, tid: str) -> str:
        return (
            f"Tool call limit reached ({self.max_tool_calls}) for thread {tid}. "
            "edit 预算耗尽；停止写入，直接产出 CoderReport。"
        )

    def _resolve(self, path: str, tid: str) -> tuple[Path | None, str]:
        if not path or not path.strip():
            return None, "path 不能为空。"
        rel = path.strip()
        if rel.startswith("/") or (len(rel) > 1 and rel[1] == ":"):
            return None, f"path 必须是相对路径（workspace 内），收到绝对路径：{rel}"
        ws = ensure_workspace(tid)
        target = (ws / rel).resolve()
        if not is_inside(target, ws):
            return None, f"path 越界 workspace：{rel}"
        return target, ""

    def _write(self, target: Path, content: str, mode: Mode) -> str:
        target.parent.mkdir(parents=True, exist_ok=True)
        if mode == "create":
            if target.exists():
                return f"create 失败：文件已存在 -> {target}。如需覆盖请用 mode='overwrite'。"
            target.write_text(content, encoding="utf-8")
            return f"已创建 ({len(content)} chars) -> {target}"
        if mode == "overwrite":
            target.write_text(content, encoding="utf-8")
            return f"已覆盖 ({len(content)} chars) -> {target}"
        if mode == "append":
            with target.open("a", encoding="utf-8") as f:
                f.write(content)
            return f"已追加 ({len(content)} chars) -> {target}"
        return f"未知 mode: {mode}"

    def _format(self, body: str, n: int, rem: int) -> str:
        return f"{body}\n\n[Tool call {n}/{self.max_tool_calls}, remaining: {rem}]"

    def _run(self, path: str, content: str, mode: Mode = "create") -> str:
        tid = current_thread_id()
        ok, n, rem = bump_budget(self._call_counts, tid, self.max_tool_calls)
        if not ok:
            return self._budget_response(tid)
        target, err = self._resolve(path, tid)
        if err:
            return self._format(err, n, rem)
        return self._format(self._write(target, content, mode), n, rem)

    async def _arun(self, path: str, content: str, mode: Mode = "create") -> str:
        return self._run(path, content, mode)
