from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Literal, Optional, Type
from langchain_core.tools import BaseTool
from langgraph.prebuilt import InjectedState
from pydantic import BaseModel, Field
import json
import sys

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from Agents.checker import run_checker, arun_checker  # noqa: E402
from Tools.utils import current_thread_id  # noqa: E402


def _plan_path(thread_id: str) -> Path:
    d = PROJECT_ROOT / "SessionDB" / thread_id
    d.mkdir(parents=True, exist_ok=True)
    return d / "plan.json"


PlanAction = Literal[
    "read",
    "write",
    "update_subtask_status",
    "set_milestone_status",
    "set_plan_status",
    "clear",
]


class PlanInput(BaseModel):
    action: PlanAction = Field(
        description=(
            "read / write / update_subtask_status / set_milestone_status / "
            "set_plan_status / clear"
        )
    )
    plan_json: Optional[str] = Field(
        default=None,
        description=(
            "action=write 必填：完整 plan JSON 文本（覆盖式重写）。"
            "建议先 read 拿到现状，再在内存里改完一次性 write 回去。"
        ),
    )
    subtask_id: Optional[str] = Field(
        default=None,
        description="action=update_subtask_status 必填，例如 'm1-t1'",
    )
    milestone_id: Optional[str] = Field(
        default=None,
        description="action=set_milestone_status 必填，例如 'm1'",
    )
    new_status: Optional[str] = Field(
        default=None,
        description=(
            "action=update_subtask_status / set_milestone_status 取值："
            "'pending' / 'in_progress' / 'done' / 'blocked'；"
            "action=set_plan_status 取值："
            "'drafting' / 'ready' / 'executing' / 'done' / 'blocked'"
        ),
    )
    result_summary: Optional[str] = Field(
        default=None,
        description="action=update_subtask_status 可选：本 subtask 的结果一句话",
    )
    state: Annotated[Optional[dict], InjectedState] = Field(
        default=None,
        description="LangGraph 注入的 agent state（含 messages），LLM 不可见、不需要填",
    )


# Plan I/O Helpers ====================================================================


def _read_plan(path: Path) -> Optional[dict]:
    if not path.exists():
        return None
    raw = path.read_text(encoding="utf-8").strip()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def _write_plan(path: Path, plan: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    plan["updated_at"] = datetime.now().isoformat(timespec="seconds")
    path.write_text(
        json.dumps(plan, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _find_subtask(plan: dict, subtask_id: str) -> tuple[Optional[dict], Optional[dict]]:
    for m in plan.get("milestones", []):
        for st in m.get("subtasks", []):
            if st.get("id") == subtask_id:
                return m, st
    return None, None


def _find_milestone(plan: dict, milestone_id: str) -> Optional[dict]:
    for m in plan.get("milestones", []):
        if m.get("id") == milestone_id:
            return m
    return None


# Plan Tool ==========================================================================


HARD_GATE_THRESHOLD = 50
HARD_GATE_NOTE = "（偏离分过大，系统判定为不通过，请修复或补充证据）"


def _apply_hard_gate(subtask_id: str, plan: dict, report: dict) -> bool:
    """硬门禁（纯代码控制，不依赖 LLM）：drift_score > 50 时，把该 subtask 及其所属
    milestone 一并回退为 in_progress，并把系统判定语写进 report。返回是否触发。"""
    if int(report.get("drift_score", 0)) <= HARD_GATE_THRESHOLD:
        return False
    m, st = _find_subtask(plan, subtask_id)
    if st is not None:
        st["status"] = "in_progress"
        if m is not None:
            m["status"] = "in_progress"
        _write_plan(_plan_path(current_thread_id()), plan)
    report["system_verdict"] = HARD_GATE_NOTE
    return True


def _format_done_response(subtask_id: str, report: dict, gated: bool = False) -> str:
    if gated:
        head = (
            f"=== 硬门禁不通过：subtask `{subtask_id}` drift_score="
            f"{report.get('drift_score')} > {HARD_GATE_THRESHOLD} ===\n"
            "已自动把该 subtask 及其所属 milestone 一并回退为 in_progress（本次 done 不予采纳）。\n\n"
        )
    else:
        head = f"=== subtask `{subtask_id}` 已写入 plan.json，状态 = done ===\n\n"
    return (
        head
        + "=== Checker 强制对齐报告（hard gate） ===\n"
        f"{json.dumps(report, ensure_ascii=False, indent=2)}\n\n"
        "=== 你下一步必须做的（铁律） ===\n"
        "* on_track / minor_drift  → 直接开始下一个 subtask（按 plan 拓扑派发 / 执行）。\n"
        "* major_drift / off_track → 立即按 suggestions 调整（回滚刚才的状态 / "
        "重做 / 拆分 subtask），**禁止**继续推进；必要时把 plan.status 改回 "
        "'drafting' 并向用户复盘。\n"
        "* 任何情况下都不得忽略本报告。"
    )


# Plan Tool ==========================================================================


class Plan(BaseTool):
    name: str = "plan"
    description: str = (
        "管理 SessionDB/<thread_id>/plan.json 的读写。actions:\n"
        "- read: 返回当前 plan dict 的 JSON 字符串；空 / 非法时返回提示语；\n"
        "- write: 用 plan_json 覆盖整个 plan.json（写完会自动盖 updated_at；首次 write "
        "会自动加 created_at）；\n"
        "- update_subtask_status: 改某个 subtask 的 status（pending/in_progress/done/blocked）。"
        "**当 new_status='done' 时，本工具会强制调 checker_agent 做对齐检查，"
        "并把 CheckerReport 嵌入返回值；manager 必须读完 report 再决定下一步。"
        "若 report.drift_score > 50，系统会硬门禁判定不通过：自动把该 subtask 及其所属 "
        "milestone 一并回退为 in_progress 并在 report 附判定语，此 done 不予采纳**；\n"
        "- set_milestone_status: 改 milestone 的 status；\n"
        "- set_plan_status: 改 plan.status（drafting/ready/executing/done/blocked）；\n"
        "- clear: 清空 plan.json。\n"
        "**只有 manager 能用此工具**。plan.json 是唯一可信事实源，只能经本工具读写"
        "（不得用 terminal 直接 cat/改）；write 是**整文件覆盖**——先 read 现状，"
        "在内存改完一次性写回，避免漏字段；updated_at/created_at 自动维护，不要手填。"
    )
    args_schema: Type[BaseModel] = PlanInput

    def _run(
        self, action: str, plan_json: Optional[str] = None, subtask_id: Optional[str] = None, milestone_id: Optional[str] = None,
        new_status: Optional[str] = None, result_summary: Optional[str] = None, state: Annotated[dict, InjectedState] = None,
    ) -> str:
        prep = self._prepare(
            action=action,
            plan_json=plan_json,
            subtask_id=subtask_id,
            milestone_id=milestone_id,
            new_status=new_status,
            result_summary=result_summary,
        )
        if isinstance(prep, str):
            return prep
        kind, payload = prep
        if kind == "subtask_done":
            messages = (state or {}).get("messages", [])
            report = run_checker(messages, payload["plan"])
            gated = _apply_hard_gate(payload["subtask_id"], payload["plan"], report)
            return _format_done_response(payload["subtask_id"], report, gated)
        return payload

    async def _arun(
        self, action: str, plan_json: Optional[str] = None, subtask_id: Optional[str] = None, milestone_id: Optional[str] = None,
        new_status: Optional[str] = None, result_summary: Optional[str] = None, state: Annotated[dict, InjectedState] = None,
    ) -> str:
        prep = self._prepare(
            action=action,
            plan_json=plan_json,
            subtask_id=subtask_id,
            milestone_id=milestone_id,
            new_status=new_status,
            result_summary=result_summary,
        )
        if isinstance(prep, str):
            return prep
        kind, payload = prep
        if kind == "subtask_done":
            messages = (state or {}).get("messages", [])
            report = await arun_checker(messages, payload["plan"])
            gated = _apply_hard_gate(payload["subtask_id"], payload["plan"], report)
            return _format_done_response(payload["subtask_id"], report, gated)
        return payload


    def _prepare(
        self, action: str, plan_json: Optional[str], subtask_id: Optional[str],
        milestone_id: Optional[str], new_status: Optional[str], result_summary: Optional[str],
    ) -> Any:
        path = _plan_path(current_thread_id())

        if action == "read":
            plan = _read_plan(path)
            if plan is None:
                return "(plan.json 为空 / 不存在 / 非法 JSON)"
            return json.dumps(plan, ensure_ascii=False, indent=2)

        if action == "clear":
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("", encoding="utf-8")
            return "plan.json 已清空。"

        if action == "write":
            if not plan_json:
                return "write 需要 plan_json（完整 plan JSON 文本）。"
            try:
                plan = json.loads(plan_json)
            except json.JSONDecodeError as e:
                return f"plan_json 不是合法 JSON：{e}"
            if not plan.get("created_at"):
                plan["created_at"] = datetime.now().isoformat(timespec="seconds")
            _write_plan(path, plan)
            return f"plan.json 已写入。当前 status={plan.get('status', '?')}。"

        plan = _read_plan(path)
        if plan is None:
            return "plan.json 为空，请先 write 写入完整 plan 再调本工具。"

        if action == "update_subtask_status":
            if not subtask_id or not new_status:
                return "update_subtask_status 需要 subtask_id + new_status。"
            _, st = _find_subtask(plan, subtask_id)
            if st is None:
                return f"未找到 subtask_id={subtask_id}。"
            st["status"] = new_status
            if result_summary is not None:
                st["result_summary"] = result_summary
            _write_plan(path, plan)
            if new_status == "done":
                return ("subtask_done", {"subtask_id": subtask_id, "plan": plan})
            return f"subtask {subtask_id} 状态已更新为 {new_status}。"

        if action == "set_milestone_status":
            if not milestone_id or not new_status:
                return "set_milestone_status 需要 milestone_id + new_status。"
            m = _find_milestone(plan, milestone_id)
            if m is None:
                return f"未找到 milestone_id={milestone_id}。"
            m["status"] = new_status
            _write_plan(path, plan)
            return f"milestone {milestone_id} 状态已更新为 {new_status}。"

        if action == "set_plan_status":
            if not new_status:
                return "set_plan_status 需要 new_status。"
            plan["status"] = new_status
            _write_plan(path, plan)
            return f"plan.status 已更新为 {new_status}。"

        return (
            f"未知 action '{action}'。可选: read / write / update_subtask_status / "
            "set_milestone_status / set_plan_status / clear。"
        )


# Plan 自动注入（manager 每次 model call，messages 末尾追加 SystemMessage）====


def plan_inject_text() -> str:
    plan = _read_plan(_plan_path(current_thread_id()))
    if plan is None:
        return (
            "## 当前 plan.json（自动注入 · 权威 · 必须遵守）\n\n"
            "(plan.json 为空 / 不存在 / 非法 JSON)"
        )
    return (
        "## 当前 plan.json（自动注入 · 权威 · 必须遵守）\n\n"
        "以下 plan 是唯一事实源，不得偏离 goal / constraints / subtask 拓扑；"
        "无需重复 plan(action='read')。\n\n"
        f"```json\n{json.dumps(plan, ensure_ascii=False, indent=2)}\n```"
    )
