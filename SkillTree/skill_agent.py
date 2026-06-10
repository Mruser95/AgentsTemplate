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

from toolagent_prompt import SKILL_IMPROVE_PROMPT, SKILL_TREE_PROMPT  # noqa: E402

load_dotenv()
llm = ChatOpenAI(
    model=os.getenv("agent_llm_model"),
    api_key=os.getenv("agent_llm_key"),
    base_url=os.getenv("agent_llm_base_url"),
)


# skill tree (from project notes) ============================================


TREE_DIR = Path(__file__).resolve().parent
PROJECT_NOTES = ROOT / "Memory" / "projectKnow.md"
_SLUG_RE = re.compile(r'[^a-zA-Z0-9_\-]+')


def _slug(s: str) -> str:
    return _SLUG_RE.sub("_", (s or "").strip()).strip("_") or "misc"


def _scan_tree() -> dict[str, str]:
    """{'<category>/<name>': '<frontmatter description / 使用场景>'}，供 curator 去重判断。"""
    from Tools.skills import _parse_frontmatter

    out: dict[str, str] = {}
    if not TREE_DIR.exists():
        return out
    for md in TREE_DIR.glob("*/*.md"):
        key = f"{md.parent.name}/{md.stem}"
        try:
            meta, body = _parse_frontmatter(md.read_text(encoding="utf-8"))
        except Exception:
            continue
        out[key] = str(meta.get("description") or "").strip() or body.strip()[:400]
    return out


class SkillTreeEdit(BaseModel):
    action: Literal["insert", "update", "skip"] = Field(
        description="insert = create new skill md; update = overwrite existing; skip = drop."
    )
    category: str = Field(description="Category slug (folder under SkillTree/).")
    name: str = Field(description="Skill slug (file stem, no extension).")
    content: Optional[str] = Field(
        default=None,
        description=(
            "Full markdown for the file. Required for insert/update; ignored for skip. "
            "MUST start with a YAML frontmatter block (`name` + `description`, where "
            "`description` is a one-paragraph usage scenario telling when to consult "
            "the skill), then the how-to body."
        ),
    )
    target_key: Optional[str] = Field(
        default=None,
        description="Existing '<category>/<name>' key. Required for update; must be None for insert/skip.",
    )
    reason: str = Field(description="Short justification grounded in the project notes.")


class SkillTreeBatch(BaseModel):
    edits: list[SkillTreeEdit] = Field(
        default_factory=list,
        description="0 to 3 edits. Empty list is a legitimate answer.",
    )


_tree_chain = (
    ChatPromptTemplate.from_messages([
        ("system", SKILL_TREE_PROMPT),
        ("human", "notes:\n{notes}\n\nexisting_tree:\n{existing_tree}"),
    ])
    | llm.with_structured_output(SkillTreeBatch)
)


def _apply_tree_edit(e: SkillTreeEdit, existing: dict[str, str]) -> dict:
    out: dict = {"action": e.action, "category": e.category, "name": e.name,
                 "target_key": e.target_key, "ok": False}
    try:
        if e.action == "skip":
            out["ok"] = True
            return out
        if not e.content:
            raise ValueError(f"{e.action} requires content")
        cat, name = _slug(e.category), _slug(e.name)
        path = TREE_DIR / cat / f"{name}.md"
        if e.action == "update":
            if not e.target_key or e.target_key not in existing:
                raise ValueError(f"update requires target_key present in existing_tree")
        path.parent.mkdir(parents=True, exist_ok=True)
        text = e.content if e.content.endswith("\n") else e.content + "\n"
        path.write_text(text, encoding="utf-8")
        out["path"] = str(path.relative_to(ROOT))
        out["ok"] = True
    except Exception as ex:
        out["error"] = repr(ex)
    return out


@tool
async def collate_skill_tree() -> dict:
    """
    从 Memory/projectKnow.md 抽取可复用技能并写入 SkillTree/<category>/<name>.md。
    已存在则按 LLM 决策 update / 保持不变；新技能自动建分类目录。
    """
    if not PROJECT_NOTES.exists():
        return {"error": "projectKnow.md not found", "edits": []}
    notes = PROJECT_NOTES.read_text(encoding="utf-8").strip()
    if not notes:
        return {"edits": [], "applied": []}
    existing = _scan_tree()
    batch: SkillTreeBatch = await _tree_chain.ainvoke({
        "notes": notes,
        "existing_tree": json.dumps(existing, ensure_ascii=False),
    })
    applied = [_apply_tree_edit(e, existing) for e in batch.edits]
    return {"edits": [e.model_dump() for e in batch.edits], "applied": applied}


async def route_skill_tree(tid: str, new: list[BaseMessage], *, offset: int = 0, k: int = 5) -> None:
    """调度入口：从 projectKnow.md 提炼技能树。与消息内容无关，仅在调度节拍触发。"""
    await collate_skill_tree.ainvoke({})


# skill improve (from run transcript) ========================================


_improve_chain = (
    ChatPromptTemplate.from_messages([
        ("system", SKILL_IMPROVE_PROMPT),
        ("human", "transcript:\n{transcript}\n\nexisting_tree:\n{existing_tree}"),
    ])
    | llm.with_structured_output(SkillTreeBatch)
)


@tool
async def improve_skill_tree(messages: list[BaseMessage]) -> dict:
    """
    从最近一次运行的 transcript 提炼教训，改进 SkillTree 中已有技能
    （update 为主，确有新技能才 insert）。
    """
    existing = _scan_tree()
    batch: SkillTreeBatch = await _improve_chain.ainvoke({
        "transcript": get_buffer_string(messages),
        "existing_tree": json.dumps(existing, ensure_ascii=False),
    })
    applied = [_apply_tree_edit(e, existing) for e in batch.edits]
    return {"edits": [e.model_dump() for e in batch.edits], "applied": applied}


async def route_skills(tid: str, new: list[BaseMessage], *, offset: int = 0, k: int = 5) -> None:
    """调度入口：用本批次 transcript 改进 SkillTree 中的技能。"""
    await improve_skill_tree.ainvoke({"messages": new})

