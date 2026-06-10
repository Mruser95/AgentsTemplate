from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Type

import yaml
from pydantic import BaseModel, Field, PrivateAttr
from langchain_core.tools import BaseTool

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from Tools.utils import bump_budget, current_thread_id, ensure_workspace  # noqa: E402

with open(PROJECT_ROOT / "config.yaml", "r", encoding="utf-8") as _f:
    _cfg = yaml.safe_load(_f) or {}

READ_COUNT_LIMIT: int = int(_cfg.get("read_count_limit", 30))
READ_DEFAULT_LIMIT: int = int(_cfg.get("read_default_limit", 2000))
READ_MAX_CHARS: int = int(_cfg.get("read_output_max_chars", 50000))


def _render_page(lines: list[str], offset: int, limit: int, max_chars: int) -> tuple[str, int, bool]:
    """渲染 cat -n 格式页：返回 (文本, 下一未读行号(1-based), 是否因字符上限提前停)。"""
    end = min(offset + limit - 1, len(lines))
    buf: list[str] = []
    size, n, capped = 0, offset, False
    while n <= end:
        row = f"{n:6d}\t{lines[n - 1]}\n"
        if buf and size + len(row) > max_chars:  # 至少返回 1 行，避免单行超限卡死
            capped = True
            break
        buf.append(row)
        size += len(row)
        n += 1
    return "".join(buf), n, capped


class ReadInput(BaseModel):
    path: str = Field(
        description="文件路径：workspace 内相对路径，或绝对路径（读项目外参考代码用）。"
    )
    offset: int | None = Field(
        default=None, description="起始行号（1-based）；不填默认从第 1 行。"
    )
    limit: int | None = Field(
        default=None, description=f"读取行数；不填默认最多 {READ_DEFAULT_LIMIT} 行。"
    )


class Read(BaseTool):
    name: str = "read_file"
    description: str = (
        f"按 `cat -n` 格式（行号+制表符+内容）读文件，默认从头读最多 {READ_DEFAULT_LIMIT} 行，"
        "用 offset(起始行)+limit(行数) 翻页。整文件超字符上限时只返回第一页并附 [PARTIAL] "
        "提示（用 offset 续读，内容不丢）；**已显式给 offset/limit 仍超限则直接报错**，不静默截断。"
    )
    args_schema: Type[BaseModel] = ReadInput
    max_tool_calls: int = Field(default=READ_COUNT_LIMIT)
    _call_counts: dict[str, int] = PrivateAttr(default_factory=dict)

    def reset(self) -> None:
        self._call_counts.clear()

    def _resolve(self, path: str, tid: str) -> tuple[Path | None, str]:
        p = (path or "").strip()
        if not p:
            return None, "path 不能为空。"
        raw = Path(p)
        target = raw.resolve() if raw.is_absolute() else (ensure_workspace(tid) / p).resolve()
        if not target.exists():
            return None, f"文件不存在：{path}"
        if not target.is_file():
            return None, f"不是文件：{path}"
        return target, ""

    def _read(self, path: str, offset: int | None, limit: int | None, tid: str) -> str:
        target, err = self._resolve(path, tid)
        if err:
            return err
        explicit = offset is not None or limit is not None
        off = offset if offset is not None else 1
        lim = limit if limit is not None else READ_DEFAULT_LIMIT
        if off < 1 or lim < 1:
            return "offset 与 limit 必须为正整数（1-based）。"
        try:
            lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError as e:
            return f"读取失败：{e}"
        total = len(lines)
        if total == 0:
            return f"[空文件] {path}"
        if off > total:
            return f"offset={off} 越界：文件共 {total} 行。"
        page, nxt, capped = _render_page(lines, off, lim, READ_MAX_CHARS)
        if capped and explicit:
            return (
                f"[ERROR] 指定范围 offset={off}, limit={lim} 渲染后超过 {READ_MAX_CHARS} 字符上限"
                f"（读到第 {nxt - 1} 行即超）。请减小 limit 或调整 offset 分页读。"
            )
        body = f"# {path}  (行 {off}-{nxt - 1} / 共 {total})\n{page.rstrip(chr(10))}"
        if nxt <= total:
            body += (
                f"\n\n[PARTIAL] 还有 {total - (nxt - 1)} 行未读；"
                f"续读请调用 read_file(path, offset={nxt}, limit={lim})。"
            )
        return body

    def _run(self, path: str, offset: int | None = None, limit: int | None = None) -> str:
        tid = current_thread_id()
        ok, n, rem = bump_budget(self._call_counts, tid, self.max_tool_calls)
        if not ok:
            return f"Tool call limit reached ({self.max_tool_calls}) for thread {tid}. read_file 预算耗尽。"
        body = self._read(path, offset, limit, tid)
        return f"{body}\n\n[Tool call {n}/{self.max_tool_calls}, remaining: {rem}]"

    async def _arun(self, path: str, offset: int | None = None, limit: int | None = None) -> str:
        return await asyncio.to_thread(self._run, path, offset, limit)  # 文件 IO 不阻塞 event loop
