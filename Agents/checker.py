from typing import Literal, Optional
from langchain_openai import ChatOpenAI
from langchain.agents import create_agent
from langchain.agents.middleware import ModelCallLimitMiddleware
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage, get_buffer_string
from pathlib import Path
from pydantic import BaseModel, Field
from dotenv import load_dotenv
import json
import os
import sys
import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from Tools.terminal import SafeShell  # noqa: E402
from Tools.read import Read  # noqa: E402
from Tools.overview import Glob, Grep, RepoMap  # noqa: E402
from Tools.utils import llm_runtime_kwargs, subagent_checkpointer  # noqa: E402
from agents_prompt import checker_prompt  # noqa: E402

load_dotenv(PROJECT_ROOT / ".env")

with open(PROJECT_ROOT / "config.yaml", "r", encoding="utf-8") as f:
    _config = yaml.safe_load(f) or {}

checker_run_call_limit: int = _config.get("checker_run_call_limit", 20)
checker_exit_behavior: str = _config.get("checker_exit_behavior", "end")
checker_max_tokens: int = int(_config.get("checker_max_tokens", 2048))


llm = ChatOpenAI(
    model=os.getenv("small_llm_model"),
    api_key=os.getenv("small_llm_key"),
    base_url=os.getenv("small_llm_base_url"),
    max_tokens=checker_max_tokens,
    **llm_runtime_kwargs("checker", _config),
)


# CheckerReport Schema =================================================================


Alignment = Literal["on_track", "minor_drift", "major_drift", "off_track"]
DriftType = Literal[
    "scope_creep",           # plan 外的实质性工作
    "missing_step",          # 跳过了 plan 要求的某步
    "wrong_order",           # 步骤顺序违反依赖
    "constraint_violation",  # 违反 plan 的约束条款
    "rabbit_hole",           # 困在同一细节出不来
]
Severity = Literal["low", "medium", "high"]
Confidence = Literal["high", "medium", "low"]


class Deviation(BaseModel):
    type: DriftType = Field(description="偏离分类，五选一。")
    evidence: str = Field(
        description="从 transcript 或真实文件中摘的具体证据，一句话；禁止'大概 / 好像'。"
    )
    severity: Severity = Field(description="该偏离点的严重程度。")


class Suggestion(BaseModel):
    action: str = Field(
        description=(
            "给 manager 的**具体可执行**动作，例如"
            "'回到 milestone X 的 subtask Y' / '放弃当前分支' / "
            "'把 subtask Z 拆成 A+B'；禁止'再想想 / 增强一下'。"
        )
    )
    rationale: str = Field(description="为何这样调整，一句话。")
    priority: Severity = Field(description="紧迫性。")


class CheckerReport(BaseModel):
    overall_alignment: Alignment = Field(
        description="四选一：on_track / minor_drift / major_drift / off_track。"
    )
    drift_score: int = Field(
        ge=0, le=100,
        description=(
            "0-100 的偏离分；必须与 overall_alignment 档位一致："
            "0-10=on_track / 11-30=minor_drift / 31-60=major_drift / 61-100=off_track。"
            "判断在相邻档位之间犹豫时一律取更严格的那一档（默认从严）。"
        ),
    )
    current_phase: str = Field(
        description=(
            "一行：当前动作在 plan 里的位置（milestone/subtask 名 + 状态）；"
            "对不上就写 'plan 外：<简述>'。"
        )
    )
    progress_summary: str = Field(
        description="1-2 句话：transcript 里此刻实际正在做什么。"
    )
    deviations: list[Deviation] = Field(
        default_factory=list,
        description="具体偏离点清单；无偏离时为空列表（不要凑数）。",
    )
    problems: list[str] = Field(
        default_factory=list,
        description="当前方向的具体问题，每条一句话可操作；无则空列表。",
    )
    suggestions: list[Suggestion] = Field(
        default_factory=list,
        description="给 manager 的调整建议；on_track 时可为空列表。",
    )
    confidence: Confidence = Field(description="本次评估的置信度。")


def _plan_inject_text(plan: dict) -> str:
    return (
        "## 当前 plan.json（权威 · 必须遵守）\n\n"
        f"```json\n{json.dumps(plan, ensure_ascii=False, indent=2)}\n```"
    )


def build_checker_agent(plan: dict):
    system_prompt = f"{checker_prompt}\n\n---\n\n{_plan_inject_text(plan)}"
    return create_agent(
        model=llm,
        tools=[SafeShell(), Read(), RepoMap(), Grep(), Glob()],
        system_prompt=system_prompt,
        response_format=CheckerReport,
        checkpointer=subagent_checkpointer(_config),  # 默认 False=不落盘；config.subagent_persist_checkpoint=true 时继承 manager saver（仅调试）
        middleware=[
            ModelCallLimitMiddleware(
                run_limit=checker_run_call_limit,
                exit_behavior=checker_exit_behavior,
            ),
        ],
    )


def _build_user_content(messages: list) -> str:
    transcript = get_buffer_string(messages or [])
    return (
        "=== MESSAGES TRANSCRIPT ===\n"
        f"{transcript}\n\n"
        "请基于 system 中的 plan 与以上 transcript 评估偏离情况，以 CheckerReport 结构化 JSON 输出。"
    )


def run_checker(messages: list, plan: dict) -> dict:
    state = build_checker_agent(plan).invoke(
        {"messages": [HumanMessage(content=_build_user_content(messages))]}
    )
    report = state.get("structured_response")
    if not isinstance(report, CheckerReport):
        report = salvage_checker(state.get("messages") or [])
    if not isinstance(report, CheckerReport):
        return checker_failed_report()
    return report.model_dump()


async def arun_checker(messages: list, plan: dict) -> dict:
    state = await build_checker_agent(plan).ainvoke(
        {"messages": [HumanMessage(content=_build_user_content(messages))]}
    )
    report = state.get("structured_response")
    if not isinstance(report, CheckerReport):
        report = await asalvage_checker(state.get("messages") or [])
    if not isinstance(report, CheckerReport):
        return checker_failed_report()
    return report.model_dump()


# 结构化补救（仿 tester）：主跑因把预算耗在旁白 / 被截断而没产出 CheckerReport 时，
# 直接打 llm（绕开带预算中间件的 agent，不受调用上限约束），用 structured_output
# 强制基于已发生轨迹补一份合法 CheckerReport；仍失败才回退 checker_failed_report()。
_SALVAGE_INSTRUCTION = (
    "以上是一次执行路径偏离检查的完整轨迹——它把调用预算耗在了自然语言旁白上、"
    "或被截断，没来得及产出结构化 CheckerReport。请**严格基于轨迹里已核对到的事实**，"
    "把它归纳成一份合法的 CheckerReport：只填轨迹中真实出现过的核对结论，不要臆造；"
    "没落地核对过的判断一律取从严档位，并把 confidence 降到 low。"
)


def _salvage_input(messages: list) -> Optional[list]:
    """用现有对话历史拼补救调用输入；轨迹里没有任何实际产出（无 AI / Tool 消息）时返回 None。"""
    if not any(isinstance(m, (AIMessage, ToolMessage)) for m in messages):
        return None
    return list(messages) + [HumanMessage(content=_SALVAGE_INSTRUCTION)]


def salvage_checker(messages: list) -> Optional[CheckerReport]:
    payload = _salvage_input(messages)
    if payload is None:
        return None
    try:
        out = llm.with_structured_output(CheckerReport).invoke(payload)
    except Exception:
        return None
    return out if isinstance(out, CheckerReport) else None


async def asalvage_checker(messages: list) -> Optional[CheckerReport]:
    payload = _salvage_input(messages)
    if payload is None:
        return None
    try:  # 同 salvage_checker，走异步不阻塞事件循环
        out = await llm.with_structured_output(CheckerReport).ainvoke(payload)
    except Exception:
        return None
    return out if isinstance(out, CheckerReport) else None


def checker_failed_report() -> dict:
    return CheckerReport(
        overall_alignment="off_track",
        drift_score=80,
        current_phase="checker 自身失败",
        progress_summary="checker_agent 未返回合法的 CheckerReport 结构化响应。",
        deviations=[],
        problems=[
            "checker_agent 未能产出结构化 CheckerReport，"
            "可能是调用预算被提前截断或模型输出漂移。"
        ],
        suggestions=[
            Suggestion(
                action=(
                    "提高 checker_run_call_limit，"
                    "或精简 messages / plan 后让 manager 重新触发 hard gate。"
                ),
                rationale="预算不足或输入过长会导致 structured_response 缺失。",
                priority="medium",
            )
        ],
        confidence="low",
    ).model_dump()
