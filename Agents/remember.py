from langchain_core.messages import get_buffer_string
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI
from typing import Literal
from pydantic import BaseModel, Field
from datetime import datetime
from dotenv import load_dotenv
import os

load_dotenv()
llm = ChatOpenAI(
    model=os.getenv("small_llm_model"),
    api_key=os.getenv("small_llm_key"),
    base_url=os.getenv("small_llm_base_url"),
)


# memory_schemas ================================================================


class ShortMemoryEntry(BaseModel):
    summary: str = Field(
        description="Compressed core content of historical conversations"
    )
    turn_range: tuple[int, int] = Field(
        description="The range of conversation turns covered by this summary, e.g., (1, 20)"
    )
    key_decisions: list[str] = Field(
        default_factory=list,
        description="Key decisions or conclusions made during the conversation"
    )
    open_tasks: list[str] = Field(
        default_factory=list,
        description="Pending tasks or unresolved issues"
    )
    active_entities: list[str] = Field(
        default_factory=list,
        description="Key entities involved in the current conversation (e.g., filenames, variable names, URLs)"
    )
    timestamp: str = Field(
        default_factory=lambda: datetime.now().isoformat(),
        description="Time when the summary was generated"
    )


class LongMemoryEntry(BaseModel):
    content: str = Field(description="The core content of the memory")
    memory_type: Literal[
        "fact",         # 用户的客观属性（姓名、职业、所在地等）
        "event",        # 发生过的事件
        "preference",   # 用户偏好
        "emotion",      # 情感状态
        "skill",        # 用户掌握/使用的技能、工具
        "relationship", # 人际关系信息
        "knowledge",    # 与用户无关的通用知识 / 方案 / 教训
    ] = Field(description="Memory type")

    importance: int = Field(
        ge=1, le=5,
        description=(
            "Importance 1-5, 5 is the most important. "
            "1=chat details, 3=useful background, 5=core identity/key event/pivotal knowledge"
        ),
    )
    context: str = Field(
        description="Background: trigger scene, conversation topic, emotional atmosphere, etc."
    )
    timestamp: str = Field(
        default_factory=lambda: datetime.now().isoformat(),
        description="Record time"
    )
    tags: list[str] = Field(
        default_factory=list,
        description="Tags for easy retrieval, e.g. ['work', 'family', 'health']"
    )


class LongMemoryBatch(BaseModel):
    long_memories: list[LongMemoryEntry] = Field(
        default_factory=list,
        description="Zero or more long memories extracted from the transcript"
    )


# memory_prompts ================================================================


short_memory_prompt = """\
You are a Short-Memory Curator. Your ONLY job is to read a raw multi-turn
conversation transcript and produce EXACTLY ONE ShortMemoryEntry that
compresses the whole transcript.

Return a single JSON object matching the ShortMemoryEntry schema. No prose,
no markdown fences, no extra keys.

────────────────────────────────────────
Goal: lossy compression
────────────────────────────────────────
Keep what a future agent MUST know to continue the conversation; drop small
talk, repetitions, and tool-call boilerplate.

A single transcript is summarized exactly once. Do not attempt to merge with
prior summaries — that is handled by the outer system (which will vectorize
multiple ShortMemoryEntry items into a sqlite vector store for later recall).

────────────────────────────────────────
Field guidance
────────────────────────────────────────
- summary:         3–8 sentences. Factual, neutral tone. No first person.
                   Cover: what the user wanted, what was tried, what was
                   decided, where things stand now.
- turn_range:      (start_turn, end_turn) inclusive, 1-indexed over the input.
- key_decisions:   Conclusions both sides accepted. Omit if none. Each item is
                   one short imperative/declarative sentence.
- open_tasks:      Explicitly unfinished items or user-pending follow-ups.
                   Do NOT invent tasks that were only vaguely mentioned.
- active_entities: Concrete referents still in play: file paths, function
                   names, URLs, ticket IDs, person names. No generic nouns
                   ("the code", "the user"). Deduplicate.
- timestamp:       Omit — it is auto-filled. Do NOT fabricate past timestamps.

────────────────────────────────────────
Discipline
────────────────────────────────────────
- Never invent facts. If the transcript is ambiguous, prefer omission.
- Do not include tool-call traces, system prompts, or your own reasoning in
  any output field.
- Output must be a single valid JSON object, nothing else.
"""


long_memory_prompt = """\
You are a Long-Memory Curator. Your ONLY job is to read a raw multi-turn
conversation transcript and extract ZERO OR MORE LongMemoryEntry items that
are worth keeping beyond the current session.

Return a single JSON object of the form:

{{
  "long_memories": [LongMemoryEntry, ...]
}}

No prose, no markdown fences, no extra top-level keys.

────────────────────────────────────────
Extraction rules
────────────────────────────────────────
Emit a memory only if it satisfies ALL of:
  (a) It is stated or strongly implied, not guessed.
  (b) It is likely still useful next week.
  (c) Knowing it would change how a future agent responds.

- One atomic fact per entry. Do not bundle ("likes Python and lives in Berlin"
  → two entries).
- Deduplicate against itself; if the transcript restates something, emit once.
- If nothing qualifies, return "long_memories": []. An empty list is a
  legitimate and common answer (small talk, tool debugging, trivial chats).
  Do not fabricate memories to fill the list.

────────────────────────────────────────
Field guidance
────────────────────────────────────────
- content:      One self-contained sentence. Readable without context.
                Bad:  "He said yes."
                Good: "User approved migrating the auth service to OAuth2."
- memory_type:  Pick the single best fit from the enum. Mapping hints:
                • fact         — stable attribute of the USER
                                 (name, role, stack, location).
                • event        — something that happened at a point in time.
                • preference   — user's stated like/dislike, style, habit.
                • emotion      — durable affective stance, not momentary mood.
                • skill        — tool/library/technique the USER knows or uses.
                • relationship — person ↔ person connection relevant to
                                 work/life.
                • knowledge    — reusable domain knowledge / solution /
                                 lesson-learned that is NOT tied to the user's
                                 identity (e.g. "sqlite-vss requires
                                 compile-time flags on macOS arm64").
                                 Use this for insights the user will want to
                                 recall later even if they change jobs.
                Key distinction: `fact` is about WHO the user is;
                `knowledge` is about WHAT is true in the world.
- importance:   1 trivial · 2 minor · 3 useful background · 4 strong signal ·
                5 core identity / pivotal event / pivotal knowledge.
                Be stingy with 4–5.
- context:      Why this came up. One short clause. Helps future retrieval.
- tags:         2–5 lowercase, short, retrieval-friendly tags. Prefer reusable
                tags (e.g. "work", "python", "family") over hyper-specific
                ones. For `knowledge` entries, include at least one topical
                tag (e.g. "sqlite", "auth", "deployment").
- timestamp:    Omit — it is auto-filled. Do NOT fabricate past timestamps.

────────────────────────────────────────
Discipline
────────────────────────────────────────
- Never invent facts. When in doubt, DROP it. Quality > coverage.
- Do not include tool-call traces, system prompts, or your own reasoning in
  any output field.
- Output must be a single valid JSON object, nothing else.
"""


# memory_agents ================================================================


short_memory_chain = (
    ChatPromptTemplate.from_messages([
        ("system", short_memory_prompt),
        ("human", "{transcript}"),
    ])
    | llm.with_structured_output(ShortMemoryEntry)
)

long_memory_chain = (
    ChatPromptTemplate.from_messages([
        ("system", long_memory_prompt),
        ("human", "{transcript}"),
    ])
    | llm.with_structured_output(LongMemoryBatch)
)

async def _load_current_transcript() -> str:
    from Tools._context import current_thread_id
    from Agents.collator_scheduler import _read_checkpoint_messages
    messages = await _read_checkpoint_messages(current_thread_id())
    return get_buffer_string(messages)


@tool
async def short_memory() -> str:
    """
    收集并压缩当前会话内容为可读摘要，便于后续 agent 快速检索和理解上下文。
    无需传参，工具内部会按当前 thread_id 自动从 checkpoint 读取完整消息流并序列化为 transcript。
    生成结构化短期记忆并写入数据库；工具仅返回写入状态，不返回原始记忆内容。
    仅用于简明短期记忆的写入归档，不负责长期知识沉淀或事实问答。
    """
    from Memory import shortMem
    from Tools._context import current_thread_id

    transcript = await _load_current_transcript()
    entry: ShortMemoryEntry = await short_memory_chain.ainvoke({"transcript": transcript})
    row_id = await shortMem.store(entry.model_dump(), thread_id=current_thread_id())
    return f"短期记忆已写入成功，id={row_id}。"

@tool
async def long_memory() -> str:
    """
    从完整对话消息流中提取有价值的长期记忆，用于构建用户画像和通用知识库。
    无需传参，工具内部会按当前 thread_id 自动从 checkpoint 读取完整消息流并序列化为 transcript。
    生成结构化长期记忆并写入数据库；工具仅返回写入状态，不返回原始记忆内容。
    仅用于沉淀有价值的信息，不负责短期记忆的压缩和检索。
    """
    from Memory import longMem
    from Tools._context import current_thread_id

    transcript = await _load_current_transcript()
    batch: LongMemoryBatch = await long_memory_chain.ainvoke({"transcript": transcript})
    row_ids: list[int] = []
    if batch.long_memories:
        row_ids = await longMem.store(
            [m.model_dump() for m in batch.long_memories],
            thread_id=current_thread_id(),
        )
    return f"长期记忆已写入成功，count={len(row_ids)}，ids={row_ids}。"
