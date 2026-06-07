#!/usr/bin/env python3
"""命令行入口：把"构建 agent 的标准"注入 manager 上下文，非交互地构建一个成品 agent 到 BuiltAgents/。

用法：
    python build_agent.py "构建一个能查天气并总结成简报的 agent"
    python build_agent.py "<任务描述>"

manager 根据任务自行取 <slug> 作为 BuiltAgents/<slug>/ 文件夹名；全程不与用户交互，
直接产出成品（agent.py / requirements.txt / README.md）并自验收。
"""
from __future__ import annotations

import argparse
import asyncio
import os
import re
import shutil
import sys
import venv
import yaml
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(PROJECT_ROOT / ".env")

BUILT_AGENTS_DIR = PROJECT_ROOT / "BuiltAgents"
STANDARD_SKILL = PROJECT_ROOT / "SkillTree" / "agent_dev" / "build_agent.md"

DONE_RE = re.compile(r"BUILD_RESULT:\s*DONE\s+([A-Za-z0-9_\-./]+)")
BLOCKED_RE = re.compile(r"BUILD_RESULT:\s*BLOCKED\b")

_BIN = "Scripts" if os.name == "nt" else "bin"
_PY = "python.exe" if os.name == "nt" else "python"

CONTINUE_MSG = (
    "【续跑】上一轮你已停止，但尚未输出 BUILD_RESULT: DONE / BLOCKED。"
    "先 plan(read) 看清当前进度与卡点，分析为何提前停下（预算用尽 / subtask 未完成 / "
    "验收没过 / 子代理 BLOCKED 未自救等），自行补救后继续执行剩余 subtask 并真验收。"
    "禁止询问用户、禁止等确认、禁止重复已 done 的工作。完成或彻底卡死时，"
    "最后一条消息最后一行严格输出 BUILD_RESULT: DONE <slug> 或 BUILD_RESULT: BLOCKED <原因>。"
)


def _max_retries() -> int:
    with open(PROJECT_ROOT / "config.yaml", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    return max(int(cfg.get("build_agent_max_retries", 5)), 1)


def _read_standard() -> str:
    from Tools.skills import _parse_frontmatter

    _, body = _parse_frontmatter(STANDARD_SKILL.read_text(encoding="utf-8"))
    return body.strip()


def _build_injection(task: str, standard: str) -> str:
    header = (
        "【自动化 agent 构建 · 非交互批处理模式】\n\n"
        "你运行在命令行批处理里，没有真人会回话。禁止澄清提问、禁止「要我开始吗/等你确认」。"
        "立刻：把必要假设写进 plan 的 constraints/notes → 直接把 plan 置为 ready → 进入 Executing "
        "一路做到底，直到成品 agent 构建并真验收通过。同一需求不要做任何澄清轮。\n\n"
        "目标：按下方《构建标准》用 CompLib 组件 + 原生 Tools 拼出一个可运行的成品 agent，"
        "安装到本项目 BuiltAgents/ 下。\n\n"
        "落盘位置与结构：\n"
        "- 你的 workspace 已映射到项目的 BuiltAgents/ 目录。**只新建一个子文件夹** <slug>/"
        "（slug 由你按任务语义取的简短英文蛇形名，如 weather_briefing），全部文件放进去；"
        "**不要改动** BuiltAgents/ 下任何已存在的同级文件夹。\n"
        "- <slug>/ 内必含：agent.py（入口：构建并暴露 agent，且带 if __name__ == \"__main__\": 的"
        "真实最小冒烟）、requirements.txt（依赖）、README.md（用途 / 运行方式 / 依赖 / env 约定）。\n"
        "- 路径铁律：BuiltAgents/<slug>/agent.py 里用 "
        "PROJECT_ROOT = Path(__file__).resolve().parents[2] 定位项目根并 sys.path.insert 之，"
        "再 import CompLib.* / Tools.*。\n\n"
        "派发纪律（关键）：coder 子代理**看不到 CompLib**，也没有 component_library / skill_tree 工具。"
        "dispatch 时必须把下方《构建标准》里**确切的 import 路径与 API 片段**抄进每个自包含 task_prompt；"
        "也可让 coder 用 read_file 去读你点名的 CompLib 文件。\n\n"
        "验收（必须真跑，不能只看退出码）：按《构建标准》第 5 节——用到的组件逐个冒烟 + 端到端 "
        "ainvoke 真跑通且返回非空 + 产物类开真产物核验。验收在 BuiltAgents/<slug>/ 下执行"
        "（terminal 的 cwd 已锁在 workspace=BuiltAgents，venv 已含项目依赖，可直接 "
        "import langchain / CompLib）。可 dispatch_test_runner，或你亲自 terminal 真跑 "
        "python <slug>/agent.py 并贴输出。\n\n"
        "完成信号（机器读取，务必遵守）：全部构建并真验收通过后，你**最后一条消息的最后一行**"
        "必须严格是一行：\n"
        "BUILD_RESULT: DONE <slug>\n"
        "若彻底卡死无法推进：最后一行输出 BUILD_RESULT: BLOCKED <一句原因>。"
        "除这一行外照常给 ≤200 字简短交付报告。"
    )
    return "\n\n".join([
        header,
        "## 要构建的 agent（任务）\n" + task,
        "## 构建标准（agent_dev/build_agent，权威）\n" + standard,
    ])


def _setup_thread(thread_id: str) -> Path:
    """建 SessionDB/<tid>/：workspace 软链到 BuiltAgents（写入直达成品目录）、
    venv 继承系统 site-packages（验收时可直接 import 项目的 langchain / CompLib 栈）。"""
    base = PROJECT_ROOT / "SessionDB" / thread_id
    base.mkdir(parents=True, exist_ok=True)

    ws = base / "workspace"
    if ws.is_symlink():
        ws.unlink()
    elif ws.exists():
        raise SystemExit(f"workspace 已存在且非软链，请换 --thread-id：{ws}")
    BUILT_AGENTS_DIR.mkdir(parents=True, exist_ok=True)
    os.symlink(BUILT_AGENTS_DIR, ws)

    vd = base / ".venv"
    if not (vd / _BIN / _PY).exists():
        venv.EnvBuilder(
            with_pip=True, system_site_packages=True, symlinks=(os.name != "nt")
        ).create(str(vd))
    return base


def _cleanup_thread(base: Path) -> None:
    ws = base / "workspace"
    try:
        if ws.is_symlink():
            ws.unlink()  # 只删软链，BuiltAgents 内的成品不受影响
    except OSError:
        pass
    shutil.rmtree(base, ignore_errors=True)


def _existing_agents() -> set[str]:
    if not BUILT_AGENTS_DIR.exists():
        return set()
    return {p.name for p in BUILT_AGENTS_DIR.iterdir() if p.is_dir()}


async def _ensure_ckpt() -> None:
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    from Tools.utils import CHECKPOINT_DB

    CHECKPOINT_DB.parent.mkdir(parents=True, exist_ok=True)
    async with AsyncSqliteSaver.from_conn_string(str(CHECKPOINT_DB)) as saver:
        await saver.setup()


async def _run_turn(thread_id: str, message: str) -> str:
    """跑 manager 一轮；实时打印工具进度与 manager 输出，返回 manager 最终回复文本。"""
    from Agents.manager import manager_session

    final_parts: list[str] = []
    async with manager_session(thread_id) as sess:
        async for name, payload in sess.astream(message):
            if name == "token":
                sys.stdout.write(payload)
                sys.stdout.flush()
                final_parts.append(payload)
            elif name == "tool_start":
                final_parts.clear()  # 工具调用前的文本不是最终回复
                sys.stdout.write(f"\n\n  ⟶ [{payload.get('name')}]\n")
                sys.stdout.flush()
    sys.stdout.write("\n")
    return "".join(final_parts)


async def amain(args: argparse.Namespace) -> int:
    task = args.task.strip()
    if not task:
        raise SystemExit("任务描述不能为空。")
    standard = _read_standard()
    thread_id = args.thread_id or f"build_agent_{datetime.now():%Y%m%d_%H%M%S}"

    base = _setup_thread(thread_id)
    await _ensure_ckpt()

    before = _existing_agents()
    msg = _build_injection(task, standard)
    max_retries = _max_retries()
    status, slug = "incomplete", None
    try:
        from schedule import scheduler

        for rnd in range(1, max_retries + 1):
            print(f"\n===== round {rnd}/{max_retries} (thread={thread_id}) =====", flush=True)
            final = await _run_turn(thread_id, msg)
            done = DONE_RE.search(final)
            if done:
                slug = done.group(1).strip().strip("/").split("/")[0]
                status = "done"
                break
            if BLOCKED_RE.search(final):
                status = "blocked"
                break
            if rnd < max_retries:
                print(f"\n[build_agent] 未收到 BUILD_RESULT，自动续跑 ({rnd}/{max_retries})…", flush=True)
                msg = CONTINUE_MSG
            else:
                print(f"\n[build_agent] 已达最大续跑次数 {max_retries}，停止。", flush=True)
    finally:
        try:
            scheduler.shutdown()
        except Exception:
            pass
        _cleanup_thread(base)

    new_dirs = sorted(_existing_agents() - before)
    if not slug and new_dirs:
        slug = new_dirs[0]

    print("\n" + "=" * 60)
    print(f"状态：{status}")
    if slug and (BUILT_AGENTS_DIR / slug).is_dir():
        target = BUILT_AGENTS_DIR / slug
        print(f"成品目录：{target}")
        files = sorted(p for p in target.rglob("*") if p.is_file())
        for p in files[:40]:
            print(f"  - {p.relative_to(BUILT_AGENTS_DIR)}")
    elif new_dirs:
        print(f"新建目录：{', '.join(new_dirs)}（BuiltAgents/ 下）")
    else:
        print("未在 BuiltAgents/ 下检测到新成品目录。")
    print("=" * 60)
    return 0 if status == "done" else 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description="非交互地用 manager + CompLib 标准构建一个成品 agent 到 BuiltAgents/。"
    )
    parser.add_argument("task", help="要构建的 agent 的任务描述（用引号括起整段）。")
    parser.add_argument(
        "--thread-id", default=None,
        help="可选：指定会话 thread_id（默认按时间戳生成）。",
    )
    args = parser.parse_args()
    return asyncio.run(amain(args))


if __name__ == "__main__":
    raise SystemExit(main())
