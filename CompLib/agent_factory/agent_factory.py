from __future__ import annotations

from typing import Callable, Optional, Sequence

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import SystemMessage


class ContextInjectMiddleware(AgentMiddleware):
    """把一组 ``provider()`` 文本块注入到 messages 末尾的 SystemMessage（空串跳过）。

    解耦：providers 是无参可调用，各返回一段可变上下文（如 workspace/plan/todo 文本）；静态 system_prompt 不动，保缓存。
    """

    def __init__(self, providers: Sequence[Callable[[], str]]) -> None:
        super().__init__()
        self.providers = list(providers)

    def _parts(self) -> list[str]:
        out: list[str] = []
        for p in self.providers:
            try:
                t = p()
            except Exception:
                t = ""
            if t and t.strip():
                out.append(t.strip())
        return out

    def wrap_model_call(self, request, handler):  # type: ignore[override]
        return handler(self._inject(request))

    async def awrap_model_call(self, request, handler):  # type: ignore[override]
        return await handler(self._inject(request))

    def _inject(self, request):
        parts = self._parts()
        if not parts:
            return request
        return request.override(
            messages=list(request.messages) + [SystemMessage(content="\n\n---\n\n".join(parts))]
        )


class BudgetReminderMiddleware(AgentMiddleware):
    """在本轮**最后一次** LLM 调用前注入一条预算提醒；非最后一轮原样放行（不破坏缓存）。"""

    def __init__(self, run_limit: int, message: Optional[str] = None) -> None:
        super().__init__()
        self.run_limit = int(run_limit)
        self.message = message or (
            "[调用预算] 本轮最后一次 LLM 调用：别再调工具，立刻输出最终状态报告（做了什么 + 关键证据 + 还差什么）。"
        )

    def wrap_model_call(self, request, handler):  # type: ignore[override]
        return handler(self._with(request))

    async def awrap_model_call(self, request, handler):  # type: ignore[override]
        return await handler(self._with(request))

    def _with(self, request):
        if self.run_limit <= 0 or int(request.state.get("run_model_call_count", 0)) + 1 < self.run_limit:
            return request
        return request.override(messages=list(request.messages) + [SystemMessage(content=self.message)])


class AgentBuilder:
    """组装一个 langchain agent：model + tools + system_prompt + middleware + checkpointer。

    只把零件交给 ``create_agent``，无业务逻辑；中间件与工具由外部注入。
    """

    def __init__(
        self,
        model,
        tools: Sequence,
        system_prompt: str,
        *,
        middleware: Optional[Sequence] = None,
        checkpointer=None,
        response_format=None,
    ) -> None:
        self.model = model
        self.tools = list(tools)
        self.system_prompt = system_prompt
        self.middleware = list(middleware) if middleware else None
        self.checkpointer = checkpointer
        self.response_format = response_format

    def build(self):
        from langchain.agents import create_agent

        kwargs = dict(model=self.model, tools=self.tools, system_prompt=self.system_prompt)
        if self.middleware is not None:
            kwargs["middleware"] = self.middleware
        if self.checkpointer is not None:
            kwargs["checkpointer"] = self.checkpointer
        if self.response_format is not None:
            kwargs["response_format"] = self.response_format
        return create_agent(**kwargs)
