from typing import Any, Literal, Type
from langchain_openai import ChatOpenAI
from langchain.agents import create_agent
from langchain.agents.middleware import ModelCallLimitMiddleware
from langchain_core.messages import HumanMessage
from langchain_core.tools import BaseTool, StructuredTool
from pathlib import Path
from pydantic import BaseModel, Field, PrivateAttr
from dotenv import load_dotenv
import asyncio
import json
import os
import sys
import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from Agents.coder import (  # noqa: E402
    CoderModule,
    CoderReport,
    FileChange,
    OnEvent,
    UsageExample,
    ainvoke_with_lint_gate,
    astream_collect_final_state,
    build_coder_agent,
    coder_subtask_run_limit,
    invoke_with_lint_gate,
    on_event_var,
)
from Tools.utils import bump_budget, current_thread_id, workspace_info  # noqa: E402
from Tools.skills import SkillLibrary  # noqa: E402
from Tools.todo import Todo  # noqa: E402
from agents_prompt import tasker_coder_prompt  # noqa: E402

load_dotenv(PROJECT_ROOT / ".env")

with open(PROJECT_ROOT / "config.yaml", "r", encoding="utf-8") as f:
    _config = yaml.safe_load(f) or {}

tasker_run_call_limit: int = _config.get("tasker_run_call_limit", 30)
tasker_thread_call_limit: int = _config.get("tasker_thread_call_limit", 100)
tasker_exit_behavior: str = _config.get("tasker_exit_behavior", "end")
tasker_max_tokens: int = int(_config.get("tasker_max_tokens", 4096))
dispatch_count_limit: int = _config.get("dispatch_count_limit", 10)

    
# TaskerReport Schema =============================================================


class TaskerSubtaskSummary(BaseModel):
    task_name: str = Field(description="与调度时使用的 task_name 对齐。")
    status: Literal["DONE", "DONE_WITH_CONCERNS", "NEEDS_CONTEXT", "BLOCKED"] = Field(
        description="子代理回报的状态，原样摘录。"
    )
    summary: str = Field(description="子代理 summary 字段的一句话浓缩。")
    key_modules: list[str] = Field(
        default_factory=list,
        description="本子任务负责的主要模块路径（从 CoderReport.modules 中抽选）。",
    )
    verification: str = Field(
        default="",
        description=(
            "子代理 verification 字段的摘要或原文（含命令与关键结果）。"
            "若子代理没跑真实验证，必须原样反映，不要美化。"
        ),
    )


class TaskerReport(BaseModel):
    overall_status: Literal["全部完成", "部分完成", "需用户介入"] = Field(
        description=(
            "整体状态，三选一。只有**所有子任务都 DONE 且 verification 证据可信**时"
            "才能填 '全部完成'；有子任务 BLOCKED / DONE_WITH_CONCERNS 或验证证据"
            "空缺时，至少降级为 '部分完成' 或 '需用户介入'。"
        )
    )
    project_overview: str = Field(
        description="整个项目 / feature 的一段总述：做了什么、为什么、谁会用它。"
    )
    architecture: str = Field(
        default="",
        description=(
            "架构说明：组件分工、数据流、关键边界。如果只是一个简单的函数级改动，"
            "可以填空字符串。"
        ),
    )
    main_modules: list[CoderModule] = Field(
        default_factory=list,
        description=(
            "全项目视角下的主要模块清单——把各子任务 CoderReport.modules 合并去重，"
            "只保留对读者有意义的条目。"
        ),
    )
    usage: str = Field(
        default="",
        description="项目级用法：入口 / 启动命令 / 前置依赖 / 配置项。",
    )
    usage_examples: list[UsageExample] = Field(
        default_factory=list,
        description="项目级的使用示例（0 到 N 个）。",
    )
    subtasks: list[TaskerSubtaskSummary] = Field(
        default_factory=list,
        description="每个被派发过的子任务一行摘要（状态 + 简述 + 主要模块 + 验证）。",
    )
    file_changes: list[FileChange] = Field(
        default_factory=list,
        description="全部子任务的文件变更合集（按路径去重）。",
    )
    key_decisions: list[str] = Field(
        default_factory=list,
        description="Tasker 层面的关键拆分 / 调度取舍（不是子任务内部细节）。",
    )
    user_needs_attention: list[str] = Field(
        default_factory=list,
        description=(
            "汇总需要用户介入的事项：子代理标的 open_issues、BLOCKED 原因、"
            "DONE_WITH_CONCERNS 疑虑等。若此列表非空，overall_status 不得为"
            "'全部完成'。"
        ),
    )


# DispatchCoder Tool ==================================================================


def _coder_report_to_json(report: Any) -> str:
    if report is None:
        return ""
    if isinstance(report, CoderReport):
        payload = report.model_dump()
    else:
        payload = {"raw": str(report)}
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _format_task_specific_prompt(task_name: str, task_prompt: str, context: str) -> str:
    task_name = task_name.strip() or "(未命名子任务)"
    blocks = [f"## 本次子任务：{task_name}"]
    context = context.strip()
    if context:
        blocks.append("### 上下文\n" + context)
    blocks.append("### 任务要求\n" + task_prompt.strip())
    tid = current_thread_id()
    blocks.append(workspace_info(tid))
    return "\n\n".join(blocks)


class DispatchCoderInput(BaseModel):
    task_name: str = Field(
        description="子任务简短名字（用于日志与最终汇总表格），避免空格以外的特殊字符。"
    )
    task_prompt: str = Field(
        description=(
            "**任务特定 prompt**——子代理除了通用编码规范之外只能看到这一段。"
            "必须自包含：目标 / 精确文件路径 / 具体需求（能给代码就给代码） / "
            "验证命令 / 边界约束。禁止出现 TBD、'类似前面那个任务'、未定义的引用。"
        )
    )
    context: str = Field(
        default="",
        description=(
            "周边场景：整体目标、上游子任务的关键产出（接口签名 / 文件路径 / 新常量）、"
            "不得违反的边界。可选但强烈建议填。"
        ),
    )
    step_index: int = Field(
        description=(
            "对应 workingTodo.md 中的 step 1-based 索引——本次 dispatch_coder 派发的"
            "就是该 step 描述的那条任务。子代理返回 status=DONE 时，框架会**自动**调"
            " todo.mark_done(step_index)，无需你再手勾；非 DONE 状态不会勾，"
            "由你按需补派或重派同一 step_index。"
            "强制 1:1：每条 step 必须且只能对应一次 dispatch_coder 调用；并行派发时"
            "也要给出各自正确的 step_index。"
        ),
    )


def _format_child_initial(task_name: str) -> str:
    return (
        f"上级调度器已把子任务 **{task_name}** 的完整规格同步到你的 system prompt。"
        "请按其中的需求、验证命令、边界约束动手执行；完成后用 CoderReport 结构化 "
        "schema 输出 JSON 介绍（主要模块、用法、验证证据都要填）。"
        "提交后系统会自动跑强制 lint 关卡；不过会把错误塞回来让你重改。"
    )


def _format_exception_report(task_name: str, exc: Exception) -> str:
    return json.dumps(
        {
            "status": "BLOCKED",
            "task_name": task_name,
            "summary": f"dispatch_coder 调用异常：{exc!r}",
            "modules": [],
            "usage": "",
            "usage_examples": [],
            "file_changes": [],
            "verification": "",
            "key_decisions": [],
            "open_issues": [f"exception: {exc!r}"],
        },
        ensure_ascii=False,
        indent=2,
    )


class DispatchCoder(BaseTool):
    name: str = "dispatch_coder"
    description: str = (
        "派发一个独立的编码子任务给一个全新的 coder 子代理。子代理是隔离的："
        "它只能看到通用的 coder_prompt 编码规范，以及你在 task_prompt 里写的任务特定要求。"
        "每次调用都会启动一个干净的子代理（工具预算独立，上下文隔离），所以可以放心多派；"
        "但每个 task_prompt 必须自包含——子代理看不到其他子任务的 prompt 或整体计划。"
        "**与 workingTodo 强绑定**：每次调用必须传 step_index（1-based），与 write_steps "
        "时落盘的 step 顺序对齐；子代理 status=DONE 时框架会自动 mark_done(step_index)，"
        "你不必再手勾；非 DONE 不会自动勾，你按需补派 / 重派同一 step_index。"
        "返回值是子代理产出的 CoderReport JSON（字段见下：status / task_name / summary / "
        "modules / usage / usage_examples / file_changes / verification / key_decisions / "
        "open_issues），末尾会附一行 [auto-mark] 提示勾选结果。请解析这段 JSON 再决定下一步。"
    )
    args_schema: Type[BaseModel] = DispatchCoderInput
    max_tool_calls: int = Field(default=dispatch_count_limit)
    _call_counts: dict[str, int] = PrivateAttr(default_factory=dict)
    _todo: Todo = PrivateAttr(default_factory=Todo)

    def reset(self) -> None:
        self._call_counts.clear()

    def _auto_mark_done(self, report_json: str, step_index: int) -> str:
        try:
            status = (json.loads(report_json).get("status") or "").upper()
        except Exception:
            status = ""
        if status != "DONE":
            return f"[auto-mark] 跳过 step {step_index}（status={status or 'UNKNOWN'}）。"
        return f"[auto-mark] {self._todo._run('mark_done', step_index=step_index)}"

    def _dispatch(self, task_name: str, task_prompt: str, context: str) -> str:
        task_block = _format_task_specific_prompt(task_name, task_prompt, context)
        child_agent = build_coder_agent(
            task_specific_prompt=task_block, run_limit=coder_subtask_run_limit,
        )
        report = invoke_with_lint_gate(child_agent, _format_child_initial(task_name))
        return _coder_report_to_json(report)

    async def _adispatch(self, task_name: str, task_prompt: str, context: str) -> str:
        task_block = _format_task_specific_prompt(task_name, task_prompt, context)
        child_agent = await asyncio.to_thread(
            build_coder_agent,
            task_specific_prompt=task_block,
            run_limit=coder_subtask_run_limit,
        )
        on_event = on_event_var.get()
        report = await ainvoke_with_lint_gate(
            child_agent, _format_child_initial(task_name), on_event=on_event,
        )
        return _coder_report_to_json(report)

    def _format_response(self, task_name: str, report_json: str, n: int, rem: int, mark_msg: str = "") -> str:
        return (
            f"===== 子代理 CoderReport（子任务：{task_name}）=====\n"
            f"{report_json}\n\n"
            f"[Tool call {n}/{self.max_tool_calls}, remaining: {rem}]\n{mark_msg}"
        )

    def _budget_exceeded(self, tid: str) -> str:
        return (
            f"Tool call limit reached ({self.max_tool_calls}) for thread {tid}. "
            "dispatch_coder 预算已耗尽；请基于现有子任务结果直接产出最终 TaskerReport。"
        )

    def _run(self, task_name: str, task_prompt: str, step_index: int, context: str = "") -> str:
        tid = current_thread_id()
        ok, n, rem = bump_budget(self._call_counts, tid, self.max_tool_calls)
        if not ok:
            return self._budget_exceeded(tid)
        try:
            report_json = self._dispatch(task_name, task_prompt, context)
        except Exception as e:
            report_json = _format_exception_report(task_name, e)
        mark_msg = self._auto_mark_done(report_json, step_index)
        return self._format_response(task_name, report_json, n, rem, mark_msg)

    async def _arun(self, task_name: str, task_prompt: str, step_index: int, context: str = "") -> str:
        tid = current_thread_id()
        ok, n, rem = bump_budget(self._call_counts, tid, self.max_tool_calls)
        if not ok:
            return self._budget_exceeded(tid)
        try:
            report_json = await self._adispatch(task_name, task_prompt, context)
        except Exception as e:
            report_json = _format_exception_report(task_name, e)
        mark_msg = self._auto_mark_done(report_json, step_index)
        return self._format_response(task_name, report_json, n, rem, mark_msg)


# Tasker Coder Agent ==================================================================


llm = ChatOpenAI(
    model=os.getenv("agent_llm_model"),
    api_key=os.getenv("agent_llm_key"),
    base_url=os.getenv("agent_llm_base_url"),
    max_tokens=tasker_max_tokens,
)

_dispatch_coder_tool = DispatchCoder()

tasker_coder_agent = create_agent(
    model=llm,
    tools=[SkillLibrary(), _dispatch_coder_tool, Todo()],
    system_prompt=tasker_coder_prompt,
    response_format=TaskerReport,
    middleware=[
        ModelCallLimitMiddleware(
            run_limit=tasker_run_call_limit,
            thread_limit=tasker_thread_call_limit,
            exit_behavior=tasker_exit_behavior,
        ),
    ],
)


_DISPATCH_TASKER_DESC = (
    "派发一个综合编码任务给 tasker_coder 子代理。需求与禁止的点需要描述十分详细，"
    "包含任务背景、需求、想要什么效果等。"
    "tasker_coder 会把自然语言任务拆成若干独立子任务、派发给隔离的 coder 子代理执行，"
    "最终汇总为 TaskerReport JSON 返回（overall_status / project_overview / architecture / "
    "main_modules / usage / usage_examples / subtasks / file_changes / key_decisions / "
    "user_needs_attention）。适合跨多个模块 / 文件协同的中大型编码工作；单文件小改动"
    "直接调 coder 即可，无需经过此工具。task_prompt 必须自包含：目标、约束、验证标准"
    "都写清楚，子代理层看不到外部上下文。"
)


def _finalize_tasker_report(report: Any) -> str:
    if isinstance(report, TaskerReport):
        return report.model_dump_json(indent=2)
    fallback = TaskerReport(
        overall_status="需用户介入",
        project_overview="tasker_coder 未产出结构化 TaskerReport。",
        architecture="",
        main_modules=[],
        usage="",
        usage_examples=[],
        subtasks=[],
        file_changes=[],
        key_decisions=[],
        user_needs_attention=[
            "tasker_coder 未返回结构化 TaskerReport：可能是 system prompt 过长被截断、"
            "工具预算耗尽，或模型未走完结构化输出流程。**禁止**用同一 task_prompt "
            "原样重派，请先精简 prompt 或拆分 subtask 再继续。",
        ],
    )
    return fallback.model_dump_json(indent=2)


def _dispatch_tasker_coder_sync(task_prompt: str) -> str:
    state = tasker_coder_agent.invoke(
        {"messages": [HumanMessage(content=task_prompt)]}
    )
    return _finalize_tasker_report(state.get("structured_response"))


async def _dispatch_tasker_coder_inner(task_prompt: str) -> str:
    on_event = on_event_var.get()
    state = await astream_collect_final_state(
        tasker_coder_agent,
        [HumanMessage(content=task_prompt)],
        on_event=on_event,
    )
    return _finalize_tasker_report(state.get("structured_response"))


async def adispatch_tasker_coder(
    task_prompt: str, on_event: OnEvent | None = None,
) -> str:
    token = on_event_var.set(on_event) if on_event is not None else None
    try:
        return await _dispatch_tasker_coder_inner(task_prompt)
    finally:
        if token is not None:
            on_event_var.reset(token)


dispatch_tasker_coder = StructuredTool.from_function(
    func=_dispatch_tasker_coder_sync,
    coroutine=_dispatch_tasker_coder_inner,
    name="dispatch_tasker_coder",
    description=_DISPATCH_TASKER_DESC,
)
