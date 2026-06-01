from typing import Any, Literal, Optional
from langchain_openai import ChatOpenAI
from langchain.agents import create_agent
from langchain.agents.middleware import ModelCallLimitMiddleware
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import StructuredTool
from pathlib import Path
from pydantic import BaseModel, Field, model_validator
from dotenv import load_dotenv
import asyncio
import json
import os
import sys
import tempfile
import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from Tools.terminal import SafeShell  # noqa: E402
from Tools.skills import SkillLibrary  # noqa: E402
from Tools.read import Read  # noqa: E402
from Tools.tavily import TavilySearch  # noqa: E402
from Tools.utils import current_thread_id, ensure_workspace, llm_runtime_kwargs, subagent_checkpointer  # noqa: E402
from agents_prompt import tester_prompt, runner_prompt  # noqa: E402

load_dotenv(PROJECT_ROOT / ".env")
_CFG = yaml.safe_load((PROJECT_ROOT / "config.yaml").read_text(encoding="utf-8")) or {}
DATASET_FILENAME = "TestDatasets.json"

llm = ChatOpenAI(
    model=os.getenv("code_llm_model"),
    api_key=os.getenv("code_llm_key"),
    base_url=os.getenv("code_llm_base_url"),
    disable_streaming=not bool(_CFG.get("subagent_stream_output", False)),
    **llm_runtime_kwargs("tester", _CFG),
)


# ===== Schemas (Field.description 是给 LLM 的契约，请勿删) ============================

TestCaseCategory = Literal["happy_path", "edge_case", "boundary", "error_input", "adversarial"]


class TestCase(BaseModel):
    model_config = {
        "extra": "forbid",
        "json_schema_extra": {
            "oneOf": [
                {
                    "description": "有精确答案：expected_output 非 null，judgment_criteria 必须为空串。",
                    "properties": {
                        "expected_output": {"not": {"type": "null"}},
                        "judgment_criteria": {"const": ""},
                    },
                },
                {
                    "description": "无精确答案：expected_output 为 null，judgment_criteria 必须有非空白字符。",
                    "properties": {
                        "expected_output": {"type": "null"},
                        "judgment_criteria": {"pattern": r".*\S.*"},
                    },
                },
            ]
        },
    }

    name: str = Field(description=(
        "简短蛇形命名，描述被测行为而非序号，例如 'happy_path_perfect_square' / 'error_input_negative'。"
        "禁止使用 'id' / 'index' 等序号字段（schema 里没有这个字段）。"
    ))
    category: TestCaseCategory = Field(description=(
        "用例分类，五选一：happy_path（正常通路）/ edge_case（合法但非典型）/ "
        "boundary（数值或长度边界）/ error_input（非法输入预期报错）/ adversarial（对抗性输入）。"
    ))
    description: str = Field(description="一句话说这条用例在测什么行为（不是对 input 的重复）。")
    input: Any = Field(description=(
        "任务的输入数据（可以是 str / dict / list 等）。"
        "字段结构必须与被测任务真实 schema 对齐，不得臆造字段。"
    ))
    expected_output: Optional[Any] = Field(default=None, description=(
        "精确预期输出。若无精确答案必须填 null —— 和 judgment_criteria 恰有一个非空。"
    ))
    judgment_criteria: str = Field(default="", description=(
        "没有精确答案时的评判标准；必须是**可机械判断**的条件，"
        "禁止'差不多 / 看起来对 / 合理'等模糊措辞。有精确答案时本字段必须为空字符串。"
    ))

    @model_validator(mode="after")
    def _xor_expected_criteria(self) -> "TestCase":
        if (self.expected_output is not None) == bool(self.judgment_criteria.strip()):
            raise ValueError(
                f"TestCase '{self.name}': expected_output 与 judgment_criteria 必须恰有一个非空。"
            )
        return self


class TestDataset(BaseModel):
    model_config = {"extra": "forbid"}
    task_summary: str = Field(description="一句话复述本数据集为哪个任务生成，便于追溯。")
    cases: list[TestCase] = Field(default_factory=list, description=(
        "本次生成的测试用例列表（落盘时作为 JSON 顶层数组）。每个元素必须严格包含 "
        "name / category / description / input / expected_output / judgment_criteria 六个字段，禁止额外字段（如 id）。"
    ))


FailureKind = Literal[
    "assertion",            # actual_output 不匹配 expected_output
    "criteria_unmet",       # 不满足 judgment_criteria
    "exception",            # 被测代码抛异常 / 进程非零退出
    "timeout",              # 超时
    "schema_mismatch",      # 输入/输出形状与被测程序不符
    "missing_dependency",   # 依赖缺失（包、文件、环境变量）
    "external_unreachable", # 真实外部资源不可达（网络、站点、API）
    "skipped",              # 用例不可执行且经判断为合理跳过（仍计 fail，等 manager 决策）
    "other",
]


class TestCaseResult(BaseModel):
    model_config = {
        "extra": "forbid",
        "json_schema_extra": {
            "allOf": [
                {
                    "if": {"properties": {"passed": {"const": True}}, "required": ["passed"]},
                    "then": {
                        "description": "passed=True 时 failure_kind 必须为 null，failure_reason 必须为空串。",
                        "properties": {
                            "failure_kind": {"type": "null"},
                            "failure_reason": {"const": ""},
                        },
                    },
                    "else": {
                        "description": "passed=False 时 failure_kind 必填，failure_reason 必须有非空白字符。",
                        "properties": {
                            "failure_kind": {"not": {"type": "null"}},
                            "failure_reason": {"pattern": r".*\S.*"},
                        },
                    },
                },
                {
                    "description": "evidence 必须有非空白字符。",
                    "properties": {"evidence": {"pattern": r".*\S.*"}},
                },
            ]
        },
    }
    name: str = Field(description="对应 TestDataset 中的 case.name，必须能在数据集中找到。")
    category: TestCaseCategory = Field(description="原 case 的分类，原样回填。")
    passed: bool = Field(description="本条用例是否通过。")
    actual_output: Optional[Any] = Field(default=None, description=(
        "被测程序实际输出（数据 / 异常类型 / 退出码摘要）；passed 与否都尽量填，便于 manager 复盘。"
    ))
    failure_kind: Optional[FailureKind] = Field(default=None, description=(
        "failed 时必须给一个 FailureKind 枚举值；passed 时必须为 null。"
    ))
    failure_reason: str = Field(default="", description=(
        "failed 时一句话定位根因（如 '返回 4.5，期望 4.0'、'抛 KeyError ...'）；passed 时必须为空字符串。"
    ))
    evidence: str = Field(description=(
        "可追溯证据：实际命令、关键 stdout / stderr 摘录、退出码、文件路径；务必引用真实片段，禁止编造。"
    ))

    @model_validator(mode="after")
    def _consistency(self) -> "TestCaseResult":
        if self.passed and (self.failure_kind is not None or self.failure_reason.strip()):
            raise ValueError(f"TestCaseResult '{self.name}': passed=True 时 failure_kind/failure_reason 必须为空。")
        if not self.passed and (self.failure_kind is None or not self.failure_reason.strip()):
            raise ValueError(f"TestCaseResult '{self.name}': passed=False 必须给 failure_kind 与 failure_reason。")
        if not self.evidence.strip():
            raise ValueError(f"TestCaseResult '{self.name}': evidence 不得为空。")
        return self


class TestReport(BaseModel):
    model_config = {"extra": "forbid"}
    task_summary: str = Field(description="一句话复述被测任务，便于追溯。")
    dataset_path: str = Field(description="实际跑的 TestDatasets.json 路径（项目根相对路径优先）。")
    total: int = Field(ge=0, description="数据集中实际执行的用例总数。")
    passed: int = Field(ge=0)
    failed: int = Field(ge=0)
    overall: Literal["all_pass", "partial_fail", "all_fail"] = Field(description=(
        "overall 必须由 passed/failed/total 派生：全过=all_pass，全挂=all_fail，否则 partial_fail。"
    ))
    results: list[TestCaseResult] = Field(default_factory=list, description="每条用例一项，顺序与数据集一致。")
    diagnosis: str = Field(default="", description=(
        "失败模式归纳与给 manager 的下一步建议："
        "如 '4/5 fail，皆为 schema_mismatch：被测函数签名是 (x: float)，用例传 dict，需修用例'。全过时填 ''。"
    ))

    @model_validator(mode="after")
    def _math(self) -> "TestReport":
        if self.total == 0 or self.total != len(self.results):
            raise ValueError("TestReport: total 必须 >0 且与 results 长度一致。")
        p = sum(1 for r in self.results if r.passed)
        f = self.total - p
        expected = "all_pass" if f == 0 else "all_fail" if p == 0 else "partial_fail"
        if (self.passed, self.failed, self.overall) != (p, f, expected):
            raise ValueError("TestReport: passed/failed/overall 与 results 不一致。")
        if f and not self.diagnosis.strip():
            raise ValueError("TestReport: 存在 failed 用例时 diagnosis 不得为空。")
        return self


# ===== 共用辅助 ========================================================================

def _output_path() -> Path:
    """workspace 下的 TestDatasets.json；无 thread 上下文时回退 Logs/。"""
    tid = current_thread_id()
    return (ensure_workspace(tid) / DATASET_FILENAME) if tid else (PROJECT_ROOT / "Logs" / DATASET_FILENAME)


def _rel(p: Path) -> str:
    try:
        return str(p.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(p)


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", suffix=".json",
        prefix=".testdatasets-", dir=str(path.parent), delete=False,
    )
    try:
        tmp.write(text); tmp.flush(); os.fsync(tmp.fileno()); tmp.close()
        os.replace(tmp.name, path)
        os.chmod(path, 0o644)
    except Exception:
        try: os.unlink(tmp.name)
        except OSError: pass
        raise


def _build_agent(prompt: str, schema: type[BaseModel], task_prompt: str, prefix: str):
    sp = f"{prompt}\n\n---\n\n{task_prompt}" if task_prompt.strip() else prompt
    # tester 与 runner 共用底层 llm，按 prefix 绑定各自 max_tokens（无配置时回退 4096）
    bound_llm = llm.bind(max_tokens=int(_CFG.get(f"{prefix}_max_tokens", 4096)))
    return create_agent(
        model=bound_llm,
        tools=[SkillLibrary(), SafeShell(), Read(), TavilySearch()],
        system_prompt=sp,
        response_format=schema,
        checkpointer=subagent_checkpointer(_CFG),  # 默认 False=不落盘；config.subagent_persist_checkpoint=true 时继承 manager saver（仅调试）
        middleware=[ModelCallLimitMiddleware(
            run_limit=_CFG.get(f"{prefix}_run_call_limit", 30),
            exit_behavior=_CFG.get(f"{prefix}_exit_behavior", "end"),
        )],
    )


# 结构化结果解析 / 失败补救（仿 coder：缺结构化时补救一次，仍失败则回带解释的失败报告交 manager 决策，不抛异常）
_SALVAGE_INSTRUCTION = (
    "以上是一个子代理的完整执行轨迹——它因为流式分片把工具调用 JSON 拼坏、调用预算耗尽或被截断，"
    "没来得及产出结构化结果。请**严格基于轨迹里已发生的事实**，把它重新归纳成一份合法的 {name}："
    "只填轨迹中真实出现过的内容，不要臆造。"
)


def _structured(state: dict, schema: type[BaseModel]) -> Optional[BaseModel]:
    out = state.get("structured_response")
    return out if isinstance(out, schema) else None


def _salvage_input(messages: list, schema: type[BaseModel]) -> Optional[list]:
    """用现有对话历史拼出补救调用输入；轨迹里没有任何实际产出（无 AI / Tool 消息）时返回 None。"""
    if not any(isinstance(m, (AIMessage, ToolMessage)) for m in messages):
        return None
    return list(messages) + [HumanMessage(content=_SALVAGE_INSTRUCTION.format(name=schema.__name__))]


def _salvage(messages: list, schema: type[BaseModel]) -> Optional[BaseModel]:
    payload = _salvage_input(messages, schema)
    if payload is None:
        return None
    try:  # 直接打 llm（不经带预算中间件的 agent，故不受调用上限约束）
        out = llm.with_structured_output(schema).invoke(payload)
    except Exception:
        return None
    return out if isinstance(out, schema) else None


async def _asalvage(messages: list, schema: type[BaseModel]) -> Optional[BaseModel]:
    payload = _salvage_input(messages, schema)
    if payload is None:
        return None
    try:  # 同 _salvage，走异步不阻塞事件循环
        out = await llm.with_structured_output(schema).ainvoke(payload)
    except Exception:
        return None
    return out if isinstance(out, schema) else None


def _failure_report(state: dict, schema: type[BaseModel], prefix: str) -> dict:
    """补救仍失败时的兜底：带根因解释的失败报告交 manager 决策（manager 见 status=BLOCKED 自行裁决，禁止原样重派）。"""
    reason = f"未发现 {schema.__name__} 工具调用：可能调用预算耗尽或模型未走完结构化输出。"
    for m in reversed(state.get("messages", []) or []):
        for itc in getattr(m, "invalid_tool_calls", None) or []:
            if itc.get("name") == schema.__name__:
                a = itc.get("args") or ""
                reason = (f"{schema.__name__} 工具调用的 arguments 是非法 JSON（多为流式分片拼接损坏）："
                          f"len={len(a)}，head={a[:120]!r}。")
                break
    return {
        "ok": False,
        "status": "BLOCKED",
        "agent": prefix,
        "error": f"{prefix} 未产出合法 {schema.__name__} 结构化结果。",
        "reason": reason,
        "advice": "禁止原样重派：请精简 / 拆小 task_prompt，或把用例分批后再试。",
    }


def _make_dispatch(
    *, name: str, prompt: str, schema: type[BaseModel], prefix: str,
    human: str, post,  # post: (BaseModel) -> dict   组装 JSON 返回体
    description: str, input_arg: str = "task_prompt",
) -> StructuredTool:
    """生成一对 sync/async 派发函数并打包成 StructuredTool；tester / runner 共用。"""

    def _check(p: str) -> None:
        if not p.strip():
            raise ValueError(f"{prefix}: {input_arg} 为空")

    def _sync(**kw) -> str:
        p = kw[input_arg]; _check(p)
        state = _build_agent(prompt, schema, p, prefix).invoke({"messages": [HumanMessage(content=human)]})
        out = _structured(state, schema) or _salvage(state.get("messages") or [], schema)  # 缺结构化则补救一次
        payload = post(out) if out is not None else _failure_report(state, schema, prefix)
        return json.dumps(payload, ensure_ascii=False, indent=2)

    async def _async(**kw) -> str:
        p = kw[input_arg]; _check(p)
        state = await _build_agent(prompt, schema, p, prefix).ainvoke({"messages": [HumanMessage(content=human)]})
        out = _structured(state, schema)
        if out is None:  # 缺结构化则补救一次
            out = await _asalvage(state.get("messages") or [], schema)
        payload = await asyncio.to_thread(post, out) if out is not None else _failure_report(state, schema, prefix)
        return json.dumps(payload, ensure_ascii=False, indent=2)

    # StructuredTool.from_function 通过函数签名推断参数；动态改名以匹配 input_arg
    _sync.__name__ = f"_{name}_sync"
    _async.__name__ = f"_{name}_async"
    return StructuredTool.from_function(
        func=lambda **kw: _sync(**kw),
        coroutine=lambda **kw: _async(**kw),
        name=name,
        description=description,
        args_schema=_make_args_schema(name, input_arg),
    )


def _make_args_schema(tool_name: str, arg_name: str) -> type[BaseModel]:
    return type(
        f"{tool_name}_args",
        (BaseModel,),
        {"__annotations__": {arg_name: str}, arg_name: Field(...)},
    )


# ===== 派发工具：tester（生成数据集）+ runner（执行并出报告） =======================

_TESTER_HUMAN = (
    "请按 system prompt 中的规范为上面描述的任务生成测试数据集；"
    "完成后以 TestDataset 结构化 schema 输出 JSON（task_summary + cases）。"
)
_TESTER_DESC = (
    "派发 tester 子代理生成结构化 TestDataset 并落盘到 workspace/TestDatasets.json"
    "（无 thread 上下文回退 Logs/TestDatasets.json）。"
    "task_prompt 必须自包含被测任务的输入/输出 schema、错误语义、边界条件；"
    "tester 不实现任务、不跑被测代码，只产数据。返回 JSON：count / output_path / cases。"
)


def _persist_dataset(ds: BaseModel) -> dict:
    assert isinstance(ds, TestDataset)
    out = _output_path()
    cases = [c.model_dump(mode="json") for c in ds.cases]
    _atomic_write(out, json.dumps(cases, ensure_ascii=False, indent=2))
    return {"count": len(cases), "output_path": _rel(out), "cases": cases}


dispatch_tester = _make_dispatch(
    name="dispatch_tester", prompt=tester_prompt, schema=TestDataset, prefix="tester",
    human=_TESTER_HUMAN, post=_persist_dataset,
    description=_TESTER_DESC, input_arg="task_prompt",
)


_RUNNER_HUMAN = (
    "请按 system prompt 中的规范读取 TestDatasets.json，逐条执行被测程序，"
    "记录实际输出与判定，最后以 TestReport 结构化 schema 返回完整报告。"
)
_RUNNER_DESC = (
    "派发 runner 子代理读 TestDatasets.json 跑全量用例，返回 TestReport JSON："
    "task_summary / dataset_path / total / passed / failed / overall / results[] / diagnosis；"
    "fail 时 results[i].failure_kind+failure_reason+evidence。"
    "run_prompt 必须自包含：被测程序入口（脚本/模块/CLI）、调用方式、依赖、TestDatasets.json 路径。"
    "禁止用本工具修被测代码 / 改测试集——只跑 + 只报。"
)

dispatch_test_runner = _make_dispatch(
    name="dispatch_test_runner", prompt=runner_prompt, schema=TestReport, prefix="runner",
    human=_RUNNER_HUMAN, post=lambda r: r.model_dump(mode="json"),
    description=_RUNNER_DESC, input_arg="run_prompt",
)
