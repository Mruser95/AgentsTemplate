from typing import Any, Literal, Optional
from langchain_openai import ChatOpenAI
from langchain.agents import create_agent
from langchain.agents.middleware import ModelCallLimitMiddleware
from langchain_core.messages import HumanMessage
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
from agents_prompt import tester_prompt  # noqa: E402

load_dotenv(PROJECT_ROOT / ".env")

with open(PROJECT_ROOT / "config.yaml", "r", encoding="utf-8") as f:
    _config = yaml.safe_load(f) or {}

tester_run_call_limit: int = _config.get("tester_run_call_limit", 30)
tester_thread_call_limit: int = _config.get("tester_thread_call_limit", 100)
tester_exit_behavior: str = _config.get("tester_exit_behavior", "end")

DEFAULT_OUTPUT_PATH = PROJECT_ROOT / "Logs" / "TestDatasets.json"


llm = ChatOpenAI(
    model=os.getenv("agent_llm_model"),
    api_key=os.getenv("agent_llm_key"),
    base_url=os.getenv("agent_llm_base_url"),
)


# TestDataset Schema ==================================================================


TestCaseCategory = Literal[
    "happy_path",
    "edge_case",
    "boundary",
    "error_input",
    "adversarial",
]

class TestCase(BaseModel):
    name: str = Field(
        description=(
            "简短蛇形命名，描述被测行为而非序号，"
            "例如 'happy_path_perfect_square' / 'error_input_negative'。"
        )
    )
    category: TestCaseCategory = Field(
        description=(
            "用例分类，五选一："
            "happy_path（正常通路）/ edge_case（合法但非典型）/ "
            "boundary（数值或长度边界）/ error_input（非法输入预期报错）/ "
            "adversarial（对抗性输入）。"
        )
    )
    description: str = Field(
        description="一句话说这条用例在测什么行为（不是对 input 的重复）。"
    )
    input: Any = Field(
        description=(
            "任务的输入数据（可以是 str / dict / list 等）。"
            "字段结构必须与被测任务真实 schema 对齐，不得臆造字段。"
        )
    )
    expected_output: Optional[Any] = Field(
        default=None,
        description=(
            "精确预期输出。若无精确答案必须填 null —— "
            "和 judgment_criteria 恰有一个非空。"
        ),
    )
    judgment_criteria: str = Field(
        default="",
        description=(
            "没有精确答案时的评判标准；必须是**可机械判断**的条件，"
            "禁止'差不多 / 看起来对 / 合理'等模糊措辞。"
            "有精确答案时本字段必须为空字符串。"
        ),
    )

    @model_validator(mode="after")
    def _exactly_one_of_expected_or_criteria(self) -> "TestCase":
        has_expected = self.expected_output is not None
        has_criteria = bool(self.judgment_criteria.strip())
        if has_expected and has_criteria:
            raise ValueError(
                f"TestCase '{self.name}': expected_output 与 judgment_criteria "
                "同时给出；必须恰有一个非空（精确答案 vs 判断标准，二选一）。"
            )
        if not has_expected and not has_criteria:
            raise ValueError(
                f"TestCase '{self.name}': expected_output 与 judgment_criteria "
                "同时为空；必须恰有一个非空。"
            )
        return self


class TestDataset(BaseModel):
    task_summary: str = Field(
        description="一句话复述本数据集为哪个任务生成，便于追溯。"
    )
    cases: list[TestCase] = Field(
        default_factory=list,
        description="本次生成的测试用例列表（落盘时作为 JSON 顶层数组）。"
    )


# Tester Agent Factory ================================================================


_TASK_PROMPT_SEPARATOR = "\n\n---\n\n"

def build_tester_agent(task_specific_prompt: str = ""):
    system_prompt = (
        tester_prompt + _TASK_PROMPT_SEPARATOR + task_specific_prompt
        if task_specific_prompt.strip()
        else tester_prompt
    )
    return create_agent(
        model=llm,
        tools=[SkillLibrary(), SafeShell()],
        system_prompt=system_prompt,
        response_format=TestDataset,
        middleware=[
            ModelCallLimitMiddleware(
                run_limit=tester_run_call_limit,
                thread_limit=tester_thread_call_limit,
                exit_behavior=tester_exit_behavior,
            ),
        ],
    )


def _write_dataset_atomic(cases: list[TestCase], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    payload = [case.model_dump(mode="json") for case in cases]
    data = json.dumps(payload, ensure_ascii=False, indent=2)

    tmp = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        suffix=".json",
        prefix=".testdatasets-",
        dir=str(output_path.parent),
        delete=False,
    )
    try:
        tmp.write(data)
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp.close()
        os.replace(tmp.name, output_path)
        # tempfile 默认 0o600，调整为与项目内其他文件一致的 0o644
        os.chmod(output_path, 0o644)
    except Exception:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass
        raise


_INITIAL_HUMAN = (
    "请按 system prompt 中的规范为上面描述的任务生成测试数据集；"
    "完成后以 TestDataset 结构化 schema 输出 JSON（task_summary + cases）。"
)


def _ensure_dataset(dataset: Any) -> TestDataset:
    if not isinstance(dataset, TestDataset):
        raise RuntimeError(
            "tester agent 没有返回合法的 TestDataset 结构化响应："
            f"got {type(dataset).__name__}。请检查 task_prompt 是否清晰，"
            "或者调用预算是否被 ModelCallLimitMiddleware 提前截断。"
        )
    return dataset


def _validate_task_prompt(task_prompt: str) -> None:
    if not task_prompt.strip():
        raise ValueError(
            "task_prompt 为空：无法为空任务生成有意义的测试数据。"
        )


def generate_test_dataset(task_prompt: str, output_path: Optional[Path] = None) -> list[dict]:
    _validate_task_prompt(task_prompt)
    out = output_path or DEFAULT_OUTPUT_PATH
    agent = build_tester_agent(task_specific_prompt=task_prompt)
    state = agent.invoke({"messages": [HumanMessage(content=_INITIAL_HUMAN)]})
    dataset = _ensure_dataset(state.get("structured_response"))
    _write_dataset_atomic(dataset.cases, out)
    return [case.model_dump(mode="json") for case in dataset.cases]


async def agenerate_test_dataset(
    task_prompt: str, output_path: Optional[Path] = None,
) -> list[dict]:
    _validate_task_prompt(task_prompt)
    out = output_path or DEFAULT_OUTPUT_PATH
    agent = build_tester_agent(task_specific_prompt=task_prompt)
    state = await agent.ainvoke({"messages": [HumanMessage(content=_INITIAL_HUMAN)]})
    dataset = _ensure_dataset(state.get("structured_response"))
    # 原子写入是阻塞 IO；从 async 路径调时放线程池
    await asyncio.to_thread(_write_dataset_atomic, dataset.cases, out)
    return [case.model_dump(mode="json") for case in dataset.cases]


tester_agent = build_tester_agent()


# Dispatch Tool ========================================================================


_DISPATCH_TESTER_DESC = (
    "派发一个测试数据生成任务给 tester 子代理。"
    "task_prompt 必须自包含：清晰描述被测任务（输入 schema / 输出 schema / 错误语义 / "
    "边界条件 / 哪些字段一定不能臆造），子代理只能看到这一段。tester 不实现任务、"
    "不跑被测代码，只产出一份结构化 TestDataset（task_summary + cases）并落盘到 "
    "Logs/TestDatasets.json。返回值是一段 JSON：包含 count / output_path / cases。"
    "适合'先有任务规格、再生成验收数据'的环节；不要在不清楚被测函数 schema 时调用，"
    "会得到臆造字段的垃圾数据。"
)


def _format_dispatch_payload(cases: list[dict]) -> str:
    payload = {
        "count": len(cases),
        "output_path": str(DEFAULT_OUTPUT_PATH.relative_to(PROJECT_ROOT)),
        "cases": cases,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _dispatch_tester_sync(task_prompt: str) -> str:
    cases = generate_test_dataset(task_prompt)
    return _format_dispatch_payload(cases)


async def _dispatch_tester_async(task_prompt: str) -> str:
    cases = await agenerate_test_dataset(task_prompt)
    return _format_dispatch_payload(cases)


dispatch_tester = StructuredTool.from_function(
    func=_dispatch_tester_sync,
    coroutine=_dispatch_tester_async,
    name="dispatch_tester",
    description=_DISPATCH_TESTER_DESC,
)
