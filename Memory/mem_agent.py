from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Literal, Optional

from dotenv import load_dotenv
from langchain_core.messages import BaseMessage, get_buffer_string
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from Memory import longMem, shortMem  # noqa: E402
from Tools.utils import current_thread_id  # noqa: E402
from toolagent_prompt import (  # noqa: E402
    LONG_CURATOR_PROMPT,
    LONG_MEMORY_PROMPT,
    SHORT_MEMORY_PROMPT,
)

load_dotenv()
llm = ChatOpenAI(
    model=os.getenv("agent_llm_model"),
    api_key=os.getenv("agent_llm_key"),
    base_url=os.getenv("agent_llm_base_url"),
)


# extraction schemas =========================================================


MemoryType = Literal[
    "fact", "event", "preference", "emotion",
    "skill", "relationship", "knowledge",
]


class ShortMemoryEntry(BaseModel):
    summary: str = Field(description="Compressed core content of historical conversations")
    turn_range: tuple[int, int] = Field(
        description="The range of conversation turns covered by this summary, e.g., (1, 20)"
    )
    key_issues: list[str] = Field(
        default_factory=list,
        description=(
            "Key problems / questions / blockers encountered during the conversation. "
            "One short sentence per item, factual, no speculation."
        ),
    )
    key_decisions: list[str] = Field(
        default_factory=list,
        description="Key decisions or conclusions made during the conversation",
    )
    key_errors: list[str] = Field(
        default_factory=list,
        description=(
            "Concrete errors / failures observed (exception messages, failed tool calls, "
            "wrong outputs, etc.). Include identifying details when available."
        ),
    )
    resolutions: list[str] = Field(
        default_factory=list,
        description=(
            "How the issues / errors above were resolved or worked around. "
            "Each item should be self-contained and reference the matching issue/error when useful."
        ),
    )
    open_tasks: list[str] = Field(default_factory=list, description="Pending tasks or unresolved issues")
    active_entities: list[str] = Field(
        default_factory=list,
        description="Key entities involved in the current conversation (e.g., filenames, variable names, URLs)",
    )
    timestamp: str = Field(
        default_factory=lambda: datetime.now().isoformat(),
        description="Time when the summary was generated",
    )


class LongMemoryEntry(BaseModel):
    content: str = Field(description="The core content of the memory")
    memory_type: MemoryType = Field(description="Memory type")
    importance: int = Field(
        ge=1, le=5,
        description=(
            "Importance 1-5, 5 is the most important. "
            "1=chat details, 3=useful background, 5=core identity/key event/pivotal knowledge"
        ),
    )
    context: str = Field(description="Background: trigger scene, conversation topic, emotional atmosphere, etc.")
    timestamp: str = Field(
        default_factory=lambda: datetime.now().isoformat(),
        description="Record time",
    )
    tags: list[str] = Field(
        default_factory=list,
        description="Tags for easy retrieval, e.g. ['work', 'family', 'health']",
    )


class LongMemoryBatch(BaseModel):
    long_memories: list[LongMemoryEntry] = Field(
        default_factory=list,
        description="Zero or more long memories extracted from the transcript",
    )


# curation schemas ===========================================================


class LongMemoryDecision(BaseModel):
    candidate_index: int = Field(
        ge=0,
        description="0-based index of the candidate in the input candidates list this decision refers to",
    )
    action: Literal["insert", "update", "skip", "delete"] = Field(
        description=(
            "insert = write the candidate as a new row; "
            "update = replace fields of an existing row (target_id required); "
            "skip   = drop the candidate, keep DB unchanged; "
            "delete = remove an existing row that is now wrong/obsolete (target_id required)."
        )
    )
    target_id: Optional[int] = Field(
        default=None,
        description="Existing row id to act on. Required for update/delete, must be None for insert/skip.",
    )
    content: Optional[str] = Field(
        default=None,
        description="New content. Required for insert/update; ignored otherwise. May be a merged rewrite of candidate + existing row.",
    )
    memory_type: Optional[MemoryType] = Field(
        default=None,
        description="New memory_type. Required for insert/update; ignored otherwise.",
    )
    importance: Optional[int] = Field(
        default=None, ge=1, le=5,
        description="New importance 1-5. Required for insert/update; ignored otherwise.",
    )
    context: Optional[str] = Field(
        default=None,
        description="New context. Optional for insert/update; ignored otherwise.",
    )
    tags: Optional[list[str]] = Field(
        default=None,
        description="New tags. Optional for insert/update; ignored otherwise.",
    )
    reason: str = Field(description="Short justification grounded in importance/timestamp/type/similarity.")


class LongMemoryCurationBatch(BaseModel):
    decisions: list[LongMemoryDecision] = Field(
        default_factory=list,
        description="Exactly one decision per input candidate, in any order.",
    )


# chains =====================================================================


_short_chain = (
    ChatPromptTemplate.from_messages([
        ("system", SHORT_MEMORY_PROMPT),
        ("human", "{transcript}"),
    ])
    | llm.with_structured_output(ShortMemoryEntry)
)

_long_extract_chain = (
    ChatPromptTemplate.from_messages([
        ("system", LONG_MEMORY_PROMPT),
        ("human", "{transcript}"),
    ])
    | llm.with_structured_output(LongMemoryBatch)
)

_long_chain = (
    ChatPromptTemplate.from_messages([
        ("system", LONG_CURATOR_PROMPT),
        ("human", "candidates:\n{candidates}\n\nexisting:\n{existing}"),
    ])
    | llm.with_structured_output(LongMemoryCurationBatch)
)


# tool =======================================================================


@tool
async def collate_long_memory(candidates: list[dict], k: int = 5) -> dict:
    """
    长期记忆冲突整理 + 直接落库一体化（仅作用于当前 thread_id 的记忆）。
    流程: 1) 按候选向量召回当前 thread_id 的 top-k 邻居；
         2) 喂决策 chain；3) 按 decisions 调用 longMem.store/update/delete。
    """
    if not candidates:
        return {"decisions": [], "results": []}

    tid = current_thread_id()
    contents = [c["content"] for c in candidates]
    existing = await longMem.search_neighbors(contents, k=k, thread_id=tid)

    batch: LongMemoryCurationBatch = await _long_chain.ainvoke({
        "candidates": json.dumps(candidates, ensure_ascii=False),
        "existing": json.dumps(existing, ensure_ascii=False, default=str),
    })

    valid_ids = {r["id"] for r in existing}
    results: list[dict] = []
    for d in batch.decisions:
        out: dict = {"candidate_index": d.candidate_index, "action": d.action,
                     "target_id": d.target_id, "ok": False}
        try:
            if d.action == "skip":
                out["ok"] = True
            elif d.action == "insert":
                if d.content is None or d.memory_type is None or d.importance is None:
                    raise ValueError("insert requires content/memory_type/importance")
                cand = candidates[d.candidate_index] if 0 <= d.candidate_index < len(candidates) else {}
                row = {
                    "content": d.content,
                    "memory_type": d.memory_type,
                    "importance": d.importance,
                    "context": d.context if d.context is not None else cand.get("context", ""),
                    "tags": d.tags if d.tags is not None else cand.get("tags", []),
                    "timestamp": cand.get("timestamp") or datetime.now().isoformat(),
                }
                out["db_id"] = await longMem.store(row, thread_id=tid)
                out["ok"] = True
            else:  # update / delete
                if d.target_id is None or d.target_id not in valid_ids:
                    raise ValueError(f"{d.action} requires target_id present in existing")
                if d.action == "update":
                    affected = await longMem.update(d.target_id, {
                        "content": d.content, "memory_type": d.memory_type,
                        "importance": d.importance, "context": d.context, "tags": d.tags,
                    })
                else:
                    affected = await longMem.delete(d.target_id)
                out["affected"] = affected
                out["ok"] = affected > 0
        except Exception as ex:
            out["error"] = repr(ex)
        results.append(out)

    return {"decisions": [d.model_dump() for d in batch.decisions], "results": results}


# routes =====================================================================


async def route_short(tid: str, *_args, **_kwargs) -> None:
    from Tools.utils import compress_ckpt_messages, is_summary_message, read_ckpt_msgs
    msgs = await read_ckpt_msgs(tid)
    fresh = [m for m in msgs if not is_summary_message(m)]
    half = len(fresh) // 2
    if half <= 0:
        return
    target = fresh[:half]

    entry = await _short_chain.ainvoke({"transcript": get_buffer_string(target)})
    payload = entry.model_dump()
    tr = payload.get("turn_range") or [1, len(target)]
    try:
        ls, le = int(tr[0]), int(tr[1])
    except (TypeError, ValueError, IndexError):
        ls, le = 1, len(target)
    payload["turn_range"] = (ls, le)
    payload["timestamp"] = datetime.now().isoformat()
    await shortMem.store(payload, thread_id=tid)
    try:
        ids = [getattr(m, "id", None) for m in target]
        await compress_ckpt_messages(tid, ids, payload.get("summary") or "")
    except Exception:
        pass


async def route_long(tid: str, new: list[BaseMessage], *, offset: int = 0, k: int = 5) -> None:
    """从新增片段抽取长期记忆候选并交给 collate_long_memory 整理入库。"""
    batch = await _long_extract_chain.ainvoke({"transcript": get_buffer_string(new)})
    entries = getattr(batch, "long_memories", None) or []
    if not entries:
        return
    candidates: list[dict] = []
    for e in entries:
        d = e.model_dump() if hasattr(e, "model_dump") else dict(e)
        d["timestamp"] = datetime.now().isoformat()
        candidates.append(d)
    await collate_long_memory.ainvoke(
        {"candidates": candidates, "k": k},
        config={"configurable": {"thread_id": tid}},
    )
