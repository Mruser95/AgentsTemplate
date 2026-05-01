from contextvars import ContextVar
from typing import Any, Awaitable, Callable, Literal, Optional
from langchain_openai import ChatOpenAI
from langchain.agents import create_agent
from langchain.agents.middleware import ModelCallLimitMiddleware
from langchain_core.messages import HumanMessage
from pathlib import Path
from pydantic import BaseModel, Field
from dotenv import load_dotenv
import asyncio
import os
import sys
import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
from Tools.terminal import SafeShell
from Tools.tavily import TavilySearch
from Tools.skills import SkillLibrary
from Tools.edit import Edit
from Tools.linter import LintOutcome, alint_paths, lint_paths
from Tools.utils import current_thread_id, workspace_dir
from agents_prompt import coder_prompt

# 事件流回调（供 Tasker_coder 等下游 agent 复用） 
OnEvent = Callable[[dict], Awaitable[None]]
on_event_var: ContextVar[Optional[OnEvent]] = ContextVar("on_event", default=None)

load_dotenv(PROJECT_ROOT / ".env")

with open(PROJECT_ROOT / "config.yaml", "r", encoding="utf-8") as f:
    _config = yaml.safe_load(f) or {}

coder_run_call_limit: int = _config.get("coder_run_call_limit", 30)
coder_thread_call_limit: int = _config.get("coder_thread_call_limit", 100)
coder_exit_behavior: str = _config.get("coder_exit_behavior", "end")
coder_lint_max_retries: int = _config.get("coder_lint_max_retries", 3)

llm = ChatOpenAI(
    model=os.getenv("code_llm_model"),
    api_key=os.getenv("code_llm_key"),
    base_url=os.getenv("code_llm_base_url"),
    stream_chunk_timeout=600,
)


# Coder Report Schema ==================================================================


CoderStatus = Literal["DONE", "DONE_WITH_CONCERNS", "NEEDS_CONTEXT", "BLOCKED"]
FileChangeAction = Literal["create", "modify", "delete", "read"]


class CoderModule(BaseModel):
    path: str = Field(description="模块文件的项目相对路径，例如 'Tools/foo.py'。")
    responsibility: str = Field(
        description="这个模块承担的唯一职责，一句话（描述做什么，不是怎么做）。"
    )
    public_api: list[str] = Field(
        default_factory=list,
        description="对外暴露的类 / 函数 / 常量名（不含参数签名）。私有辅助不要列。",
    )
    depends_on: list[str] = Field(
        default_factory=list,
        description="本模块依赖的其他项目内模块或第三方库的名字（不是路径）。",
    )


class FileChange(BaseModel):
    action: FileChangeAction = Field(
        description="对该文件做了什么：新增 / 修改 / 删除 / 仅查阅。"
    )
    path: str = Field(description="项目相对路径。")
    note: str = Field(
        default="",
        description="一句话说明改了哪一块 / 为什么改；仅查阅时说明为何读它。",
    )


class UsageExample(BaseModel):
    scenario: str = Field(description="场景 / 使用目的，一句话。")
    snippet: str = Field(description="可直接跑的代码片段或命令行。")


class LintResult(BaseModel):
    tool: str = Field(default="", description="执行的 lint 工具标识。")
    command: str = Field(default="", description="实际执行的命令原文。")
    exit_code: int = Field(default=-1, description="命令真实退出码。")
    passed: bool = Field(default=False, description="是否通过 gate 的 lint 关卡。")
    summary: str = Field(default="", description="聚合摘要（通过 / 未通过 / 跳过数）。")
    note: str = Field(default="", description="补充说明：缺工具跳过 / 超时 / 其它。")


class CoderReport(BaseModel):
    status: CoderStatus = Field(
        description=(
            "子任务执行状态，四选一："
            "DONE（完成且通过自验证）/ DONE_WITH_CONCERNS（完成但有疑虑）/ "
            "NEEDS_CONTEXT（缺上下文，需调用方补）/ BLOCKED（无法继续）。"
        )
    )
    task_name: str = Field(
        description="系统提示中派发的子任务名字，原样回填，用于调度器对账。"
    )
    summary: str = Field(
        description="1-3 句话：本次交付了什么能力、解决了什么问题。别贴代码。"
    )
    modules: list[CoderModule] = Field(
        default_factory=list,
        description="本次交付 / 涉及的主要模块清单（每个文件一条，私有或无关文件不列）。",
    )
    usage: str = Field(
        default="",
        description=(
            "整体用法说明：入口在哪、怎么调用、依赖什么前置条件。"
            "如果没新增对外接口，填空字符串。"
        ),
    )
    usage_examples: list[UsageExample] = Field(
        default_factory=list,
        description="0 到 N 个可直接跑的用法示例（代码片段 / 命令行）。",
    )
    file_changes: list[FileChange] = Field(
        default_factory=list,
        description="全部文件级操作：新增 / 修改 / 删除 / 仅查阅。",
    )
    verification: str = Field(
        default="",
        description=(
            "跑过的验证命令 + 关键输出 + 退出码。**不要写'应该通过'这类措辞**——"
            "没跑过就写空串，并把 status 降级为 DONE_WITH_CONCERNS。"
        ),
    )
    lint: LintResult = Field(
        default_factory=LintResult,
        description=(
            "由上层 Python gate（invoke_with_lint_gate）自动填充，agent 不必自填。"
            "Gate 会按扩展名跑语法级 lint（py_compile / node --check / gofmt / "
            "javac / gcc -fsyntax-only），不过会把错误塞回让 coder 继续修，最多"
            "重试 coder_lint_max_retries 次，超限自动将 status 置为 BLOCKED。"
        ),
    )
    key_decisions: list[str] = Field(
        default_factory=list,
        description="影响实现的关键判断 / 取舍 / 与原需求不一致之处。",
    )
    open_issues: list[str] = Field(
        default_factory=list,
        description="已知风险 / 未完成项 / 需调用方关注的疑虑。",
    )


# Coder Agent Factory ==================================================================


_TASK_PROMPT_SEPARATOR = "\n\n---\n\n"

def build_coder_agent(task_specific_prompt: str = ""):
    system_prompt = (
        coder_prompt + _TASK_PROMPT_SEPARATOR + task_specific_prompt
        if task_specific_prompt.strip()
        else coder_prompt
    )
    return create_agent(
        model=llm,
        tools=[SkillLibrary(), SafeShell(), Edit(), TavilySearch()],
        system_prompt=system_prompt,
        response_format=CoderReport,
        middleware=[
            ModelCallLimitMiddleware(
                run_limit=coder_run_call_limit,
                thread_limit=coder_thread_call_limit,
                exit_behavior=coder_exit_behavior,
            ),
        ],
    )


coder_agent = build_coder_agent()


# Lint Gate ============================================================================


def _changed_source_files(report: CoderReport) -> list[str]:
    tid = current_thread_id()
    ws = workspace_dir(tid)
    paths: list[str] = []
    for fc in report.file_changes:
        if fc.action not in ("create", "modify"):
            continue
        rel = fc.path.strip()
        if not rel:
            continue
        candidate = (ws / rel).resolve()
        try:
            candidate.relative_to(ws.resolve())
        except ValueError:
            continue
        paths.append(str(candidate))
    return paths


def _fill_lint_result(report: CoderReport, outcome: LintOutcome) -> None:
    total = len(outcome.entries)
    ok = sum(1 for e in outcome.entries if e.passed and not e.skipped_reason)
    failed = sum(1 for e in outcome.entries if not e.passed)
    skipped = sum(1 for e in outcome.entries if e.skipped_reason)
    summary = f"{total} files: {ok} passed, {failed} failed, {skipped} skipped"
    if failed:
        summary += "\n" + outcome.errors_digest(max_errors=3)
    note = "; ".join(sorted({e.skipped_reason for e in outcome.entries if e.skipped_reason}))
    report.lint = LintResult(
        tool="multi (syntax-level)",
        command="Tools.linter.lint_paths(<changed source files>)",
        exit_code=0 if outcome.passed else 1,
        passed=outcome.passed,
        summary=summary,
        note=note,
    )


def _retry_feedback(attempt: int, limit: int, outcome: LintOutcome) -> HumanMessage:
    return HumanMessage(content=(
        f"你刚才提交的 CoderReport 未通过强制 lint 检查"
        f"（第 {attempt + 1}/{limit} 轮）。"
        "必须根据下列报错修改对应源码，再**重新产出**一份完整的 CoderReport。"
        f"\n\n=== Lint 报错 ===\n{outcome.errors_digest()}"
    ))


def _finalize_blocked(report: CoderReport | None, outcome: LintOutcome | None, limit: int) -> CoderReport:
    assert report is not None and outcome is not None
    _fill_lint_result(report, outcome)
    report.status = "BLOCKED"
    report.open_issues = list(report.open_issues) + [
        f"Lint 连续 {limit + 1} 轮未通过：{outcome.errors_digest(max_errors=3)}",
    ]
    return report


def _missing_structured_response_report() -> CoderReport:
    return CoderReport(
        status="BLOCKED",
        task_name="(unknown)",
        summary="coder agent 未返回结构化 CoderReport。",
        modules=[],
        usage="",
        usage_examples=[],
        file_changes=[],
        verification="",
        key_decisions=[],
        open_issues=[
            "coder agent 未返回结构化 CoderReport：可能是 system prompt 过长被截断、"
            "工具预算耗尽，或模型未走完结构化输出流程。**禁止**原样重派，请精简 "
            "task_prompt 或拆得更小再继续。",
        ],
    )


def invoke_with_lint_gate(agent: Any, user_content: str, *, max_retries: int | None = None,) -> CoderReport:
    limit = coder_lint_max_retries if max_retries is None else max_retries
    messages: list = [HumanMessage(content=user_content)]
    report: CoderReport | None = None
    outcome: LintOutcome | None = None

    for attempt in range(limit + 1):
        state = agent.invoke({"messages": messages})
        report = state.get("structured_response")
        if not isinstance(report, CoderReport):
            return _missing_structured_response_report()

        outcome = lint_paths(_changed_source_files(report))
        if outcome.passed:
            _fill_lint_result(report, outcome)
            return report
        if attempt >= limit:
            break

        messages = list(state.get("messages") or messages) + [
            _retry_feedback(attempt, limit, outcome),
        ]

    return _finalize_blocked(report, outcome, limit)


async def astream_collect_final_state(agent: Any, messages: list, on_event: OnEvent | None = None) -> dict:
    final_state: dict = {}
    root_run_id: str | None = None
    async for event in agent.astream_events({"messages": messages}, version="v2"):
        if root_run_id is None and event.get("event") == "on_chain_start":
            root_run_id = event.get("run_id")
        if (
            event.get("event") == "on_chain_end"
            and event.get("run_id") == root_run_id
        ):
            output = (event.get("data") or {}).get("output")
            if isinstance(output, dict):
                final_state = output
        if on_event is not None:
            await on_event(event)
    return final_state


async def ainvoke_with_lint_gate(
    agent: Any, user_content: str, *,
    max_retries: int | None = None, on_event: OnEvent | None = None,
) -> CoderReport:
    limit = coder_lint_max_retries if max_retries is None else max_retries
    messages: list = [HumanMessage(content=user_content)]
    report: CoderReport | None = None
    outcome: LintOutcome | None = None

    for attempt in range(limit + 1):
        state = await astream_collect_final_state(agent, messages, on_event=on_event)
        report = state.get("structured_response")
        if not isinstance(report, CoderReport):
            return _missing_structured_response_report()

        # _changed_source_files 现在只是路径计算，但保持 to_thread 习惯避免阻塞 loop
        paths = await asyncio.to_thread(_changed_source_files, report)
        outcome = await alint_paths(paths)
        if outcome.passed:
            _fill_lint_result(report, outcome)
            return report
        if attempt >= limit:
            break

        messages = list(state.get("messages") or messages) + [
            _retry_feedback(attempt, limit, outcome),
        ]

    return _finalize_blocked(report, outcome, limit)
