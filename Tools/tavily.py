import os
import sys
from typing import Type, List
from pathlib import Path
import yaml
from pydantic import BaseModel, Field, PrivateAttr
from langchain_core.tools import BaseTool
from langchain_community.utilities.tavily_search import TavilySearchAPIWrapper
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
load_dotenv(PROJECT_ROOT / '.env')

from Tools._context import bump_budget, current_thread_id  # noqa: E402

with open(PROJECT_ROOT / 'config.yaml', 'r', encoding='utf-8') as file:
    config = yaml.safe_load(file)

tavily_count_limit: int = config.get('tavily_count_limit', 10)
tavily_max_results: int = config.get('tavily_max_results', 5)
tavily_search_depth: str = config.get('tavily_search_depth', 'advanced')
tavily_include_answer: bool = config.get('tavily_include_answer', True)
tavily_include_domains: List[str] = config.get('tavily_include_domains', [])
tavily_exclude_domains: List[str] = config.get('tavily_exclude_domains', [])


class TavilyInput(BaseModel):
    query: str = Field(description="The natural-language search query to look up on the web.")


def _format_results(payload: dict) -> str:
    parts: list[str] = []
    if answer := payload.get("answer"):
        parts.append(f"Answer: {answer}")
    results = payload.get("results", []) or []
    if not results and not parts:
        return "No results."
    for i, item in enumerate(results, 1):
        title = item.get("title", "")
        url = item.get("url", "")
        content = (item.get("content") or "").strip()
        if len(content) > 800:
            content = content[:800] + "...[truncated]"
        parts.append(f"[{i}] {title}\n{url}\n{content}")
    return "\n\n".join(parts)


class TavilySearch(BaseTool):
    name: str = "tavily_search"
    description: str = (
        "Web search via Tavily. Use to look up current docs, APIs, libraries, error "
        "messages, or any external knowledge needed before coding. Input is a natural "
        "language query."
    )
    args_schema: Type[BaseModel] = TavilyInput
    max_tool_calls: int = Field(default=tavily_count_limit, description="Maximum number of allowed tool calls per thread")
    _call_counts: dict[str, int] = PrivateAttr(default_factory=dict)
    _wrapper: TavilySearchAPIWrapper = PrivateAttr(default=None)

    def _get_wrapper(self) -> TavilySearchAPIWrapper:
        if self._wrapper is None:
            api_key = os.getenv("TAVILY_API_KEY")
            if not api_key:
                raise RuntimeError("TAVILY_API_KEY is not set")
            self._wrapper = TavilySearchAPIWrapper(tavily_api_key=api_key)
        return self._wrapper

    def reset(self):
        self._call_counts.clear()

    def _budget_response(self, tid: str) -> str:
        return (
            f"Tool call limit reached ({self.max_tool_calls}) for thread {tid}. "
            "Stop using tavily_search and respond directly."
        )

    def _run(self, query: str) -> str:
        tid = current_thread_id()
        ok, n, rem = bump_budget(self._call_counts, tid, self.max_tool_calls)
        if not ok:
            return self._budget_response(tid)
        try:
            payload = self._get_wrapper().raw_results(
                query=query,
                max_results=tavily_max_results,
                search_depth=tavily_search_depth,
                include_domains=tavily_include_domains,
                exclude_domains=tavily_exclude_domains,
                include_answer=tavily_include_answer,
            )
            result = _format_results(payload)
        except Exception as e:
            result = f"Tavily search failed: {e!r}"
        return f"{result}\n\n[Tool call {n}/{self.max_tool_calls}, remaining: {rem}]"

    async def _arun(self, query: str) -> str:
        tid = current_thread_id()
        ok, n, rem = bump_budget(self._call_counts, tid, self.max_tool_calls)
        if not ok:
            return self._budget_response(tid)
        try:
            payload = await self._get_wrapper().raw_results_async(
                query=query,
                max_results=tavily_max_results,
                search_depth=tavily_search_depth,
                include_domains=tavily_include_domains,
                exclude_domains=tavily_exclude_domains,
                include_answer=tavily_include_answer,
            )
            result = _format_results(payload)
        except Exception as e:
            result = f"Tavily search failed: {e!r}"
        return f"{result}\n\n[Tool call {n}/{self.max_tool_calls}, remaining: {rem}]"
