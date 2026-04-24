from typing import Literal, Optional
from pathlib import Path
from pydantic import BaseModel, Field
from langchain_core.tools import tool
from langchain_core.messages import HumanMessage
from langchain_openai import ChatOpenAI
from langchain.agents import create_agent
from dotenv import load_dotenv
import os
import sys

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
from Memory.longMem import search_long_memory  # noqa: E402
from Memory.shortMem import search_short_memory  # noqa: E402
from Tools.tavily import TavilySearch  # noqa: E402
from Tools.browser import Browser  # noqa: E402
from Knowledge.retriever import KnowledgeSearch  # noqa: E402
from agents_prompt import retriever_prompt  # noqa: E402

load_dotenv()

llm = ChatOpenAI(
    model=os.getenv("agent_llm_model"),
    api_key=os.getenv("agent_llm_key"),
    base_url=os.getenv("agent_llm_base_url"),
)


# schemas =====================================================================


Source = Literal["long_memory", "short_memory", "web", "knowledge"]


class RetrievedItem(BaseModel):
    source: Source = Field(description="命中来源")
    content: str = Field(description="原始或浓缩后的命中内容")
    relevance: str = Field(description="与 query 的相关点，一句话")
    item_id: Optional[int] = Field(default=None, description="memory/knowledge 的行 ID；web 命中留空")
    similarity: Optional[float] = Field(
        default=None, description="memory 的余弦相似度 或 knowledge 的 rerank score"
    )
    memory_type: Optional[str] = Field(
        default=None,
        description="长记忆类型 (fact/event/preference/emotion/skill/relationship/knowledge)",
    )
    importance: Optional[int] = Field(default=None, description="长记忆 importance 1-5")
    turn_start: Optional[int] = Field(default=None, description="短记忆起始轮次")
    turn_end: Optional[int] = Field(default=None, description="短记忆结束轮次")
    url: Optional[str] = Field(default=None, description="web 命中 URL")
    title: Optional[str] = Field(default=None, description="web 命中标题")
    timestamp: Optional[str] = Field(default=None, description="ISO 时间戳")


class RetrievalReport(BaseModel):
    query: str = Field(description="原始 query，原样回填")
    summary: str = Field(description="跨源综合答案；没信息则空串")
    key_points: list[str] = Field(default_factory=list, description="结论要点 bullet")
    sources_used: list[Source] = Field(
        default_factory=list, description="本次实际调用过的检索源"
    )
    items: list[RetrievedItem] = Field(
        default_factory=list, description="各源的相关命中，保留溯源"
    )
    confidence: Literal["high", "medium", "low"] = Field(description="综合置信度")
    gaps: list[str] = Field(
        default_factory=list, description="未检到 / 信息不足 / 源间冲突"
    )


# retriever agent =============================================================


retriever_agent = create_agent(
    model=llm,
    tools=[
        search_long_memory,
        search_short_memory,
        KnowledgeSearch(),
        TavilySearch(),
        Browser(),
    ],
    system_prompt=retriever_prompt,
    response_format=RetrievalReport,
)


@tool
async def retrieve(query: str) -> dict:
    """
    跨源检索并合成结构化 JSON 报告。
    内部 agent 自行决定调用以下 5 个源的哪些：长期记忆 / 短期记忆 / 项目知识库 /
    互联网搜索 / 浏览器（仅在 tavily 不够时）。
    输入: query (自然语言问题)
    输出: RetrievalReport 的 dict —— query / summary / key_points / sources_used /
          items[{source, content, relevance, item_id, similarity, ...}] /
          confidence / gaps。
    """
    state = await retriever_agent.ainvoke({"messages": [HumanMessage(content=query)]})
    report = state.get("structured_response")
    if not isinstance(report, RetrievalReport):
        return {
            "query": query,
            "summary": "",
            "key_points": [],
            "sources_used": [],
            "items": [],
            "confidence": "low",
            "gaps": ["retriever agent did not return a structured response"],
        }
    return report.model_dump()
