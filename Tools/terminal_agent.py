from __future__ import annotations

import os
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

from toolagent_prompt import TERMINAL_CHECKER_PROMPT  # noqa: E402

with open(PROJECT_ROOT / "config.yaml", "r", encoding="utf-8") as _f:
    _cfg = yaml.safe_load(_f)

TERMINAL_SMALL_MAX_TOKENS: int = int(_cfg.get("terminal_small_max_tokens", 1024))


# Schemas =====================================================================


class CheckerOutput(BaseModel):
    allowed: bool = Field(description="Whether the command is allowed to execute")
    reason: str = Field(description="If not allowed, the reason why")


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


checker_agent = CheckerAgent()
