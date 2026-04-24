from __future__ import annotations

import asyncio
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable
from pydantic import BaseModel, Field


def _py(p: Path) -> list[str]:
    return [sys.executable, "-m", "py_compile", str(p)]

def _js(p: Path) -> list[str]:
    return ["node", "--check", str(p)]

def _go(p: Path) -> list[str]:
    return ["gofmt", "-e", str(p)]

def _java(p: Path) -> list[str]:
    return [
        "javac", "-Xlint:all",
        "-d", tempfile.gettempdir(),
        "-sourcepath", str(p.parent),
        str(p),
    ]

def _c(p: Path) -> list[str]:
    return ["gcc", "-fsyntax-only", "-Wall", "-Wextra", str(p)]

def _c_header(p: Path) -> list[str]:
    return ["gcc", "-fsyntax-only", "-Wall", "-Wextra", "-x", "c", str(p)]

def _cxx(p: Path) -> list[str]:
    return ["g++", "-fsyntax-only", "-Wall", "-Wextra", str(p)]

def _cxx_header(p: Path) -> list[str]:
    return ["g++", "-fsyntax-only", "-Wall", "-Wextra", "-x", "c++", str(p)]

_LINTERS: dict[str, Callable[[Path], list[str]]] = {
    ".py":  _py,
    ".js":  _js, ".mjs": _js, ".cjs": _js,
    ".go":  _go,
    ".java": _java,
    ".c":   _c,  ".h":   _c_header,
    ".cpp": _cxx, ".cc":  _cxx, ".cxx": _cxx,
    ".hpp": _cxx_header, ".hxx": _cxx_header, ".hh": _cxx_header,
}


class LintEntry(BaseModel):
    path: str
    command: str
    exit_code: int
    passed: bool
    output: str = ""
    skipped_reason: str = ""


class LintOutcome(BaseModel):
    passed: bool
    entries: list[LintEntry] = Field(default_factory=list)

    def errors_digest(self, max_errors: int = 5) -> str:
        failed = [e for e in self.entries if not e.passed]
        if not failed:
            return "所有文件 lint 通过。"
        lines = [f"共 {len(failed)} 个文件未通过 lint："]
        for e in failed[:max_errors]:
            lines.append(f"\n--- {e.path} (cmd=`{e.command}`, exit={e.exit_code}) ---")
            lines.append(e.output.strip() or "(无输出)")
        if len(failed) > max_errors:
            lines.append(f"\n...另有 {len(failed) - max_errors} 个文件未列出")
        return "\n".join(lines)


async def _lint_one(p: Path, timeout: int) -> LintEntry | None:
    if not p.is_file():
        return None
    builder = _LINTERS.get(p.suffix.lower())
    if builder is None:
        return None
    cmd = builder(p)
    exe = cmd[0]
    if shutil.which(exe) is None:
        return LintEntry(
            path=str(p), command=" ".join(cmd),
            exit_code=-1, passed=True,
            skipped_reason=f"未安装 {Path(exe).name}，跳过",
        )
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return LintEntry(
            path=str(p), command=" ".join(cmd),
            exit_code=-1, passed=False,
            output=f"lint 执行超时（>{timeout}s）",
        )
    output = (stderr_b.decode(errors="replace") or stdout_b.decode(errors="replace") or "").strip()
    return LintEntry(
        path=str(p), command=" ".join(cmd),
        exit_code=proc.returncode, passed=proc.returncode == 0,
        output=output,
    )


async def alint_paths(paths: list[str], *, timeout: int = 30) -> LintOutcome:
    tasks = [_lint_one(Path(raw), timeout) for raw in paths]
    results = await asyncio.gather(*tasks)
    entries = [e for e in results if e is not None]
    blocking = [e for e in entries if not e.passed]
    return LintOutcome(passed=not blocking, entries=entries)


def _run_coro_sync(coro: Any) -> Any:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    import threading
    result: dict = {}
    def runner():
        try:
            result["value"] = asyncio.run(coro)
        except BaseException as e:
            result["error"] = e
    t = threading.Thread(target=runner, daemon=True)
    t.start()
    t.join()
    if "error" in result:
        raise result["error"]
    return result.get("value")


def lint_paths(paths: list[str], *, timeout: int = 30) -> LintOutcome:
    return _run_coro_sync(alint_paths(paths, timeout=timeout))
