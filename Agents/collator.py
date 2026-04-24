from langchain_core.messages import BaseMessage, get_buffer_string
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI
from typing import Literal, Optional
from pydantic import BaseModel, Field
from datetime import datetime
from dotenv import load_dotenv
from pathlib import Path
import json
import os
import re
import sys

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
from Memory import longMem  # noqa: E402
from Tools._context import current_thread_id  # noqa: E402

load_dotenv()

llm = ChatOpenAI(
    model=os.getenv("agent_llm_model"),
    api_key=os.getenv("agent_llm_key"),
    base_url=os.getenv("agent_llm_base_url"),
)


# schemas =====================================================================


MemoryType = Literal[
    "fact",
    "event",
    "preference",
    "emotion",
    "skill",
    "relationship",
    "knowledge",
]


class LongMemoryRecord(BaseModel):
    id: int = Field(description="Primary key in the long_memory table")
    content: str = Field(description="Stored memory content")
    memory_type: MemoryType = Field(description="Stored memory type")
    importance: int = Field(ge=1, le=5, description="Stored importance 1-5")
    context: str = Field(default="", description="Stored context / background")
    tags: list[str] = Field(default_factory=list, description="Stored tags")
    timestamp: str = Field(description="Stored ISO timestamp")
    similarity: Optional[float] = Field(
        default=None,
        description="Optional semantic similarity score against the candidate (0-1, higher = more similar)",
    )


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


# prompts =====================================================================


long_memory_collator_prompt = """\
You are a Long-Memory Curator for a sqlite vector store.

You receive:
  - candidates: a JSON list of LongMemoryCandidate items just extracted from a
    fresh transcript and not yet stored.
  - existing:   a JSON list of LongMemoryRecord items already in the DB that
    were retrieved as the top semantic neighbours of those candidates.
              Each record has an `id` (DB primary key) and may carry a
              `similarity` score in [0, 1] against the closest candidate.

Your job: emit EXACTLY ONE LongMemoryDecision per candidate (so
`len(decisions) == len(candidates)`), choosing how the DB should change.

Return a single JSON object matching LongMemoryCurationBatch. No prose, no
markdown fences, no extra keys.

────────────────────────────────────────
Action semantics
────────────────────────────────────────
- insert : the candidate is genuinely new information. No existing record
           covers it. `target_id` MUST be null.
- update : an existing record covers the same fact but the candidate refines,
           corrects, or supersedes it. Set `target_id` to that record's id and
           provide the FULL new `content` / `memory_type` / `importance` (and
           optionally `context` / `tags`). The new content may be a merged
           rewrite that preserves still-valid pieces of the old row.
- skip   : the candidate is already fully captured by an existing record, OR
           the candidate is too low-quality (importance 1, vague, transient).
           DB stays unchanged. `target_id` MUST be null.
- delete : an existing record is now demonstrably wrong, obsolete, or
           contradicted by the candidate, AND the candidate itself is not worth
           keeping (otherwise prefer `update`). Set `target_id` to that
           record's id; `content` / `memory_type` / `importance` are ignored.

────────────────────────────────────────
Conflict resolution policy
────────────────────────────────────────
Decide by weighing, in order:
  1. memory_type compatibility
       - `fact` / `preference` / `emotion` / `relationship` / `skill` about the
         user are SINGLE-VALUED per subject: a newer candidate of the same
         type that contradicts an existing row should `update` it, not insert
         a duplicate.
       - `event` rows are append-only by nature: prefer `insert` even if
         similar, unless the candidate is literally the same event restated.
       - `knowledge` rows can coexist if they cover different facets; only
         `update` when the candidate strictly supersedes the old lesson.
  2. importance
       - If both rows describe the same thing, keep / promote to the HIGHER
         importance. Do not silently downgrade a 5 to a 3.
  3. timestamp
       - When type and topic match, the more recent observation wins. Use
         this as the tiebreaker, not as the primary signal.
  4. similarity
       - similarity >= 0.85 with matching memory_type ⇒ strong duplicate
         signal, prefer `update` or `skip` over `insert`.
       - similarity in [0.6, 0.85) ⇒ probably related but distinct, usually
         `insert` unless the candidate clearly subsumes the existing one.
       - similarity < 0.6 ⇒ treat as unrelated; default to `insert`.

────────────────────────────────────────
Discipline
────────────────────────────────────────
- Never invent fields not implied by the inputs. If unsure, prefer `skip`.
- Do not emit two decisions for the same candidate.
- Do not touch any existing record id that does not appear in `existing`.
- `reason` must be one short sentence citing the concrete signal used
  (e.g. "same preference, newer timestamp, importance preserved at 4").
- Output must be a single valid JSON object, nothing else.
"""


skill_collator_prompt = """\
You are a Tool-Strategy Curator. You read a langgraph message stream of a
recent agent run (system / human / ai / tool messages, including tool calls
and their observations) and decide whether the run reveals any reusable
lesson worth recording into the target skill markdown's "探索经验" section.

You receive:
  - skill_path:           the markdown file these lessons belong to,
                          e.g. "Skills/terminal_skill.md".
  - tool_name:            the tool the skill document covers, e.g. "terminal".
  - current_experiences:  the existing bullets of the "探索经验" list, as a
                          JSON array of strings, in their current display
                          order (index 1 = first bullet).
  - transcript:           the langgraph messages, already serialized to text.

Return a single JSON object matching SkillCurationBatch. No prose, no
markdown fences, no extra keys.

────────────────────────────────────────
What counts as a lesson
────────────────────────────────────────
A bullet should encode a TRANSFERABLE rule for FUTURE runs of the same tool,
not a recap of what just happened. It must satisfy ALL of:
  (a) Grounded in an observed pattern in the transcript (a failure that
      repeated, a denial, a timeout, a workflow that clearly worked).
  (b) Actionable: a future agent can read it and change behaviour.
  (c) Not already covered by `current_experiences` (paraphrases count as
      covered).

Bullet format: one sentence, mirroring the existing style, e.g.
  "应该避免做 X, 否则会导致 Y, 应该做 Z"
Match the language of the surrounding doc (Chinese stays Chinese).

────────────────────────────────────────
Edit budget — be conservative
────────────────────────────────────────
- Emit AT MOST 3 edits per call. Fewer is better.
- Empty `edits: []` is the correct answer for routine runs with no new
  insight (most runs).
- Prefer `update` / `replace` over `add` when an existing bullet is close
  but outdated or imprecise; this avoids list bloat.
- Use `remove` only when an existing bullet is now wrong or contradicted by
  observed evidence.
- Never reorder bullets; only the operations above.

────────────────────────────────────────
Field rules
────────────────────────────────────────
- skill_path:    echo the input value verbatim.
- action=add:        target_index MUST be null; content REQUIRED.
- action=update:     target_index REQUIRED (1-based, must exist in
                     current_experiences); content REQUIRED.
- action=replace:    same field rules as update; use this when the new
                     bullet semantically overwrites an outdated lesson.
- action=remove:     target_index REQUIRED; content MUST be null.
- reason:        one short sentence pointing at the transcript evidence
                 (e.g. "tool call denied 3x with same pipe pattern").

────────────────────────────────────────
Discipline
────────────────────────────────────────
- Do not invent failures or successes that are not in the transcript.
- Do not summarize the run, do not narrate the agent's reasoning.
- Do not propose edits to other sections of the markdown — only the
  "探索经验" list.
- Output must be a single valid JSON object, nothing else.
"""


# chains ======================================================================


long_memory_collator_chain = (
    ChatPromptTemplate.from_messages([
        ("system", long_memory_collator_prompt),
        (
            "human",
            "candidates:\n{candidates}\n\nexisting:\n{existing}",
        ),
    ])
    | llm.with_structured_output(LongMemoryCurationBatch)
)

skill_collator_chain = (
    ChatPromptTemplate.from_messages([
        ("system", skill_collator_prompt),
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


# tools =======================================================================


_EXP_HEADING_RE = re.compile(r'^\s*##\s+探索经验\s*$')
_FENCE_RE = re.compile(r'^\s*```')
_BULLET_RE = re.compile(r'^(\s*)(\d+)\.\s+(.*)$')
_PLACEHOLDER_RE = re.compile(r'^\s*(?:\d+\.\s+)?\.{3,}\s*$')


def _resolve_skill_path(skill_path: str) -> Path:
    p = Path(skill_path)
    return p if p.is_absolute() else PROJECT_ROOT / p


def _locate_experience_block(lines: list[str]) -> Optional[tuple[int, int, int]]:
    heading = next((i for i, ln in enumerate(lines) if _EXP_HEADING_RE.match(ln)), None)
    if heading is None:
        return None
    fence_open = next(
        (j for j in range(heading + 1, len(lines)) if _FENCE_RE.match(lines[j])),
        None,
    )
    if fence_open is None:
        return None
    fence_close = next(
        (j for j in range(fence_open + 1, len(lines)) if _FENCE_RE.match(lines[j])),
        None,
    )
    if fence_close is None:
        return None
    indent_len = 0
    for j in range(fence_open + 1, fence_close):
        if _PLACEHOLDER_RE.match(lines[j]):
            continue
        m = _BULLET_RE.match(lines[j])
        if m:
            indent_len = len(m.group(1))
            break
    return (fence_open, fence_close, indent_len)


def _extract_bullets(lines: list[str], fence_open: int, fence_close: int) -> list[str]:
    bullets: list[str] = []
    for j in range(fence_open + 1, fence_close):
        if _PLACEHOLDER_RE.match(lines[j]):
            continue
        m = _BULLET_RE.match(lines[j])
        if m:
            bullets.append(m.group(3).rstrip())
    return bullets


def _apply_skill_edits(bullets: list[str], edits: list[SkillExperienceEdit]) -> tuple[list[str], list[dict]]:
    cur = list(bullets)
    results: list[dict] = []
    for e in edits:
        out: dict = {"action": e.action, "target_index": e.target_index, "ok": False}
        try:
            if e.action == "add":
                if not e.content:
                    raise ValueError("add requires content")
                cur.append(e.content.strip())
            elif e.action in ("update", "replace"):
                if e.target_index is None or not (1 <= e.target_index <= len(cur)):
                    raise ValueError(f"target_index {e.target_index} out of range 1..{len(cur)}")
                if not e.content:
                    raise ValueError(f"{e.action} requires content")
                cur[e.target_index - 1] = e.content.strip()
            elif e.action == "remove":
                if e.target_index is None or not (1 <= e.target_index <= len(cur)):
                    raise ValueError(f"target_index {e.target_index} out of range 1..{len(cur)}")
                cur.pop(e.target_index - 1)
            out["ok"] = True
        except Exception as ex:
            out["error"] = repr(ex)
        results.append(out)
    return cur, results


def _render_block(bullets: list[str], indent: int) -> list[str]:
    pad = ' ' * indent
    if not bullets:
        return [
            f"{pad}1. 应该避免做..., 否则会导致..., 应该做...",
            f"{pad}2. ...",
            f"{pad}...",
        ]
    return [f"{pad}{i}. {b}" for i, b in enumerate(bullets, 1)]


def _existing_view(neighbors: list[dict]) -> list[dict]:
    return [
        {
            "id": r["id"],
            "content": r["content"],
            "memory_type": r["memory_type"],
            "importance": r["importance"],
            "context": r.get("context", ""),
            "tags": r.get("tags", []),
            "timestamp": r.get("timestamp", ""),
            "similarity": r.get("similarity"),
        }
        for r in neighbors
    ]


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

    thread_id = current_thread_id()
    contents = [c["content"] for c in candidates]
    neighbors = await longMem.search_neighbors(contents, k=k, thread_id=thread_id)
    existing = _existing_view(neighbors)

    batch: LongMemoryCurationBatch = await long_memory_collator_chain.ainvoke({
        "candidates": json.dumps(candidates, ensure_ascii=False),
        "existing": json.dumps(existing, ensure_ascii=False),
    })

    valid_ids = {r["id"] for r in existing}
    results: list[dict] = []
    for d in batch.decisions:
        out: dict = {
            "candidate_index": d.candidate_index,
            "action": d.action,
            "target_id": d.target_id,
            "ok": False,
        }
        try:
            if d.action == "skip":
                out["ok"] = True
            elif d.action == "insert":
                if d.content is None or d.memory_type is None or d.importance is None:
                    raise ValueError("insert requires content/memory_type/importance")
                cand = (
                    candidates[d.candidate_index]
                    if 0 <= d.candidate_index < len(candidates) else {}
                )
                row = {
                    "content": d.content,
                    "memory_type": d.memory_type,
                    "importance": d.importance,
                    "context": d.context if d.context is not None else cand.get("context", ""),
                    "tags": d.tags if d.tags is not None else cand.get("tags", []),
                    "timestamp": cand.get("timestamp") or datetime.now().isoformat(),
                }
                out["db_id"] = await longMem.store(row, thread_id=thread_id)
                out["ok"] = True
            elif d.action == "update":
                if d.target_id is None or d.target_id not in valid_ids:
                    raise ValueError("update requires target_id present in existing")
                fields = {
                    "content": d.content,
                    "memory_type": d.memory_type,
                    "importance": d.importance,
                    "context": d.context,
                    "tags": d.tags,
                }
                affected = await longMem.update(d.target_id, fields)
                out["affected"] = affected
                out["ok"] = affected > 0
            elif d.action == "delete":
                if d.target_id is None or d.target_id not in valid_ids:
                    raise ValueError("delete requires target_id present in existing")
                affected = await longMem.delete(d.target_id)
                out["affected"] = affected
                out["ok"] = affected > 0
        except Exception as ex:
            out["error"] = repr(ex)
        results.append(out)

    return {
        "decisions": [d.model_dump() for d in batch.decisions],
        "results": results,
    }


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
    path = _resolve_skill_path(skill_path)
    if not path.exists():
        return {"path": str(path), "error": "skill file not found"}

    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    loc = _locate_experience_block(lines)
    if loc is None:
        return {"path": str(path), "error": "experience block not found"}
    fence_open, fence_close, indent_len = loc
    bullets = _extract_bullets(lines, fence_open, fence_close)

    batch: SkillCurationBatch = await skill_collator_chain.ainvoke({
        "skill_path": skill_path,
        "tool_name": tool_name,
        "current_experiences": json.dumps(bullets, ensure_ascii=False),
        "transcript": get_buffer_string(messages),
    })

    if not batch.edits:
        return {
            "path": str(path),
            "edits": [],
            "applied": [],
            "before": bullets,
            "after": bullets,
        }

    new_bullets, applied = _apply_skill_edits(bullets, batch.edits)
    new_block = _render_block(new_bullets, indent_len)
    new_lines = lines[: fence_open + 1] + new_block + lines[fence_close:]
    new_text = "\n".join(new_lines)
    if text.endswith("\n"):
        new_text += "\n"
    path.write_text(new_text, encoding="utf-8")

    return {
        "path": str(path),
        "edits": [e.model_dump() for e in batch.edits],
        "applied": applied,
        "before": bullets,
        "after": new_bullets,
    }
