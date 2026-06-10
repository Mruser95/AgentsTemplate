from contextvars import ContextVar
from typing import Any, Awaitable, Callable, Literal, Optional
from langchain_openai import ChatOpenAI
from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware, ModelCallLimitMiddleware
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import StructuredTool
from openai import LengthFinishReasonError, OpenAIError
from pathlib import Path
import httpx
from pydantic import BaseModel, ConfigDict, Field
from dotenv import load_dotenv
import asyncio
import json
import os
import sys
import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
from Tools.terminal import SafeShell
from Tools.tavily import TavilySearch
from Tools.edit import Edit
from Tools.read import Read
from Tools.overview import Glob, Grep, RepoMap
from Tools.linter import LintOutcome, alint_paths, lint_paths
from Tools.utils import asalvage_structured, current_thread_id, llm_runtime_kwargs, salvage_structured, subagent_checkpointer, workspace_dir, workspace_info
from agents_prompt import coder_prompt

# 事件流回调（供 Tasker_coder 等下游 agent 复用） 
OnEvent = Callable[[dict], Awaitable[None]]
on_event_var: ContextVar[Optional[OnEvent]] = ContextVar("on_event", default=None)

load_dotenv(PROJECT_ROOT / ".env")

with open(PROJECT_ROOT / "config.yaml", "r", encoding="utf-8") as f:
    _config = yaml.safe_load(f) or {}

coder_run_call_limit: int = _config.get("coder_run_call_limit", 30)
coder_subtask_run_limit: int = _config.get("coder_subtask_run_limit", 40)
coder_exit_behavior: str = _config.get("coder_exit_behavior", "end")
coder_lint_max_retries: int = _config.get("coder_lint_max_retries", 2)
coder_max_tokens: int = int(_config.get("coder_max_tokens", 12000))

llm = ChatOpenAI(
    model=os.getenv("code_llm_model"),
    api_key=os.getenv("code_llm_key"),
    base_url=os.getenv("code_llm_base_url"),
    max_tokens=coder_max_tokens,
    use_responses_api=False,  # 强制走 Chat Completions：code_llm 常为 codex/o 系列，否则 langchain-openai 1.x 会自动路由到 /v1/responses，与我们现有 payload 形态不匹配
    **llm_runtime_kwargs("coder", _config),  # timeout / max_retries / stream_chunk_timeout / reasoning 见 config.yaml
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
    model_config = ConfigDict(extra="allow")

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
            "**长度硬上限：≤30 行 / 2000 字符**——只留命令本身、退出码和最关键的输出"
            "（报错只保留尾部 traceback），禁止粘贴完整日志。"
        ),
    )
    # 注意：lint 字段不在 schema 里。由 invoke_with_lint_gate 在结构化输出
    # 解析完成后通过 setattr 注入（CoderReport.model_config = extra='allow'），
    # 既能 model_dump 出来，也不会让 LLM 反复尝试自填嵌套 LintResult。
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


class _BudgetReminder(AgentMiddleware):

    def __init__(self, *, run_limit: int) -> None:
        super().__init__()
        self.run_limit = int(run_limit)

    def wrap_model_call(self, request, handler):  # type: ignore[override]
        return handler(self._with_reminder(request))

    async def awrap_model_call(self, request, handler):  # type: ignore[override]
        return await handler(self._with_reminder(request))

    def _with_reminder(self, request):
        text = self._reminder_text(request.state)
        if not text:
            return request
        return request.override(messages=list(request.messages) + [SystemMessage(content=text)])

    def _reminder_text(self, state) -> str:
        if self.run_limit <= 0:
            return ""
        used = int(state.get("run_model_call_count", 0))
        current = used + 1
        after = self.run_limit - current
        if after <= 0:
            return (
                f"\n\n[调用预算] 第 {self.run_limit}/{self.run_limit} 次——"
                "这是本次执行的**最后一次** LLM 调用：禁止再调用任何工具，"
                "立刻基于现有信息直接产出 CoderReport；代码 / 验证未完成的部分把 "
                "status 标成 DONE_WITH_CONCERNS，并在 open_issues 写清楚未完成项与建议。"
            )
        if after <= 2:
            return (
                f"\n\n[调用预算] 第 {current}/{self.run_limit} 次，本次之后只剩 {after} 次——"
                "**必须开始收尾**：先把代码落地、关键验证跑完，并预留至少一次调用直接产出 "
                "CoderReport（结构化输出本身也要占用一次调用，别拖到被强制截断）。"
                "不要再启动新的探索性工作。"
            )
        return (
            f"\n\n[调用预算] 第 {current}/{self.run_limit} 次 LLM 调用，本次之后还剩 {after} 次。"
            "请按剩余预算规划进度，临近上限主动收尾产出 CoderReport，别等被强制截断。"
        )


def build_coder_agent(task_specific_prompt: str = "", *, run_limit: int | None = None):
    effective_run_limit = coder_run_call_limit if run_limit is None else int(run_limit)
    system_prompt = (
        coder_prompt + _TASK_PROMPT_SEPARATOR + task_specific_prompt
        if task_specific_prompt.strip()
        else coder_prompt
    )
    return create_agent(
        model=llm,
        tools=[SafeShell(), Read(), Edit(), RepoMap(), Grep(), Glob(), TavilySearch()],
        system_prompt=system_prompt,
        response_format=CoderReport,
        checkpointer=subagent_checkpointer(_config),  # 默认 False=不落盘；config.subagent_persist_checkpoint=true 时继承 manager saver（仅调试）
        middleware=[
            _BudgetReminder(run_limit=effective_run_limit),
            ModelCallLimitMiddleware(
                run_limit=effective_run_limit,
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


def _recover_from_trajectory(messages: list | None) -> tuple[list[FileChange], list[str], str]:
    files: dict[str, str] = {}   # path -> action（首次出现为准：新建优先 create）
    commands: list[str] = []
    last_text = ""
    for m in messages or []:
        content = getattr(m, "content", "")
        if isinstance(m, AIMessage) and isinstance(content, str) and content.strip():
            last_text = content.strip()  # 取最后一段非空 AI 思考
        for tc in (getattr(m, "tool_calls", None) or []):
            try:
                name, args = tc.get("name"), (tc.get("args") or {})
            except AttributeError:
                continue
            if name == "edit":
                path = str(args.get("path") or "").strip()
                if path:
                    mode = str(args.get("mode") or "create")
                    files.setdefault(path, "create" if mode in ("create", "overwrite") else "modify")
            elif name == "terminal":
                cmd = str(args.get("command") or "").strip()
                if cmd:
                    commands.append(cmd[:200])
    file_changes = [
        FileChange(action=act, path=p, note="从被中断的执行轨迹恢复，可能未完成")
        for p, act in files.items()
    ]
    return file_changes, commands, last_text


def _missing_structured_response_report(messages: list | None = None) -> CoderReport:
    file_changes, commands, last_text = _recover_from_trajectory(messages)
    recovered = bool(file_changes or commands)
    verification = ""
    if commands:
        verification = "（未走完结构化输出；以下为轨迹中真实跑过的命令，仅作线索非通过证据）\n" + "\n".join(commands[-5:])
    if recovered:
        open_issues = [
            "coder 未走完结构化输出（多为**调用次数预算耗尽**——run_limit 撞顶，或被截断），但上方 "
            "file_changes / verification 是从执行轨迹**本地恢复的真实进展**。**重派是有效推进**："
            "下一个 coder 是全新子代理、调用次数预算也会刷新，能在已有进展上继续。**禁止从零原样重派**："
            "把“已存在哪些文件、已跑哪些命令、还差什么”写进新 task_prompt 让它**接着做**；并提示它"
            "**调用次数有限**——并行调用工具（一次回复并发多个 read/grep/terminal）、先落核心功能、"
            "尽早产出结构化报告。无冲突子任务可同一回复并行派发。",
        ]
        if last_text:
            open_issues.append("子代理中断前最后的思考：" + last_text[:300])
    else:  # 轨迹里也没有任何实际产出：退回通用提示
        open_issues = [
            "coder agent 未返回结构化 CoderReport：可能是 system prompt 过长被截断、"
            "**调用次数预算耗尽**（run_limit 撞顶），或模型未走完结构化输出流程。**禁止**原样重派，"
            "请精简 task_prompt 或拆得更小再继续（下一个子代理预算会刷新）。",
        ]
    return CoderReport(
        status="BLOCKED",
        task_name="(unknown)",
        summary=(
            "coder 未返回结构化 CoderReport；已从执行轨迹本地恢复部分进展（见 file_changes / open_issues）。"
            if recovered else "coder agent 未返回结构化 CoderReport。"
        ),
        modules=[],
        usage="",
        usage_examples=[],
        file_changes=file_changes,
        verification=verification,
        key_decisions=[],
        open_issues=open_issues,
    )


_SALVAGE_INSTRUCTION = (
    "以上是一个 coder 子代理的完整执行轨迹——它因为调用预算耗尽或输出被截断，"
    "没来得及产出结构化报告。请**严格基于轨迹里已经发生的事实**，把这次执行归纳成一份 "
    "CoderReport：已创建 / 修改的文件、真实跑过的验证命令与输出都要如实填写；"
    "verification 只写真正执行过的命令与输出，没跑过的留空并把 status 降级为 "
    "DONE_WITH_CONCERNS；若确实几乎没有有效产出，status 填 BLOCKED。"
)


_SALVAGE_TAIL_KEEP = 6  # salvage 只保留轨迹尾部最近 N 条原文，中间压成摘要——避免把撑爆预算的全量轨迹（可达十几万 token）原样重发，否则 salvage 调用会重蹈同一道墙


def _trajectory_digest(messages: list) -> str:
    """把整段轨迹压成要点摘要（已改文件 / 已跑命令 / 最后思考），在丢弃中间原文后仍保留事实线索。"""
    file_changes, commands, last_text = _recover_from_trajectory(messages)
    parts: list[str] = []
    if file_changes:
        parts.append("已改动文件：" + "；".join(f"{fc.action} {fc.path}" for fc in file_changes))
    if commands:
        parts.append("已跑命令（最近 5 条）：\n" + "\n".join(commands[-5:]))
    if last_text:
        parts.append("中断前最后思考：" + last_text[:300])
    return "\n".join(parts)


def _salvage_messages(messages: list) -> list | None:
    """预算耗尽 / 截断没走完结构化输出时，用现有对话历史拼出补救调用的输入；
    为避免把撑爆预算的全量轨迹原样再发一次（salvage 注定重蹈覆辙），只保留首条任务消息 +
    中间轨迹的要点摘要 + 尾部最近 N 条原文，并修掉首尾会破坏 tool_call 配对的悬空消息。
    若轨迹里没有任何实际产出（无 AI / Tool 消息）则返回 None，交回上层走 BLOCKED。"""
    if not any(isinstance(m, (AIMessage, ToolMessage)) for m in messages):
        return None
    head = messages[:1] if messages and isinstance(messages[0], HumanMessage) else []
    tail = list(messages[-_SALVAGE_TAIL_KEEP:])
    while tail and isinstance(tail[0], ToolMessage):  # 丢掉悬空 tool 响应（发起方在被省略的中间段），否则裸 tool 消息打头会 400
        tail = tail[1:]
    while tail and isinstance(tail[-1], AIMessage) and getattr(tail[-1], "tool_calls", None):
        tail = tail[:-1]  # 丢掉尾部「发起了 tool_call 但还没拿到响应」的 AIMessage：预算耗尽常停在这，直接接 HumanMessage 会破坏配对
    digest = _trajectory_digest(messages)
    bridge = [HumanMessage(content="（为控制长度，中间执行轨迹已省略，要点如下）\n" + digest)] if digest else []
    return head + bridge + tail + [HumanMessage(content=_SALVAGE_INSTRUCTION)]


def _salvage_report(messages: list) -> CoderReport | None:
    return salvage_structured(_salvage_messages(messages), llm, CoderReport)


async def _asalvage_report(messages: list) -> CoderReport | None:
    return await asalvage_structured(_salvage_messages(messages), llm, CoderReport)


def invoke_with_lint_gate(agent: Any, user_content: str, *, max_retries: int | None = None,) -> CoderReport:
    limit = coder_lint_max_retries if max_retries is None else max_retries
    messages: list = [HumanMessage(content=user_content)]
    report: CoderReport | None = None
    outcome: LintOutcome | None = None

    for attempt in range(limit + 1):
        try:
            state = agent.invoke({"messages": messages})
        except LengthFinishReasonError:
            return _missing_structured_response_report()
        report = state.get("structured_response")
        if not isinstance(report, CoderReport):
            report = _salvage_report(state.get("messages") or [])  # 预算耗尽没走完结构化输出时，补一次救回成果，再走同一道 lint gate
            if not isinstance(report, CoderReport):
                return _missing_structured_response_report(state.get("messages") or [])  # salvage 也失败（同一配额墙）：本地打捞轨迹进展，别返回空 BLOCKED

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
    seen: list = []  # 流式途中按序累积的 AI / Tool 消息：超时 / 截断时 root on_chain_end 跑不到、final_state 为空，用它兜底恢复已跑进展
    root_run_id: str | None = None
    try:
        async for event in agent.astream_events({"messages": messages}, version="v2"):
            kind = event.get("event")
            if root_run_id is None and kind == "on_chain_start":
                root_run_id = event.get("run_id")
            if kind == "on_chain_end" and event.get("run_id") == root_run_id:
                output = (event.get("data") or {}).get("output")
                if isinstance(output, dict):
                    final_state = output
            elif kind in ("on_chat_model_end", "on_tool_end"):
                out = (event.get("data") or {}).get("output")
                if isinstance(out, (AIMessage, ToolMessage)):  # AIMessageChunk 也是 AIMessage 子类
                    seen.append(out)
            if on_event is not None:
                await on_event(event)
    except (OpenAIError, httpx.HTTPError):
        # 子代理执行层统一收口：结构化输出撞 max_tokens 被截断 / 流式读超时 / 连接失败 / 限流(429) / 5xx
        # （含 SDK max_retries 耗尽后抛出的）——root on_chain_end 没跑到，final_state 多为空。
        # 返回流式途中累积的 seen 轨迹（含已改文件、已跑命令），让上层走 salvage / _missing_structured_response_report
        # 做本地恢复，把已跑进展捞回来，而不是把异常冒泡成上层的崩溃 / 空 stub。
        if final_state.get("messages"):
            return final_state
        return {"messages": seen}
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
            report = await _asalvage_report(state.get("messages") or [])  # 预算耗尽没走完结构化输出时，补一次救回成果，再走同一道 lint gate
            if not isinstance(report, CoderReport):
                return _missing_structured_response_report(state.get("messages") or [])  # salvage 也失败（同一配额墙）：本地打捞轨迹进展，别返回空 BLOCKED

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
    blocks.append(workspace_info(current_thread_id()))
    return "\n\n".join(blocks)


def _format_child_initial(task_name: str) -> str:
    return (
        f"上级调度器已把子任务 **{task_name}** 的完整规格同步到你的 system prompt。"
        "请按其中的需求、验证命令、边界约束动手执行；完成后用 CoderReport 结构化 "
        "schema 输出 JSON 介绍（主要模块、用法、验证证据都要填）。"
        "提交后系统会自动跑强制 lint 关卡；不过会把错误塞回来让你重改。"
    )


_DISPATCH_CODER_DESC = (
    "把**一个不复杂、单一内聚**的编码子任务直接派给一个全新 coder 子代理（相当于一名员工，便宜、直达）。"
    "适用：单文件或少数紧密相关文件的新增 / 修改、范围清晰、无需再拆子任务。"
    "需要拆成 ≥2 个可独立交付的子任务 / 跨多模块协同的中大型工作请改用 dispatch_tasker_coder（外包小队，更贵）。"
    "子代理隔离：只能看到通用 coder 编码规范 + 你写的 task_prompt，必须自包含（目标 / 精确相对路径 / "
    "具体需求 / 验证命令 / 边界约束）。返回 coder 产出的 CoderReport JSON（status / task_name / summary / "
    "modules / usage / usage_examples / file_changes / verification / key_decisions / open_issues），"
    "提交后框架自动跑强制 lint 关卡。请解析该 JSON 再决定下一步。"
)


def _exception_report(task_name: str, exc: Exception) -> CoderReport:
    return CoderReport(
        status="BLOCKED",
        task_name=task_name.strip() or "(unknown)",
        summary=f"coder 派发执行抛出异常：{exc!r}",
        open_issues=[f"exception: {exc!r}；多为网络 / API 错误（连接失败 / 限流 / 5xx）或重试耗尽，可稍后重派。"],
    )


def _dispatch_coder_sync(task_name: str, task_prompt: str, context: str = "") -> str:
    try:
        task_block = _format_task_specific_prompt(task_name, task_prompt, context)
        child_agent = build_coder_agent(task_specific_prompt=task_block)  # 默认 coder_run_call_limit（完整单跑预算）
        report = invoke_with_lint_gate(child_agent, _format_child_initial(task_name))
    except Exception as e:  # 顶层兜底：任何未被内部捕获的异常都收成 BLOCKED CoderReport，不冒泡给上层 manager
        report = _exception_report(task_name, e)
    return _coder_report_to_json(report)


async def _dispatch_coder_inner(task_name: str, task_prompt: str, context: str = "") -> str:
    try:
        task_block = _format_task_specific_prompt(task_name, task_prompt, context)
        child_agent = await asyncio.to_thread(build_coder_agent, task_specific_prompt=task_block)
        on_event = on_event_var.get()
        report = await ainvoke_with_lint_gate(child_agent, _format_child_initial(task_name), on_event=on_event)
    except Exception as e:  # 顶层兜底：同 _dispatch_coder_sync
        report = _exception_report(task_name, e)
    return _coder_report_to_json(report)


dispatch_coder = StructuredTool.from_function(
    func=_dispatch_coder_sync,
    coroutine=_dispatch_coder_inner,
    name="dispatch_coder",
    description=_DISPATCH_CODER_DESC,
)
