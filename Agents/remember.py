from langchain_core.messages import get_buffer_string
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI
from typing import Literal
from pydantic import BaseModel, Field
from datetime import datetime
from dotenv import load_dotenv
import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
from toolagent_prompt import SHORT_MEMORY_PROMPT, LONG_MEMORY_PROMPT  # noqa: E402

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


# memory_agents ================================================================


short_memory_prompt = SHORT_MEMORY_PROMPT
long_memory_prompt = LONG_MEMORY_PROMPT

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
    from Tools.utils import current_thread_id
    from Agents.collator import read_ckpt_msgs
    messages = await read_ckpt_msgs(current_thread_id())
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
    from Tools.utils import current_thread_id

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
    from Tools.utils import current_thread_id

    transcript = await _load_current_transcript()
    batch: LongMemoryBatch = await long_memory_chain.ainvoke({"transcript": transcript})
    row_ids: list[int] = []
    if batch.long_memories:
        row_ids = await longMem.store(
            [m.model_dump() for m in batch.long_memories],
            thread_id=current_thread_id(),
        )
    return f"长期记忆已写入成功，count={len(row_ids)}，ids={row_ids}。"
