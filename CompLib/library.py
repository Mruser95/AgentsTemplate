from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Type

from pydantic import BaseModel, Field, PrivateAttr
from langchain_core.tools import BaseTool

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from Tools.skills import _parse_frontmatter  # noqa: E402

COMPLIB_DIR = Path(__file__).resolve().parent


def _scan_components() -> dict[str, dict]:
    """扫描 CompLib/*/SKILL.md，返回 {目录名: {name, description, path}}。运行时实时重扫。"""
    out: dict[str, dict] = {}
    for skill in sorted(COMPLIB_DIR.glob("*/SKILL.md")):
        try:
            meta, body = _parse_frontmatter(skill.read_text(encoding="utf-8"))
        except Exception:
            continue
        desc = str(meta.get("description") or "").strip()
        if not desc:
            desc = next((ln.strip() for ln in body.splitlines() if ln.strip()), "")
        out[skill.parent.name] = {
            "name": str(meta.get("name") or skill.parent.name).strip(),
            "description": desc,
            "path": skill,
        }
    return out


class ComponentLibraryInput(BaseModel):
    component: str = Field(
        description="组件目录名，或 'list' 列出全部组件及其一句话用途概述。"
    )


class ComponentLibrary(BaseTool):
    name: str = "component_library"
    description: str = (
        "检索 CompLib/ 下的通用可复用组件（每个组件 = 一个类/函数实现 + 一份接口规范 SKILL.md）。"
        "先用 component='list' 看有哪些组件及其一句话用途；再用确切组件名拉取完整用法与接口规范，"
        "据此 import 复用或组合搭建领域 agent。不要预取用不到的组件。"
    )
    args_schema: Type[BaseModel] = ComponentLibraryInput
    _seen: set[str] = PrivateAttr(default_factory=set)

    def _list(self) -> str:
        comps = _scan_components()
        if not comps:
            return "CompLib 暂无组件。"
        return "\n".join(f"- {k}: {v['description']}" for k, v in comps.items())

    def _run(self, component: str) -> str:
        if component == "list":
            return self._list()
        if component in self._seen:
            return f"[component_library cache] '{component}' 本任务内已发送过，请翻看上文消息，不再重复返回。"
        info = _scan_components().get(component)
        if not info:
            avail = ", ".join(_scan_components()) or "（空）"
            return f"Component not found: '{component}'. Available: {avail}"
        _, body = _parse_frontmatter(info["path"].read_text(encoding="utf-8"))
        self._seen.add(component)
        return body

    async def _arun(self, component: str) -> str:
        return await asyncio.to_thread(self._run, component)  # 目录扫描/读盘不阻塞 event loop


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else "list"
    print(ComponentLibrary()._run(arg))
