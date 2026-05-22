from __future__ import annotations

import asyncio
import os
import re
import sys
from pathlib import Path

import yaml
from dotenv import load_dotenv
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
load_dotenv(PROJECT_ROOT / ".env")

from toolagent_prompt import TERMINAL_CHECKER_PROMPT, TERMINAL_SUMMARY_PROMPT  # noqa: E402

with open(PROJECT_ROOT / "config.yaml", "r", encoding="utf-8") as _f:
    _cfg = yaml.safe_load(_f)

SHELL_OUTPUT_MAX_LENGTH: int = int(_cfg.get("shell_output_max_length", 3000))
TERMINAL_SMALL_MAX_TOKENS: int = int(_cfg.get("terminal_small_max_tokens", 1024))

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


# Schemas =====================================================================


class CheckerOutput(BaseModel):
    allowed: bool = Field(description="Whether the command is allowed to execute")
    reason: str = Field(description="If not allowed, the reason why")


class TerminalSummary(BaseModel):
    errors: list[str] = Field(
        default_factory=list,
        description="Verbatim error / traceback blocks from the output",
    )
    highlights: list[str] = Field(
        default_factory=list,
        description="Verbatim load-bearing lines (paths, URLs, results) preserved as-is",
    )
    summary: str = Field(
        default="",
        description="Lossy summary of the remaining noisy output",
    )

    def render(self, original_len: int, limit: int) -> str:
        parts = [f"[output summarized: original {original_len} chars exceeded {limit} char limit]"]
        if self.errors:
            parts.append("== errors (verbatim) ==\n" + "\n---\n".join(self.errors))
        if self.highlights:
            parts.append("== highlights (verbatim) ==\n" + "\n".join(self.highlights))
        if self.summary:
            parts.append("== summary ==\n" + self.summary)
        return "\n\n".join(parts)


# LLM =========================================================================


_llm: ChatOpenAI | None = None

def _llm_singleton() -> ChatOpenAI:
    # 懒加载：避免 import 时构建 httpx client（SOCKS 代理无 socksio 会 ImportError）
    global _llm
    if _llm is None:
        _llm = ChatOpenAI(
            model=os.getenv("small_llm_model"),
            api_key=os.getenv("small_llm_key"),
            base_url=os.getenv("small_llm_base_url"),
            max_tokens=TERMINAL_SMALL_MAX_TOKENS,
            streaming=False,
        )
    return _llm


# Checker agent ===============================================================


class CheckerAgent:
    def _messages(self, command: str) -> list:
        return [SystemMessage(content=TERMINAL_CHECKER_PROMPT), HumanMessage(content=command)]

    def check(self, command: str) -> CheckerOutput:
        return _llm_singleton().with_structured_output(CheckerOutput).invoke(self._messages(command))

    async def acheck(self, command: str) -> CheckerOutput:
        return await _llm_singleton().with_structured_output(CheckerOutput).ainvoke(self._messages(command))


# Summarizer agent ============================================================


class SummarizerAgent:
    def __init__(self, max_length: int = SHELL_OUTPUT_MAX_LENGTH):
        self.max_length = max_length

    def _needs_summary(self, output: str) -> bool:
        return self.max_length > 0 and len(output) > self.max_length

    def _messages(self, command: str, output: str) -> list:
        clean = _ANSI_RE.sub("", output)
        if len(clean) > self.max_length * 2:
            keep = max(self.max_length, 1000)
            clean = f"{clean[:keep]}\n...[truncated for summarization]...\n{clean[-keep:]}"
        user = f"Command:\n{command}\n\nOutput ({len(clean)} chars):\n{clean}"
        return [SystemMessage(content=TERMINAL_SUMMARY_PROMPT), HumanMessage(content=user)]

    def _fallback(self, output: str, error: Exception) -> str:
        half = max(self.max_length // 2, 500)
        head, tail = output[:half], output[-half:]
        omitted = max(len(output) - len(head) - len(tail), 0)
        return (
            f"[output summary failed: {error!r}; head+tail truncation]\n"
            f"{head}\n...[{omitted} chars omitted]...\n{tail}"
        )

    def summarize(self, command: str, output: str) -> str:
        if not self._needs_summary(output):
            return output
        try:
            digest: TerminalSummary = (
                _llm_singleton().with_structured_output(TerminalSummary).invoke(self._messages(command, output))
            )
            return digest.render(len(output), self.max_length)
        except Exception as e:
            return self._fallback(output, e)

    async def asummarize(self, command: str, output: str) -> str:
        if not self._needs_summary(output):
            return output
        try:
            digest: TerminalSummary = await (
                _llm_singleton().with_structured_output(TerminalSummary).ainvoke(self._messages(command, output))
            )
            return digest.render(len(output), self.max_length)
        except Exception:
            return await asyncio.to_thread(self.summarize, command, output)


checker_agent = CheckerAgent()
summarizer_agent = SummarizerAgent()
