from __future__ import annotations

import asyncio
import json
import os
import re
import sqlite3
import sys
import traceback
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable, Literal, Optional

import yaml
from dotenv import load_dotenv
from langchain_core.messages import BaseMessage, get_buffer_string
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from Memory import longMem  # noqa: E402
from Tools.utils import current_thread_id  # noqa: E402

load_dotenv()


# config & paths ==============================================================


CKPT_DB = ROOT / "SessionDB" / "checkpoints.db"
CUR_DB = ROOT / "SessionDB" / "collator.db"
LOG_DIR = ROOT / "Logs" / "collator"

_cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8")) or {}
UNSETTLED_THRESHOLD = int(_cfg.get("collator_unsettled_threshold", 30))
MAX_PARALLEL = int(_cfg.get("collator_max_parallel", 2))
RETRY_COUNT = int(_cfg.get("collator_retry_count", 1))
LONG_MEM_K = int(_cfg.get("collator_long_memory_k", 5))

llm = ChatOpenAI(
    model=os.getenv("agent_llm_model"),
    api_key=os.getenv("agent_llm_key"),
    base_url=os.getenv("agent_llm_base_url"),
)


# schemas ====================================================================


MemoryType = Literal[
    "fact", "event", "preference", "emotion",
    "skill", "relationship", "knowledge",
]

class LongMemoryCandidate(BaseModel):
    content: str = Field(description="Candidate memory content")
    memory_type: MemoryType = Field(description="Candidate memory type")
    importance: int = Field(ge=1, le=5, description="Candidate importance 1-5")
    context: str = Field(default="", description="Candidate context / background")
    tags: list[str] = Field(default_factory=list, description="Candidate tags")
    timestamp: str = Field(
        default_factory=lambda: datetime.now().isoformat(),
        description="Candidate ISO timestamp (defaults to now)",
    )


class LongMemoryDecision(BaseModel):
    candidate_index: int = Field(
        ge=0,
        description="0-based index of the candidate in the input candidates list this decision refers to",
    )
    action: Literal["insert", "update", "skip", "delete"] = Field(
        description=(
            "insert = write the candidate as a new row; "
            "update = replace fields of an existing row (target_id required); "
            "skip   = drop the candidate, keep DB unchanged; "
            "delete = remove an existing row that is now wrong/obsolete (target_id required)."
        )
    )
    target_id: Optional[int] = Field(
        default=None,
        description="Existing row id to act on. Required for update/delete, must be None for insert/skip.",
    )
    content: Optional[str] = Field(
        default=None,
        description="New content. Required for insert/update; ignored otherwise. May be a merged rewrite of candidate + existing row.",
    )
    memory_type: Optional[MemoryType] = Field(
        default=None,
        description="New memory_type. Required for insert/update; ignored otherwise.",
    )
    importance: Optional[int] = Field(
        default=None, ge=1, le=5,
        description="New importance 1-5. Required for insert/update; ignored otherwise.",
    )
    context: Optional[str] = Field(
        default=None,
        description="New context. Optional for insert/update; ignored otherwise.",
    )
    tags: Optional[list[str]] = Field(
        default=None,
        description="New tags. Optional for insert/update; ignored otherwise.",
    )
    reason: str = Field(
        description="Short justification grounded in importance / timestamp / memory_type / similarity."
    )


class LongMemoryCurationBatch(BaseModel):
    decisions: list[LongMemoryDecision] = Field(
        default_factory=list,
        description="Exactly one decision per input candidate, in any order.",
    )


class SkillExperienceEdit(BaseModel):
    action: Literal["add", "update", "replace", "remove"] = Field(
        description=(
            "add     = append a new bullet to the experience list; "
            "update  = rewrite an existing bullet identified by target_index (1-based); "
            "replace = same as update but signals semantic overwrite of an outdated lesson; "
            "remove  = delete the bullet at target_index."
        )
    )
    target_index: Optional[int] = Field(
        default=None, ge=1,
        description="1-based index into the current experience list. Required for update/replace/remove; must be None for add.",
    )
    content: Optional[str] = Field(
        default=None,
        description=(
            "New bullet text. Required for add/update/replace. "
            "Must follow the format: '应该避免做X, 否则会导致Y, 应该做Z' "
            "(or its English equivalent if the surrounding doc is English). "
            "Keep it one sentence, concrete, tool-actionable."
        ),
    )
    reason: str = Field(
        description="Why this edit is justified by the transcript (cite the failure / success pattern observed)."
    )


class SkillCurationBatch(BaseModel):
    skill_path: str = Field(
        description="Target skill markdown path, e.g. 'Skills/terminal_skill.md'. Echo back the path provided in the input."
    )
    edits: list[SkillExperienceEdit] = Field(
        default_factory=list,
        description="0 to 3 edits. Empty list is a legitimate answer when no new lesson is worth recording.",
    )


# prompts ====================================================================


from toolagent_prompt import LONG_CURATOR_PROMPT as LONG_PROMPT, SKILL_CURATOR_PROMPT as SKILL_PROMPT  # noqa: E402,F401

long_chain = (
    ChatPromptTemplate.from_messages([
        ("system", LONG_PROMPT),
        ("human", "candidates:\n{candidates}\n\nexisting:\n{existing}"),
    ])
    | llm.with_structured_output(LongMemoryCurationBatch)
)

skill_chain = (
    ChatPromptTemplate.from_messages([
        ("system", SKILL_PROMPT),
        (
            "human",
            "skill_path: {skill_path}\n"
            "tool_name: {tool_name}\n"
            "current_experiences:\n{current_experiences}\n\n"
            "transcript:\n{transcript}",
        ),
    ])
    | llm.with_structured_output(SkillCurationBatch)
)


# skill markdown helpers ======================================================


_HEAD_RE = re.compile(r'^\s*##\s+探索经验\s*$')
_FENCE_RE = re.compile(r'^\s*```')
_BULLET_RE = re.compile(r'^(\s*)(\d+)\.\s+(.*)$')
_HOLDER_RE = re.compile(r'^\s*(?:\d+\.\s+)?\.{3,}\s*$')


def _find_block(lines: list[str]) -> Optional[tuple[int, int, int, list[str]]]:
    """返回 (fence_open, fence_close, indent, bullets) 或 None。"""
    h = next((i for i, ln in enumerate(lines) if _HEAD_RE.match(ln)), None)
    if h is None:
        return None
    a = next((j for j in range(h + 1, len(lines)) if _FENCE_RE.match(lines[j])), None)
    if a is None:
        return None
    b = next((j for j in range(a + 1, len(lines)) if _FENCE_RE.match(lines[j])), None)
    if b is None:
        return None
    indent, bullets = 0, []
    for j in range(a + 1, b):
        if _HOLDER_RE.match(lines[j]):
            continue
        m = _BULLET_RE.match(lines[j])
        if m:
            if not bullets:
                indent = len(m.group(1))
            bullets.append(m.group(3).rstrip())
    return (a, b, indent, bullets)


def _apply_edits(bullets: list[str], edits: list[SkillExperienceEdit]) -> tuple[list[str], list[dict]]:
    cur = list(bullets)
    results: list[dict] = []
    for e in edits:
        out: dict = {"action": e.action, "target_index": e.target_index, "ok": False}
        try:
            i = e.target_index
            need_idx = e.action != "add"
            if need_idx and (i is None or not 1 <= i <= len(cur)):
                raise ValueError(f"target_index {i} out of range 1..{len(cur)}")
            if e.action != "remove" and not e.content:
                raise ValueError(f"{e.action} requires content")
            if e.action == "add":
                cur.append(e.content.strip())
            elif e.action == "remove":
                cur.pop(i - 1)
            else:  # update / replace
                cur[i - 1] = e.content.strip()
            out["ok"] = True
        except Exception as ex:
            out["error"] = repr(ex)
        results.append(out)
    return cur, results


def _render(bullets: list[str], indent: int) -> list[str]:
    pad = ' ' * indent
    if not bullets:
        return [f"{pad}1. 应该避免做..., 否则会导致..., 应该做...", f"{pad}2. ...", f"{pad}..."]
    return [f"{pad}{i}. {b}" for i, b in enumerate(bullets, 1)]


# tools ======================================================================


@tool
async def collate_long_memory(candidates: list[dict], k: int = 5) -> dict:
    """
    长期记忆冲突整理 + 直接落库一体化（仅作用于当前 thread_id 的记忆）。
    输入:
      - candidates: 待入库的 LongMemoryCandidate-like dict 列表
      - k: 每条候选召回的最近邻数量，默认 5
    流程: 1) 用 candidate.content 在**当前 thread_id** 的向量库召回 top-k 邻居;
         2) 喂决策 chain; 3) 按 decisions 调用 longMem.store/update/delete
         （insert 写入当前 thread_id）; 4) 返回执行结果。
    返回: {"decisions": [...], "results": [{candidate_index, action, target_id, ok, ...}, ...]}
    """
    if not candidates:
        return {"decisions": [], "results": []}

    tid = current_thread_id()
    contents = [c["content"] for c in candidates]
    existing = await longMem.search_neighbors(contents, k=k, thread_id=tid)

    batch: LongMemoryCurationBatch = await long_chain.ainvoke({
        "candidates": json.dumps(candidates, ensure_ascii=False),
        "existing": json.dumps(existing, ensure_ascii=False, default=str),
    })

    valid_ids = {r["id"] for r in existing}
    results: list[dict] = []
    for d in batch.decisions:
        out: dict = {"candidate_index": d.candidate_index, "action": d.action,
                     "target_id": d.target_id, "ok": False}
        try:
            if d.action == "skip":
                out["ok"] = True
            elif d.action == "insert":
                if d.content is None or d.memory_type is None or d.importance is None:
                    raise ValueError("insert requires content/memory_type/importance")
                cand = candidates[d.candidate_index] if 0 <= d.candidate_index < len(candidates) else {}
                row = {
                    "content": d.content,
                    "memory_type": d.memory_type,
                    "importance": d.importance,
                    "context": d.context if d.context is not None else cand.get("context", ""),
                    "tags": d.tags if d.tags is not None else cand.get("tags", []),
                    "timestamp": cand.get("timestamp") or datetime.now().isoformat(),
                }
                out["db_id"] = await longMem.store(row, thread_id=tid)
                out["ok"] = True
            elif d.action in ("update", "delete"):
                if d.target_id is None or d.target_id not in valid_ids:
                    raise ValueError(f"{d.action} requires target_id present in existing")
                if d.action == "update":
                    affected = await longMem.update(d.target_id, {
                        "content": d.content, "memory_type": d.memory_type,
                        "importance": d.importance, "context": d.context, "tags": d.tags,
                    })
                else:
                    affected = await longMem.delete(d.target_id)
                out["affected"] = affected
                out["ok"] = affected > 0
        except Exception as ex:
            out["error"] = repr(ex)
        results.append(out)

    return {"decisions": [d.model_dump() for d in batch.decisions], "results": results}


@tool
async def collate_tool_skill(skill_path: str, tool_name: str, messages: list[BaseMessage]) -> dict:
    """
    工具调用策略整理 + 直接写回 skill 文档一体化。
    输入:
      - skill_path: 目标 skill markdown 路径（项目相对或绝对，如 'Skills/terminal_skill.md'）
      - tool_name:  对应工具名（如 'terminal'）
      - messages:   langgraph 消息流（list[BaseMessage]），工具内部负责序列化为 transcript 文本
    流程: 1) 读 md, 定位 `## 探索经验` 后的 fenced code block, 解析现有 bullets;
         2) 喂决策 chain 拿 0~3 条 edits; 3) 按 edits 应用到 bullets 并写回文件。
    返回: {"path", "edits", "applied", "before", "after"}；文件不存在或段落缺失时返回 error。
    """
    p = Path(skill_path)
    path = p if p.is_absolute() else ROOT / p
    if not path.exists():
        return {"path": str(path), "error": "skill file not found"}

    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    loc = _find_block(lines)
    if loc is None:
        return {"path": str(path), "error": "experience block not found"}
    a, b, indent, bullets = loc

    batch: SkillCurationBatch = await skill_chain.ainvoke({
        "skill_path": skill_path,
        "tool_name": tool_name,
        "current_experiences": json.dumps(bullets, ensure_ascii=False),
        "transcript": get_buffer_string(messages),
    })

    if not batch.edits:
        return {"path": str(path), "edits": [], "applied": [], "before": bullets, "after": bullets}

    new_bullets, applied = _apply_edits(bullets, batch.edits)
    new_lines = lines[: a + 1] + _render(new_bullets, indent) + lines[b:]
    new_text = "\n".join(new_lines) + ("\n" if text.endswith("\n") else "")
    path.write_text(new_text, encoding="utf-8")

    return {
        "path": str(path),
        "edits": [e.model_dump() for e in batch.edits],
        "applied": applied,
        "before": bullets,
        "after": new_bullets,
    }


# checkpoint / cursor io ======================================================


_CURSOR_DDL = """
CREATE TABLE IF NOT EXISTS collation_cursor (
    thread_id      TEXT PRIMARY KEY,
    last_msg_count INTEGER NOT NULL DEFAULT 0,
    last_run_at    TEXT    NOT NULL
)
"""

def _cur_conn() -> sqlite3.Connection:
    CUR_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(CUR_DB))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(_CURSOR_DDL)
    return conn


def _load_cursor(tid: str) -> int:
    with closing(_cur_conn()) as c:
        row = c.execute("SELECT last_msg_count FROM collation_cursor WHERE thread_id=?", (tid,)).fetchone()
    return int(row[0]) if row else 0


def _save_cursor(tid: str, n: int) -> None:
    with closing(_cur_conn()) as c, c:
        c.execute(
            "INSERT INTO collation_cursor (thread_id, last_msg_count, last_run_at) VALUES (?,?,?) "
            "ON CONFLICT(thread_id) DO UPDATE SET "
            "last_msg_count=excluded.last_msg_count, last_run_at=excluded.last_run_at",
            (tid, int(n), datetime.now().isoformat(timespec="seconds")),
        )


async def read_ckpt_msgs(thread_id: str) -> list[BaseMessage]:
    """Read full message stream from langgraph checkpoint for a thread."""
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    CKPT_DB.parent.mkdir(parents=True, exist_ok=True)
    async with AsyncSqliteSaver.from_conn_string(str(CKPT_DB)) as saver:
        tup = await saver.aget_tuple({"configurable": {"thread_id": thread_id}})
    msgs = (getattr(tup, "checkpoint", None) or {}).get("channel_values", {}).get("messages") if tup else None
    return list(msgs) if isinstance(msgs, list) else []


def _used_tools(messages: list[BaseMessage]) -> set[str]:
    used: set[str] = set()
    for m in messages:
        for tc in getattr(m, "tool_calls", None) or []:
            name = tc.get("name") if isinstance(tc, dict) else getattr(tc, "name", None)
            if name:
                used.add(str(name))
    return used


# scheduler ==================================================================  


class CollationScheduler:
    def __init__(
        self, *, unsettled_threshold: int = UNSETTLED_THRESHOLD, max_parallel: int = MAX_PARALLEL,
        retry_count: int = RETRY_COUNT, long_memory_k: int = LONG_MEM_K,
    ) -> None:
        self.unsettled_threshold = int(unsettled_threshold)
        self._max_parallel = int(max_parallel)
        self._retries = max(int(retry_count), 0)
        self._k = int(long_memory_k)
        self._sem: Optional[asyncio.Semaphore] = None
        self._unsettled: dict[str, int] = {}
        self._tasks: dict[str, asyncio.Task] = {}

    def notify(self, tid: str, delta: int = 2) -> None:
        try:
            n = self._unsettled.get(tid, 0) + max(int(delta), 0)
            self._unsettled[tid] = n
            if n >= self.unsettled_threshold:
                self._kick(tid)
        except Exception:
            self._log(tid, route="notify", ok=False, error=traceback.format_exc())

    def shutdown(self) -> None:
        for t in self._tasks.values():
            if not t.done():
                t.cancel()

    def _kick(self, tid: str) -> None:
        t = self._tasks.get(tid)
        if t is not None and not t.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        if self._sem is None:
            self._sem = asyncio.Semaphore(self._max_parallel)
        self._tasks[tid] = loop.create_task(self._collate(tid))

    async def _collate(self, tid: str) -> None:
        assert self._sem is not None
        async with self._sem:
            try:
                msgs = await read_ckpt_msgs(tid)
                last = await asyncio.to_thread(_load_cursor, tid)
                new = msgs[last:]
                if not new:
                    self._unsettled[tid] = 0
                    return
                routes: tuple[tuple[str, Callable[[], Awaitable[Any]]], ...] = (
                    ("short",  lambda: self._do_short(tid, new, offset=last)),
                    ("long",   lambda: self._do_long(tid, new)),
                    ("skills", lambda: self._do_skills(tid, new)),
                )
                await asyncio.gather(*(self._run(tid, n, f) for n, f in routes))
                await asyncio.to_thread(_save_cursor, tid, len(msgs))
                self._unsettled[tid] = 0
                self._log(tid, route="collate", ok=True, new_messages=len(new))
            except Exception:
                self._log(tid, route="collate", ok=False, error=traceback.format_exc())

    async def _run(self, tid: str, name: str, factory: Callable[[], Awaitable[Any]]) -> None:
        for attempt in range(1, self._retries + 2):
            try:
                await factory()
            except BaseException:
                final = attempt > self._retries
                self._log(tid, route=name, ok=False, error=traceback.format_exc(),
                          attempt=attempt, retrying=not final)
                if final:
                    return
                continue
            self._log(tid, route=name, ok=True, attempt=attempt)
            return

    # ---- routes ----

    async def _do_short(self, tid: str, new: list[BaseMessage], *, offset: int) -> None:
        from Agents.remember import short_memory_chain
        from Memory import shortMem

        entry = await short_memory_chain.ainvoke({"transcript": get_buffer_string(new)})
        payload: dict[str, Any] = entry.model_dump()
        # chain 里 turn_range 是相对增量（1-based），外层修正为全局位置
        tr = payload.get("turn_range") or [1, len(new)]
        try:
            ls, le = int(tr[0]), int(tr[1])
        except (TypeError, ValueError, IndexError):
            ls, le = 1, len(new)
        payload["turn_range"] = (offset + ls, offset + le)
        payload.setdefault("timestamp", datetime.now().isoformat())
        await shortMem.store(payload, thread_id=tid)

    async def _do_long(self, tid: str, new: list[BaseMessage]) -> None:
        from Agents.remember import long_memory_chain

        batch = await long_memory_chain.ainvoke({"transcript": get_buffer_string(new)})
        entries = getattr(batch, "long_memories", None) or []
        if not entries:
            return
        candidates: list[dict] = []
        for e in entries:
            d = e.model_dump() if hasattr(e, "model_dump") else dict(e)
            d.setdefault("timestamp", datetime.now().isoformat())
            candidates.append(d)
        await collate_long_memory.ainvoke(
            {"candidates": candidates, "k": self._k},
            config={"configurable": {"thread_id": tid}},
        )

    async def _do_skills(self, tid: str, new: list[BaseMessage]) -> None:
        from Tools.skills import index as skill_index

        targets: list[tuple[str, Path]] = []
        for name in _used_tools(new):
            p = (skill_index.get(name) or {}).get("path")
            if p and Path(p).exists():
                targets.append((name, Path(p)))
        if not targets:
            return

        errors: list[BaseException] = []
        for name, path in targets:
            rel = path.relative_to(ROOT) if path.is_relative_to(ROOT) else path
            try:
                await collate_tool_skill.ainvoke(
                    {"skill_path": str(rel), "tool_name": name, "messages": new},
                    config={"configurable": {"thread_id": tid}},
                )
            except BaseException as e:
                errors.append(e)
        if errors:
            raise RuntimeError("skill curation errors: " + "; ".join(repr(e) for e in errors))

    @staticmethod
    def _log(tid: str, *, route: str, ok: bool, error: Optional[str] = None, **extra: Any) -> None:
        try:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            safe = re.sub(r'[^\w\-]', '_', tid)
            rec: dict[str, Any] = {
                "ts": datetime.now().isoformat(timespec="seconds"),
                "thread_id": tid, "route": route, "ok": bool(ok),
            }
            if error:
                rec["error"] = error
            rec.update({k: v for k, v in extra.items() if v is not None})
            (LOG_DIR / f"{safe}.jsonl").open("a", encoding="utf-8").write(
                json.dumps(rec, ensure_ascii=False, default=str) + "\n"
            )
        except Exception:
            pass


scheduler = CollationScheduler()
