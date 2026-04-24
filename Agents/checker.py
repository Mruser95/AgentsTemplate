from typing import Literal, Union
from langchain_openai import ChatOpenAI
from langchain.agents import create_agent
from langchain.agents.middleware import ModelCallLimitMiddleware
from langchain_core.messages import BaseMessage, HumanMessage, get_buffer_string
from langchain_core.tools import tool
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
from Tools.skills import SkillLibrary  # noqa: E402
from Tools._context import current_thread_id  # noqa: E402
from agents_prompt import checker_prompt  # noqa: E402

load_dotenv(PROJECT_ROOT / ".env")

with open(PROJECT_ROOT / "config.yaml", "r", encoding="utf-8") as f:
    _config = yaml.safe_load(f) or {}

checker_run_call_limit: int = _config.get("checker_run_call_limit", 20)
checker_thread_call_limit: int = _config.get("checker_thread_call_limit", 60)
checker_exit_behavior: str = _config.get("checker_exit_behavior", "end")


llm = ChatOpenAI(
    model=os.getenv("agent_llm_model"),
    api_key=os.getenv("agent_llm_key"),
    base_url=os.getenv("agent_llm_base_url"),
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
            "0-15=on_track / 16-40=minor_drift / 41-70=major_drift / 71-100=off_track。"
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


def build_checker_agent():
    return create_agent(
        model=llm,
        tools=[SkillLibrary(), SafeShell()],
        system_prompt=checker_prompt,
        response_format=CheckerReport,
        middleware=[
            ModelCallLimitMiddleware(
                run_limit=checker_run_call_limit,
                thread_limit=checker_thread_call_limit,
                exit_behavior=checker_exit_behavior,
            ),
        ],
    )

checker_agent = build_checker_agent()


# Convenience Tool =====================================================================


def _load_plan(plan_path: str) -> Union[dict, str]:
    p = Path(plan_path)
    if not p.is_absolute():
        p = PROJECT_ROOT / p
    if not p.exists():
        return f"plan file not found: {p}"
    raw = p.read_text(encoding="utf-8").strip()
    if not raw:
        return f"plan file empty: {p}"
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        return f"plan file not valid JSON: {p} ({e})"
    if not data:
        return f"plan file placeholder (empty object/array): {p}"
    return data


def _fallback_report(*, current_phase: str, progress_summary: str, problems: list[str],
    suggestions: list[Suggestion], drift_score: int = 85,
) -> dict:
    return CheckerReport(
        overall_alignment="off_track",
        drift_score=drift_score,
        current_phase=current_phase,
        progress_summary=progress_summary,
        deviations=[],
        problems=problems,
        suggestions=suggestions,
        confidence="low",
    ).model_dump()


@tool
async def check_alignment(messages: list[BaseMessage], plan_path: str = "") -> dict:
    """
    检查 messages 消息流的执行路径是否偏离 plan.json 制定的实现流程。
    输入:
      - messages:  langgraph 消息流（list[BaseMessage]），工具内部用
                   get_buffer_string 序列化为 transcript 文本。
      - plan_path: plan 文件路径（项目相对或绝对）；空串时默认取
                   'SessionDB/<current_thread_id>/plan.json'。
    流程: 1) 读 plan.json；2) 序列化 messages；3) 拼 HumanMessage 喂给
         checker_agent；4) 返回 CheckerReport 的 dict。
    返回: CheckerReport 的 dict —— overall_alignment / drift_score /
         current_phase / progress_summary / deviations / problems /
         suggestions / confidence。plan 缺失 / 非法时返回 off_track 兜底报告。
    """
    if not plan_path:
        plan_path = f"SessionDB/{current_thread_id()}/plan.json"
    plan = _load_plan(plan_path)
    transcript = get_buffer_string(messages)

    if isinstance(plan, str):
        return _fallback_report(
            current_phase="plan 不可用",
            progress_summary="无法读取 / 解析 plan，跳过 checker_agent，直接给出兜底报告。",
            problems=[plan],
            suggestions=[
                Suggestion(
                    action=(
                        "停下当前实现，先在 SessionDB/<thread_id>/plan.json 里把 goal / "
                        "milestones / subtasks / notes 写清楚再继续。"
                    ),
                    rationale="没有 plan 就没有对齐标尺，任何偏离判断都是瞎猜。",
                    priority="high",
                ),
            ],
        )

    user_content = (
        "=== PLAN ===\n"
        f"{json.dumps(plan, ensure_ascii=False, indent=2)}\n\n"
        "=== MESSAGES TRANSCRIPT ===\n"
        f"{transcript}\n\n"
        "请基于以上 plan 和 transcript 评估偏离情况，以 CheckerReport "
        "结构化 JSON 输出（不要 markdown fence、不要自由文本）。"
    )

    state = await checker_agent.ainvoke({"messages": [HumanMessage(content=user_content)]})
    report = state.get("structured_response")

    if not isinstance(report, CheckerReport):
        return _fallback_report(
            current_phase="checker 自身失败",
            progress_summary="checker_agent 未返回合法的 CheckerReport 结构化响应。",
            problems=[
                "checker_agent 未能产出结构化 CheckerReport，"
                "可能是调用预算被提前截断或模型输出漂移。"
            ],
            suggestions=[
                Suggestion(
                    action=(
                        "提高 checker_run_call_limit / thread_call_limit，"
                        "或精简 messages / plan 后重试 check_alignment。"
                    ),
                    rationale="预算不足或输入过长会导致 structured_response 缺失。",
                    priority="medium",
                ),
            ],
            drift_score=80,
        )
    return report.model_dump()
