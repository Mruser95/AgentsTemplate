import re
import sys
import shlex
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
from Tools.terminal_agent import checker_agent  # noqa: E402

with open(PROJECT_ROOT / 'config.yaml', 'r', encoding='utf-8') as file:
    config = yaml.safe_load(file)

shell_restriction: bool = config['shell_restriction']
shell_permissions: list[str] = [var.strip() for cmds in config['shell_permissions'].values() for var in cmds]
shell_count_limit: int = config['shell_count_limit']
shell_default_timeout: int = int(config.get('shell_default_timeout', 120))
shell_max_timeout: int = int(config.get('shell_max_timeout', 600))
shell_output_max_length: int = int(config.get('shell_output_max_length', 3000))


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


def _truncate(output: str) -> str:
    # 超长输出直接头尾截断（不再做 LLM 压缩）：错误多在尾部 traceback，头尾各留一半
    if shell_output_max_length <= 0 or len(output) <= shell_output_max_length:
        return output
    half = shell_output_max_length // 2
    head, tail = output[:half], output[-half:]
    omitted = len(output) - len(head) - len(tail)
    return f"{head}\n...[{omitted} chars truncated, exceeded {shell_output_max_length} limit]...\n{tail}"


# ─── requirements.txt 自动登记 ──────────────────────────────────────────────
_PIP_INSTALL_RX = re.compile(
    r'(?:^|\s)(?:pip[0-9.]*\s+install|python[0-9.]*\s+-m\s+pip\s+install|uv\s+pip\s+install)(?:\s|$)'
)
# 带独立取值的 flag：其后一个 token 是值不是包名，需一并跳过
_PIP_VALUE_FLAGS = {
    "-r", "--requirement", "-c", "--constraint", "-i", "--index-url", "--extra-index-url",
    "-t", "--target", "-f", "--find-links", "--no-binary", "--only-binary", "--root",
    "--prefix", "--progress-bar", "--python-version", "--platform", "--abi", "--implementation",
}
# 不该写进清单的 token：工具链自身 / 本地目录安装
_PIP_SKIP_TOKENS = {"pip", "setuptools", "wheel", ".", ".."}
# shell 控制 / 重定向操作符：pip 包参数应在此截断（其后是管道 / 重定向 / 其它命令，不是包）
_SHELL_OP_TOKENS = {"|", "||", "&", "&&", ";", ">", ">>", "<", "<<", "<<<", "|&", "&>", ">&"}
_REDIR_RX = re.compile(r'^\d*[<>]+&?\d*$')  # 2>&1 / 2> / 1> / >&2 等


def _is_shell_op(tok: str) -> bool:
    """token 是否为 shell 控制 / 重定向操作符。版本规格如 `foo>=1.0` 以字母开头，不在此列。"""
    return tok in _SHELL_OP_TOKENS or bool(_REDIR_RX.match(tok)) or tok[:1] in "<>"


def _pip_packages(subcmd: str) -> list[str]:
    """从单条 `pip install ...` 子命令里抽出用户请求的包规格（保留版本/extras）。
    遇到 shell 操作符（`|` `>` `2>&1` 等）即停止——其后不是包名，避免把命令碎片写进 requirements。"""
    try:
        tokens = shlex.split(subcmd)
    except ValueError:
        return []
    if "install" not in tokens:
        return []
    pkgs: list[str] = []
    skip_next = False
    for tok in tokens[tokens.index("install") + 1:]:
        if _is_shell_op(tok):
            break
        if skip_next:
            skip_next = False
            continue
        if tok in _PIP_VALUE_FLAGS:
            skip_next = True
            continue
        if tok.startswith("-") or tok in _PIP_SKIP_TOKENS:
            continue
        pkgs.append(tok)
    return pkgs


def _req_name(spec: str) -> str:
    """规格里的分发名部分（去版本/extras/marker），归一化用于去重。"""
    m = re.match(r'[A-Za-z0-9._-]+', spec.strip())
    return m.group(0).lower().replace("_", "-") if m else spec.strip().lower()


def _record_requirements(command: str, output: str, cwd: str | None) -> None:
    """pip install 成功后，把新装的包并入 cwd/requirements.txt（去重、保序、保留已有行）。"""
    if not cwd or not isinstance(output, str):
        return
    if output.startswith(("[exit=", "Command timed out", "Command failed")):
        return  # 仅登记成功的安装
    specs: list[str] = []
    for sub in re.split(r'&&|\|\||;|\n', command):
        if _PIP_INSTALL_RX.search(sub):
            specs.extend(_pip_packages(sub))
    if not specs:
        return
    # 文件 I/O 全程兜底：登记失败绝不能影响命令执行/返回
    try:
        req_path = Path(cwd) / "requirements.txt"
        lines = req_path.read_text(encoding="utf-8", errors="ignore").splitlines() if req_path.is_file() else []
        seen = {_req_name(s) for s in lines if s.strip() and not s.strip().startswith("#")}
        added = False
        for spec in specs:
            name = _req_name(spec)
            if name and name not in seen:
                seen.add(name)
                lines.append(spec)
                added = True
        if not added:
            return
        while lines and not lines[-1].strip():
            lines.pop()
        req_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError:
        pass


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
    _record_requirements(command, output, cwd)
    return _truncate(output)


async def _execute_async(command: str, timeout: int | None = None, cwd: str | None = None, env: dict | None = None) -> str:
    allowed, denied = check_command(command)
    if not allowed:
        return f"Command denied, contains unauthorized commands: {', '.join(denied)}"
    if shell_restriction:
        response = await checker_agent.acheck(command)
        if not response.allowed:
            return f"Command denied by checker agent: {response.reason}"
    output = await asyncio.to_thread(_run_subprocess, command, timeout, cwd, env)
    _record_requirements(command, output, cwd)
    return _truncate(output)


class SafeShell(BaseTool):
    name: str = "terminal"
    description: str = (
        "在受限沙箱执行 shell 命令（白名单 + LLM 二次审查）。仅用于运行 / 测试 / 必要系统命令："
        "**禁止用 shell 写改文件**（cat>EOF / echo> / sed -i → 一律用 edit）；"
        "找符号 / 文件用 repo_map/grep/glob，不用 rg/find。单条命令优先，仅强依赖时才 && 串联；"
        "被拒（Command denied）不要变形 / 编码绕过——换思路或如实上报。"
        "返回末尾 [Tool call X/N] 是预算，remaining ≤ 2 时停止探索性调用。"
    )
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
