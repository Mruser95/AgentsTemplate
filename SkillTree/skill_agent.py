from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Literal, Optional

from dotenv import load_dotenv
from langchain_core.messages import BaseMessage, get_buffer_string
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from toolagent_prompt import SKILL_CURATOR_PROMPT  # noqa: E402

load_dotenv()
llm = ChatOpenAI(
    model=os.getenv("agent_llm_model"),
    api_key=os.getenv("agent_llm_key"),
    base_url=os.getenv("agent_llm_base_url"),
)


# schemas ====================================================================


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


_skill_chain = (
    ChatPromptTemplate.from_messages([
        ("system", SKILL_CURATOR_PROMPT),
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


# markdown helpers ===========================================================


_HEAD_RE = re.compile(r'^\s*##\s+探索经验\s*$')
_FENCE_RE = re.compile(r'^\s*```')
_BULLET_RE = re.compile(r'^(\s*)(\d+)\.\s+(.*)$')
_HOLDER_RE = re.compile(r'^\s*(?:\d+\.\s+)?\.{3,}\s*$')


def _find_block(lines: list[str]) -> Optional[tuple[int, int, int, list[str]]]:
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
            if e.action != "add" and (i is None or not 1 <= i <= len(cur)):
                raise ValueError(f"target_index {i} out of range 1..{len(cur)}")
            if e.action != "remove" and not e.content:
                raise ValueError(f"{e.action} requires content")
            if e.action == "add":
                cur.append(e.content.strip())
            elif e.action == "remove":
                cur.pop(i - 1)
            else:
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


# tool =======================================================================


@tool
async def collate_tool_skill(skill_path: str, tool_name: str, messages: list[BaseMessage]) -> dict:
    """
    工具调用策略整理 + 直接写回 skill 文档一体化。
    流程: 1) 读 md, 定位 `## 探索经验` 后的 fenced code block, 解析现有 bullets;
         2) 喂决策 chain 拿 0~3 条 edits; 3) 按 edits 应用并写回。
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

    batch: SkillCurationBatch = await _skill_chain.ainvoke({
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


# route ======================================================================


def _used_tools(messages: list[BaseMessage]) -> set[str]:
    used: set[str] = set()
    for m in messages:
        for tc in getattr(m, "tool_calls", None) or []:
            name = tc.get("name") if isinstance(tc, dict) else getattr(tc, "name", None)
            if name:
                used.add(str(name))
    return used


async def route_skills(tid: str, new: list[BaseMessage], *, offset: int = 0, k: int = 5) -> None:
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
