from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from langchain_core.messages import BaseMessage, get_buffer_string
from langchain_core.prompts import ChatPromptTemplate
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from toolagent_prompt import PROJECT_MEMORY_PROMPT  # noqa: E402

load_dotenv()
llm = ChatOpenAI(
    model=os.getenv("small_llm_model"),
    api_key=os.getenv("small_llm_key"),
    base_url=os.getenv("small_llm_base_url"),
)


NOTE_PATH = ROOT / "Memory" / "projectKnow.md"
_LOCK = asyncio.Lock()
_TAIL_LINES = 20  # 反馈给 LLM 用于判 new_task 与去重的最近行数


class ProjectKnowledgeBatch(BaseModel):
    new_task: bool = Field(
        default=False,
        description=(
            "True ONLY when the new notes belong to a clearly different "
            "top-level project than existing_notes. False on doubt."
        ),
    )
    notes: list[str] = Field(
        default_factory=list,
        description=(
            "Zero or more one-line Chinese notes about THIS project, each tagged: "
            "【流程】(ordered execution steps toward a goal) / 【坑】(pitfall+后果+规避) / "
            "【方法】(useful method) / 【知识】(project-specific fact). Capture BOTH the "
            "execution flow and the knowledge gained, anchoring each knowledge note to "
            "its step in the flow. No fluff."
        ),
    )


_chain = (
    ChatPromptTemplate.from_messages([
        ("system", PROJECT_MEMORY_PROMPT),
        ("human", "existing_notes:\n{existing}\n\ntranscript:\n{transcript}"),
    ])
    | llm.with_structured_output(ProjectKnowledgeBatch)
)


def read_notes() -> str:
    """供 manager 系统提示词拼接使用。"""
    if not NOTE_PATH.exists():
        return ""
    return NOTE_PATH.read_text(encoding="utf-8").strip()


def _read_tail() -> str:
    if not NOTE_PATH.exists():
        return ""
    lines = NOTE_PATH.read_text(encoding="utf-8").splitlines()
    return "\n".join(lines[-_TAIL_LINES:])


def _format(tid: str, notes: list[str]) -> str:
    ts = datetime.now().isoformat(timespec="seconds")
    out = []
    for n in notes:
        s = (n or "").strip().replace("\n", " ")
        if s:
            out.append(f"- [{ts}] [{tid}] {s}\n")
    return "".join(out)


async def _write(tid: str, notes: list[str], *, reset: bool) -> None:
    block = _format(tid, notes)
    if not block and not reset:
        return
    async with _LOCK:
        NOTE_PATH.parent.mkdir(parents=True, exist_ok=True)
        mode = "w" if reset else "a"
        await asyncio.to_thread(
            lambda: NOTE_PATH.open(mode, encoding="utf-8").write(block)
        )


async def route_project(tid: str, new: list[BaseMessage], *, offset: int = 0, k: int = 5) -> None:
    """从最近消息抽取项目记忆（执行流程 + 坑/方法/知识）；任务切换时清空旧记录后再写。"""
    if not new:
        return
    existing = _read_tail()
    batch: ProjectKnowledgeBatch = await _chain.ainvoke({
        "existing": existing or "(empty)",
        "transcript": get_buffer_string(new),
    })
    notes = list(getattr(batch, "notes", None) or [])
    reset = bool(getattr(batch, "new_task", False)) and bool(existing)
    if not notes and not reset:
        return
    await _write(tid, notes, reset=reset)
