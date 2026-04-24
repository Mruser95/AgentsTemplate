import argparse
import asyncio
import json
import sys
import traceback
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

SCHEDULE_DIR = PROJECT_ROOT / ".schedule"


_CREATOR_LABEL = {
    "user": "用户明确要求 manager 制定",
    "agent": "manager 在会话中自主决定制定",
    "unknown": "来源未知",
}


def _build_prompt(task_id: str, meta: dict) -> str:
    creator = meta.get("creator", "unknown")
    creator_desc = _CREATOR_LABEL.get(creator, "(非约定枚举值)")
    context_payload = meta.get("context") or {}
    context_json = json.dumps(context_payload, ensure_ascii=False, indent=2)
    return (
        f"【定时任务 · {meta.get('name', '(未命名)')}】\n"
        f"你（manager）在 {meta.get('created_at', '(未知时间)')} 登记了这条定时任务，"
        f"现在到点被自动唤醒。\n"
        f"任务 ID：{task_id}\n"
        f"发起者：{creator}（{creator_desc}）\n"
        f"执行者：manager（本工程里唯一被允许执行定时任务的 agent）\n"
        f"\n---\n"
        f"原始意图（到点要做什么）：\n{meta.get('intent', '')}\n"
        f"\n制定任务时记录下来的会话上下文（背景 / 目的 / 约束，JSON）：\n"
        f"```json\n{context_json}\n```\n"
        f"---\n\n"
        f"请把上面的上下文视作\u201c制定这条任务时的会话状态\u201d，据此恢复当时的思路并"
        f"按原始意图执行；完成后给出简短总结。"
    )


async def _amain() -> None:
    # 延迟 import：manager 模块会加载 checkpointer，避免在参数解析阶段就强制初始化
    from Agents.manager import manager_session

    ap = argparse.ArgumentParser()
    ap.add_argument("--task", required=True, help="创建任务时生成的 task_id")
    task_id = ap.parse_args().task

    meta_path = SCHEDULE_DIR / f"{task_id}.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    log_dir = SCHEDULE_DIR / task_id
    log_dir.mkdir(exist_ok=True)
    log_path = log_dir / f"{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    stamp = datetime.now().isoformat(timespec="seconds")

    try:
        prompt = _build_prompt(task_id, meta)
        thread_id = meta.get("thread_id") or f"schedule:{task_id}"
        async with manager_session(thread_id=thread_id) as sess:
            result = await sess.ainvoke(prompt)
        last = result["messages"][-1]
        content = getattr(last, "content", str(last))
        log_path.write_text(
            f"[OK] {stamp} thread_id={thread_id}\n\n{content}", encoding="utf-8",
        )
    except Exception:
        log_path.write_text(f"[ERR] {stamp}\n\n{traceback.format_exc()}", encoding="utf-8")
        raise


def main() -> None:
    asyncio.run(_amain())


if __name__ == "__main__":
    main()
