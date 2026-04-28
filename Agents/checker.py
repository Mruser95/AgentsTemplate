from typing import Literal
from langchain_openai import ChatOpenAI
from langchain.agents import create_agent
from langchain.agents.middleware import ModelCallLimitMiddleware
from pathlib import Path
from pydantic import BaseModel, Field
from dotenv import load_dotenv
import os
import sys
import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from Tools.terminal import SafeShell  # noqa: E402
from Tools.skills import SkillLibrary  # noqa: E402
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
                    "提高 checker_run_call_limit / thread_call_limit，"
                    "或精简 messages / plan 后让 manager 重新触发 hard gate。"
                ),
                rationale="预算不足或输入过长会导致 structured_response 缺失。",
                priority="medium",
            )
        ],
        confidence="low",
    ).model_dump()
