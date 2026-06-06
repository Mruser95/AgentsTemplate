import re
from pathlib import Path
from typing import Type
import yaml
from pydantic import BaseModel, Field, PrivateAttr
from langchain_core.tools import BaseTool

SKILLS_DIR = Path(__file__).parents[1] / "Skills"
SKILL_TREE_DIR = Path(__file__).parents[1] / "SkillTree"


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    m = re.match(r'^---\s*\n(.*?)\n---\s*\n', text, re.DOTALL)
    if not m:
        return {}, text
    return yaml.safe_load(m.group(1)) or {}, text[m.end():]


def _scan_skills() -> dict[str, dict]:
    index = {}
    for p in SKILLS_DIR.glob("*_skill.md"):
        meta, _ = _parse_frontmatter(p.read_text(encoding="utf-8"))
        raw = meta.get("tool", p.stem.removesuffix("_skill"))
        names = [t.strip() for t in str(raw).split(",") if t.strip()] or [p.stem.removesuffix("_skill")]
        for name in names:
            index[name] = {"description": meta.get("description", ""), "path": p}
    return index


index = _scan_skills()


class Input(BaseModel):
    tool_name: str = Field(
        description="The name of the tool to load the skill document for, or 'list' to list all available skills"
    )


class SkillLibrary(BaseTool):
    name: str = "skill_library"
    description: str = (
        "Load tool usage skill document. First call with tool_name='list' to see "
        "available skills and their one-line descriptions; then call with the exact "
        "tool name right before you actually use that tool to fetch its full doc. "
        "Do not prefetch docs for tools you are not about to use."
        if index else "Load tool usage skill document (no available skills currently)."
    )
    args_schema: Type[BaseModel] = Input
    _seen: set[str] = PrivateAttr(default_factory=set)

    def _list(self) -> str:
        if not index:
            return "No available skill documents."
        return "\n".join(f"- {k}: {v['description']}" for k, v in index.items())

    def _run(self, tool_name: str) -> str:
        if tool_name in self._seen:
            return (
                f"[skill_library cache] '{tool_name}' 文档本任务内已发送过，"
                "请翻看上文消息，不再重复返回内容。"
            )

        if tool_name == "list":
            self._seen.add(tool_name)
            return self._list()

        info = index.get(tool_name)
        if not info:
            # 未命中不计入 seen，避免拼错被永久卡住
            return f"Skill document not found for '{tool_name}'. Available: {', '.join(index)}"

        _, body = _parse_frontmatter(info["path"].read_text(encoding="utf-8"))
        self._seen.add(tool_name)
        return body

    async def _arun(self, tool_name: str) -> str:
        return self._run(tool_name)


def _scan_skill_tree() -> dict[str, dict]:
    """实时扫描 SkillTree/<category>/<name>.md（COLLATOR 运行时持续沉淀，故每次重扫）。"""
    out: dict[str, dict] = {}
    if not SKILL_TREE_DIR.exists():
        return out
    for p in sorted(SKILL_TREE_DIR.glob("*/*.md")):
        try:
            meta, body = _parse_frontmatter(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        desc = str(meta.get("description") or "").strip()
        if not desc:
            desc = next((ln.strip() for ln in body.splitlines() if ln.strip()), "")
        out[f"{p.parent.name}/{p.stem}"] = {"description": desc, "path": p}
    return out


class SkillTreeInput(BaseModel):
    skill_key: str = Field(
        description=(
            "The '<category>/<name>' key of the project skill to load, or 'list' to "
            "list all distilled skills with their usage-scenario descriptions."
        )
    )


class SkillTreeLibrary(BaseTool):
    name: str = "skill_tree"
    description: str = (
        "查阅 COLLATOR 从本项目历史中沉淀的可复用技能（带「使用场景」描述的 how-to）。"
        "先用 skill_key='list' 看有哪些技能及其触发场景；遇到与某场景相似的任务时，"
        "再用确切的 '<category>/<name>' 键拉取完整步骤/坑/知识。按需查阅，不要预取用不到的技能。"
    )
    args_schema: Type[BaseModel] = SkillTreeInput
    _seen: set[str] = PrivateAttr(default_factory=set)

    def _list(self) -> str:
        tree = _scan_skill_tree()
        if not tree:
            return "技能树暂为空（COLLATOR 会在项目推进中自动沉淀技能）。"
        return "\n".join(f"- {k}: {v['description']}" for k, v in tree.items())

    def _run(self, skill_key: str) -> str:
        if skill_key == "list":
            return self._list()
        if skill_key in self._seen:
            return (
                f"[skill_tree cache] '{skill_key}' 本任务内已发送过，请翻看上文消息，不再重复返回。"
            )
        info = _scan_skill_tree().get(skill_key)
        if not info:
            avail = ", ".join(_scan_skill_tree()) or "（空）"
            return f"Skill not found for '{skill_key}'. Available: {avail}"
        _, body = _parse_frontmatter(info["path"].read_text(encoding="utf-8"))
        self._seen.add(skill_key)
        return body

    async def _arun(self, skill_key: str) -> str:
        return self._run(skill_key)
