import re
import sys
import subprocess
import locale
import asyncio
from typing import Type
from pathlib import Path
import yaml
from pydantic import BaseModel, Field, PrivateAttr
from langchain_core.tools import BaseTool
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
load_dotenv(PROJECT_ROOT / '.env')

from Tools.utils import bump_budget, current_thread_id, ensure_workspace, workspace_env  # noqa: E402
from Tools.terminal_agent import checker_agent, summarizer_agent  # noqa: E402

with open(PROJECT_ROOT / 'config.yaml', 'r', encoding='utf-8') as file:
    config = yaml.safe_load(file)

shell_restriction: bool = config['shell_restriction']
shell_permissions: list[str] = [var.strip() for cmds in config['shell_permissions'].values() for var in cmds]
shell_count_limit: int = config['shell_count_limit']
shell_default_timeout: int = int(config.get('shell_default_timeout', 120))
shell_max_timeout: int = int(config.get('shell_max_timeout', 600))


class SafeShellInput(BaseModel):
    command: str = Field(description="Shell command(s) to execute")
    timeout: int | None = Field(
        default=None,
        description=(
            f"Optional per-command timeout in seconds. Default {shell_default_timeout}s, "
            f"capped at {shell_max_timeout}s. Bump it for slow operations like "
            "pip install, network downloads, or long-running tests."
        ),
    )


def parse_commands(command: str) -> list[str]:
    # 先剥掉命令替换 $(...) 和反引号，避免嵌套绕过
    cleaned = re.sub(r'\$\([^)]*\)', '', command)
    cleaned = re.sub(r'`[^`]*`', '', cleaned)
    parts = re.split(r'\s*(?:&&|\|\||[;|])\s*', cleaned)

    base_commands: list[str] = []
    for part in parts:
        part = part.strip()
        if not part:
            continue
        for token in part.split():
            # 跳过 VAR=value 这种前置赋值
            if re.match(r'^[A-Za-z_][A-Za-z0-9_]*=', token):
                continue
            base_commands.append(token.rsplit('/', 1)[-1].rsplit('\\', 1)[-1])
            break
    return base_commands


def check_command(command: str) -> tuple[bool, list[str]]:
    if not shell_restriction:
        return True, []
    base_commands = parse_commands(command)
    denied = [cmd for cmd in base_commands if cmd not in shell_permissions]
    return len(denied) == 0, denied


def _decode(data: bytes) -> str:
    # Windows 中文系统 cmd.exe 输出是 GBK/CP936，先试 UTF-8 再回落本地编码
    if not data:
        return ""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode(locale.getpreferredencoding(False), errors="replace")


def _run_subprocess(command: str, timeout: int | None = None, cwd: str | None = None, env: dict | None = None) -> str:
    timeout = min(int(timeout), shell_max_timeout) if timeout and timeout > 0 else shell_default_timeout
    try:
        proc = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            timeout=timeout,
            cwd=cwd,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return f"Command timed out after {timeout}s"
    except Exception as e:
        return f"Command failed: {e!r}"

    stdout = _decode(proc.stdout)
    stderr = _decode(proc.stderr)
    if proc.returncode != 0:
        return f"[exit={proc.returncode}]\n{stdout}{stderr}"
    return stdout + (f"\n{stderr}" if stderr.strip() else "")


def _execute(command: str, timeout: int | None = None,cwd: str | None = None,env: dict | None = None) -> str:
    allowed, denied = check_command(command)
    if not allowed:
        return f"Command denied, contains unauthorized commands: {', '.join(denied)}"
    # 仅在开启 shell_restriction 时跑 LLM 二次审查；关闭时白名单已是兜底，跳过可省每条 3-8s 延迟
    if shell_restriction:
        response = checker_agent.check(command)
        if not response.allowed:
            return f"Command denied by checker agent: {response.reason}"
    output = _run_subprocess(command, timeout, cwd=cwd, env=env)
    return summarizer_agent.summarize(command, output)


async def _execute_async(command: str, timeout: int | None = None, cwd: str | None = None, env: dict | None = None) -> str:
    allowed, denied = check_command(command)
    if not allowed:
        return f"Command denied, contains unauthorized commands: {', '.join(denied)}"
    if shell_restriction:
        response = await checker_agent.acheck(command)
        if not response.allowed:
            return f"Command denied by checker agent: {response.reason}"
    output = await asyncio.to_thread(_run_subprocess, command, timeout, cwd, env)
    return await summarizer_agent.asummarize(command, output)


class SafeShell(BaseTool):
    name: str = "terminal"
    description: str = "Run shell commands in a sandboxed terminal (allow-list enforced)."
    args_schema: Type[BaseModel] = SafeShellInput
    max_tool_calls: int = Field(default=shell_count_limit, description="Maximum number of allowed tool calls per thread")
    _call_counts: dict[str, int] = PrivateAttr(default_factory=dict)

    def reset(self):
        self._call_counts.clear()

    def _budget_response(self, tid: str) -> str:
        return (
            f"Tool call limit reached ({self.max_tool_calls}) for thread {tid}. "
            "Stop using this tool and respond directly."
        )

    def _run(self, command: str, timeout: int | None = None) -> str:
        tid = current_thread_id()
        ok, n, rem = bump_budget(self._call_counts, tid, self.max_tool_calls)
        if not ok:
            return self._budget_response(tid)
        result = _execute(
            command,
            timeout=timeout,
            cwd=str(ensure_workspace(tid)),
            env=workspace_env(tid),
        )
        return f"{result}\n\n[Tool call {n}/{self.max_tool_calls}, remaining: {rem}]"

    async def _arun(self, command: str, timeout: int | None = None) -> str:
        tid = current_thread_id()
        ok, n, rem = bump_budget(self._call_counts, tid, self.max_tool_calls)
        if not ok:
            return self._budget_response(tid)
        result = await _execute_async(
            command,
            timeout=timeout,
            cwd=str(ensure_workspace(tid)),
            env=workspace_env(tid),
        )
        return f"{result}\n\n[Tool call {n}/{self.max_tool_calls}, remaining: {rem}]"
