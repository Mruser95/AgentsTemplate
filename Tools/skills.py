import re
from pathlib import Path
from typing import Type
import yaml
from pydantic import BaseModel, Field
from langchain_core.tools import BaseTool

SKILLS_DIR = Path(__file__).parents[1] / "Skills"


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    m = re.match(r'^---\s*\n(.*?)\n---\s*\n', text, re.DOTALL)
    if not m:
        return {}, text
    return yaml.safe_load(m.group(1)) or {}, text[m.end():]


def _scan_skills() -> dict[str, dict]:
    index = {}
    for p in SKILLS_DIR.glob("*_skill.md"):
        meta, _ = _parse_frontmatter(p.read_text(encoding="utf-8"))
        tool = meta.get("tool", p.stem.removesuffix("_skill"))
        index[tool] = {"description": meta.get("description", ""), "path": p}
    return index


index = _scan_skills()
summary = ", ".join(f"{k} — {v['description']}" for k, v in index.items())


class Input(BaseModel):
    tool_name: str = Field(
        description="The name of the tool to load the skill document for, or 'list' to list all available skills"
    )


class SkillLibrary(BaseTool):
    name: str = "skill_library"
    description: str = (
        f"""Load tool usage skill document. Call this tool before using unfamiliar tools to get usage specifications. 
            Currently available: {summary}."""
        if summary else "Load tool usage skill document (no available skills currently)."
    )
    args_schema: Type[BaseModel] = Input

    def _run(self, tool_name: str) -> str:
        if tool_name == "list":
            if not index:
                return "No available skill documents."
            return "\n".join(f"- {k}: {v['description']}" for k, v in index.items())

        info = index.get(tool_name)
        if not info:
            return f"Skill document not found for '{tool_name}'. Available: {', '.join(index)}"

        _, body = _parse_frontmatter(info["path"].read_text(encoding="utf-8"))
        return body

    async def _arun(self, tool_name: str) -> str:
        return self._run(tool_name)
