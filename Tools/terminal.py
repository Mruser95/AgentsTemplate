import re
import os
import sys
import subprocess
import locale
import asyncio
from typing import Type
from pathlib import Path
import yaml
from pydantic import BaseModel, Field, PrivateAttr
from langchain_core.tools import BaseTool
from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, SystemMessage
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
load_dotenv(PROJECT_ROOT / '.env')

from Tools._context import bump_budget, current_thread_id  # noqa: E402

with open(PROJECT_ROOT / 'config.yaml', 'r', encoding='utf-8') as file:
    config = yaml.safe_load(file)

shell_restriction: bool = config['shell_restriction']
shell_permissions: list[str] = [var.strip() for cmds in config['shell_permissions'].values() for var in cmds]
shell_count_limit: int = config['shell_count_limit']
checker_prompt: str = config['shell_checker_prompt']


class SafeShellInput(BaseModel):
    command: str = Field(description="Shell command(s) to execute")

class CheckerOutput(BaseModel):
    allowed: bool = Field(description="Whether the command is allowed to execute")
    reason: str = Field(description="If not allowed, the reason why")


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


llm = ChatOpenAI(
    model=os.getenv("small_llm_model"),
    api_key=os.getenv("small_llm_key"),
    base_url=os.getenv("small_llm_base_url"),
)


def _decode(data: bytes) -> str:
    # Windows 中文系统 cmd.exe 输出是 GBK/CP936，先试 UTF-8 再回落本地编码
    if not data:
        return ""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode(locale.getpreferredencoding(False), errors="replace")


def _run_subprocess(command: str, timeout: int = 30) -> str:
    try:
        proc = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            timeout=timeout,
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


def _execute(command: str, timeout: int = 30) -> str:
    allowed, denied = check_command(command)
    if not allowed:
        return f"Command denied, contains unauthorized commands: {', '.join(denied)}"
    response = llm.with_structured_output(CheckerOutput).invoke(
        [SystemMessage(content=checker_prompt), HumanMessage(content=command)]
    )
    if not response.allowed:
        return f"Command denied by checker agent: {response.reason}"
    return _run_subprocess(command, timeout)


async def _execute_async(command: str, timeout: int = 30) -> str:
    allowed, denied = check_command(command)
    if not allowed:
        return f"Command denied, contains unauthorized commands: {', '.join(denied)}"
    response = await llm.with_structured_output(CheckerOutput).ainvoke(
        [SystemMessage(content=checker_prompt), HumanMessage(content=command)]
    )
    if not response.allowed:
        return f"Command denied by checker agent: {response.reason}"
    # subprocess 本身是阻塞 IO，放线程池；LLM 安全检查已经是 async，不再绕线程
    return await asyncio.to_thread(_run_subprocess, command, timeout)


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

    def _run(self, command: str) -> str:
        tid = current_thread_id()
        ok, n, rem = bump_budget(self._call_counts, tid, self.max_tool_calls)
        if not ok:
            return self._budget_response(tid)
        result = _execute(command)
        return f"{result}\n\n[Tool call {n}/{self.max_tool_calls}, remaining: {rem}]"

    async def _arun(self, command: str) -> str:
        tid = current_thread_id()
        ok, n, rem = bump_budget(self._call_counts, tid, self.max_tool_calls)
        if not ok:
            return self._budget_response(tid)
        result = await _execute_async(command)
        return f"{result}\n\n[Tool call {n}/{self.max_tool_calls}, remaining: {rem}]"
