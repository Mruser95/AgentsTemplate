from __future__ import annotations

import difflib
import sys
from pathlib import Path
from typing import Literal, Type

import yaml
from pydantic import BaseModel, Field, PrivateAttr
from langchain_core.tools import BaseTool

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from Tools.utils import bump_budget, current_thread_id, ensure_workspace, is_inside  # noqa: E402

with open(PROJECT_ROOT / "config.yaml", "r", encoding="utf-8") as _f:
    _cfg = yaml.safe_load(_f) or {}

edit_count_limit: int = int(_cfg.get("edit_count_limit", 50))


Mode = Literal["create", "overwrite", "str_replace", "insert"]


class EditInput(BaseModel):
    path: str = Field(
        description=(
            "目标文件路径，**必须是相对当前 workspace 的相对路径**（如 'image_spider.py'、"
            "'pkg/__init__.py'）；禁止绝对路径或 '../' 越界。父目录不存在会自动创建。"
        )
    )
    content: str = Field(
        default="",
        description="create / overwrite 模式下要写入的完整文本，原样落盘；其他模式忽略。",
    )
    mode: Mode = Field(
        default="create",
        description=(
            "create: 仅当目标不存在时写入（已存在则报错）；"
            "overwrite: 用 content 覆盖整文件；"
            "str_replace: 局部替换，把文件中**唯一出现**的 old_str 改为 new_str（最常用；"
            "replace_all=true 时替换所有匹配）；"
            "insert: 在第 insert_line 行之后插入 new_str（0=文件最前；=总行数=追加到末尾）。"
        ),
    )
    old_str: str | None = Field(
        default=None,
        description="str_replace：要被替换的原始字符串，**必须在文件中唯一出现**（含空白与缩进），除非 replace_all=true。",
    )
    new_str: str | None = Field(
        default=None,
        description="str_replace / insert：替换后 / 要插入的新字符串。可为空串表示删除。",
    )
    insert_line: int | None = Field(
        default=None,
        description="insert：在该 1-based 行号之后插入；0=文件最前，=总行数=追加到末尾。",
    )
    replace_all: bool = Field(
        default=False,
        description="str_replace：true 时替换所有匹配处（适合批量改名）；false（默认）要求唯一匹配。",
    )


class Edit(BaseTool):
    name: str = "edit"
    description: str = (
        "把指定文本写入 / 修改 workspace 内的文件。**优先用 str_replace 做局部修改**，"
        "只在新建或大规模重写时才用 create / overwrite，**不要用 `cat > x << EOF` / "
        "`python3 -c \"open(...).write(...)\"` / `sed -i` 等 shell 写法**——极易因引号 / 换行被截断转义。"
        "参数：path + mode + 对应内容字段（create/overwrite 用 content；"
        "str_replace 用 old_str+new_str；insert 用 insert_line+new_str；追加到末尾即 insert_line=总行数）。"
        "str_replace 的 old_str 从 read_file 输出**原样复制**（含空白缩进）并带足上下文确保唯一；"
        "匹配 0 处会返回最相似片段的 diff，照 diff 修正后重试，不要盲试。"
        "改完用 terminal `python -m py_compile` 自检；"
        "**写过 / 改过的文件必须出现在 CoderReport.file_changes 里**，上层 lint gate 据此跑语法检查。"
    )
    args_schema: Type[BaseModel] = EditInput
    max_tool_calls: int = Field(default=edit_count_limit)
    _call_counts: dict[str, int] = PrivateAttr(default_factory=dict)

    def reset(self) -> None:
        self._call_counts.clear()

    def _resolve(self, path: str, tid: str) -> tuple[Path | None, str]:
        rel = (path or "").strip()
        if not rel:
            return None, "path 不能为空。"
        if rel.startswith("/") or (len(rel) > 1 and rel[1] == ":"):
            return None, f"path 必须是相对路径（workspace 内），收到绝对路径：{rel}"
        ws = ensure_workspace(tid)
        target = (ws / rel).resolve()
        if not is_inside(target, ws):
            return None, f"path 越界 workspace：{rel}"
        return target, ""

    def _do_create(self, target: Path, content: str) -> str:
        if target.exists():
            return f"create 失败：文件已存在 -> {target}。如需覆盖请用 mode='overwrite'。"
        target.write_text(content, encoding="utf-8")
        return f"已创建 ({len(content)} chars) -> {target}"

    def _do_overwrite(self, target: Path, content: str) -> str:
        target.write_text(content, encoding="utf-8")
        return f"已覆盖 ({len(content)} chars) -> {target}"

    @staticmethod
    def _closest_diff(text: str, old_str: str) -> str:
        """0 匹配时找最相似的等行数窗口，返回 ndiff（暴露空白/缩进差）。"""
        file_lines = text.splitlines()
        old_lines = old_str.splitlines() or [""]
        n = len(old_lines)
        matcher = difflib.SequenceMatcher(None, "", old_str, autojunk=False)
        best_ratio, best_i = 0.0, -1
        for i in range(max(len(file_lines) - n + 1, 1)):
            matcher.set_seq1("\n".join(file_lines[i:i + n]))
            if matcher.real_quick_ratio() <= best_ratio or matcher.quick_ratio() <= best_ratio:
                continue
            r = matcher.ratio()
            if r > best_ratio:
                best_ratio, best_i = r, i
        if best_i < 0 or best_ratio < 0.5:
            return "未找到相似片段，请重新 read_file 后原样复制。"
        snippet = file_lines[best_i:best_i + n]
        diff = "\n".join(difflib.ndiff(old_lines, snippet))
        return (
            f"最相似片段在第 {best_i + 1}-{best_i + n} 行（相似度 {best_ratio:.0%}）。"
            f"差异（- 你给的 old_str / + 文件实际，? 标记位置）：\n{diff}"
        )

    def _do_str_replace(
        self, target: Path, old_str: str | None, new_str: str | None, replace_all: bool = False,
    ) -> str:
        if not target.exists():
            return f"str_replace 失败：文件不存在 -> {target}"
        if not old_str:
            return "str_replace 失败：必须提供非空 old_str。"
        text = target.read_text(encoding="utf-8")
        count = text.count(old_str)
        repl = new_str or ""
        if count == 0:
            return f"str_replace 失败：old_str 匹配 0 处。{self._closest_diff(text, old_str)}"
        if count > 1 and not replace_all:
            starts, pos = [], 0
            while (idx := text.find(old_str, pos)) != -1:
                starts.append(text.count("\n", 0, idx) + 1)
                pos = idx + len(old_str)
            return (
                f"str_replace 失败：old_str 匹配 {count} 处（起始行：{', '.join(map(str, starts))}）。"
                "加更多上下文使其唯一，或用 replace_all=true 全部替换。"
            )
        target.write_text(text.replace(old_str, repl), encoding="utf-8")
        scope = f"全部 {count} 处" if count > 1 else ""
        return f"已替换{scope} ({len(old_str)} -> {len(repl)} chars) -> {target}"

    def _do_insert(self, target: Path, insert_line: int | None, new_str: str | None) -> str:
        if not target.exists():
            return f"insert 失败：文件不存在 -> {target}"
        if insert_line is None or new_str is None:
            return "insert 失败：必须同时提供 insert_line 与 new_str。"
        lines = target.read_text(encoding="utf-8").splitlines(keepends=True)
        if not 0 <= insert_line <= len(lines):
            return f"insert 失败：insert_line={insert_line} 越界（文件共 {len(lines)} 行）。"
        payload = new_str if new_str.endswith("\n") or insert_line == len(lines) else new_str + "\n"
        lines.insert(insert_line, payload)
        target.write_text("".join(lines), encoding="utf-8")
        return f"已在第 {insert_line} 行后插入 ({len(payload)} chars) -> {target}"

    def _run(
        self, path: str, content: str = "", mode: Mode = "create",
        old_str: str | None = None, new_str: str | None = None, insert_line: int | None = None,
        replace_all: bool = False,
    ) -> str:
        tid = current_thread_id()
        ok, n, rem = bump_budget(self._call_counts, tid, self.max_tool_calls)
        if not ok:
            return (
                f"Tool call limit reached ({self.max_tool_calls}) for thread {tid}. "
                "edit 预算耗尽；停止写入，直接产出 CoderReport。"
            )
        target, err = self._resolve(path, tid)
        if err:
            body = err
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            if mode == "create":
                body = self._do_create(target, content)
            elif mode == "overwrite":
                body = self._do_overwrite(target, content)
            elif mode == "str_replace":
                body = self._do_str_replace(target, old_str, new_str, replace_all)
            elif mode == "insert":
                body = self._do_insert(target, insert_line, new_str)
            else:
                body = f"未知 mode: {mode}"
        return f"{body}\n\n[Tool call {n}/{self.max_tool_calls}, remaining: {rem}]"

    async def _arun(
        self, path: str, content: str = "", mode: Mode = "create",
        old_str: str | None = None, new_str: str | None = None, insert_line: int | None = None,
        replace_all: bool = False,
    ) -> str:
        return self._run(path, content, mode, old_str, new_str, insert_line, replace_all)
