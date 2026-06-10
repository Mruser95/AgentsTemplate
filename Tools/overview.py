from __future__ import annotations

import ast
import asyncio
import fnmatch
import re
import sys
from pathlib import Path
from typing import Callable, Type

import yaml
from pydantic import BaseModel, Field, PrivateAttr
from langchain_core.tools import BaseTool

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from Tools.utils import bump_budget, current_thread_id, ensure_workspace, is_inside  # noqa: E402

with open(PROJECT_ROOT / "config.yaml", "r", encoding="utf-8") as _f:
    _cfg = yaml.safe_load(_f) or {}

# repo_map / grep / glob 可调参数（来源：config.yaml）
RM_LIMIT: int = int(_cfg.get("repo_map_count_limit", 20))
RM_TOP_N: int = int(_cfg.get("repo_map_top_n", 20))
RM_MAX_SYMS: int = int(_cfg.get("repo_map_max_symbols", 25))
RM_MAX_LISTED: int = int(_cfg.get("repo_map_max_listed", 80))
RM_MAX_CHARS: int = int(_cfg.get("repo_map_max_chars", 24000))  # 整体输出硬上限：超出从尾部截断（top_n 展开块在前，最有价值），兜底防超大 repo_map 灌爆历史
GREP_LIMIT: int = int(_cfg.get("grep_count_limit", 30))
GREP_MAX_RESULTS: int = int(_cfg.get("grep_max_results", 80))
GREP_MAX_PER_FILE: int = int(_cfg.get("grep_max_per_file", 10))
GLOB_LIMIT: int = int(_cfg.get("glob_count_limit", 30))
GLOB_MAX_RESULTS: int = int(_cfg.get("glob_max_results", 200))


# 共享 ===================================================================

_IGNORE_DIRS = {".git", ".venv", "venv", "__pycache__", "node_modules",
                ".mypy_cache", ".pytest_cache", ".ruff_cache",
                "dist", "build", ".idea", ".vscode"}
_IGNORE_SUFFIX = {".pyc", ".pyo", ".so", ".dylib", ".dll", ".class",
                  ".jpg", ".jpeg", ".png", ".gif", ".pdf", ".zip", ".tar", ".gz"}


def _iter_files(root: Path, suffix: str | None = None):
    if root.is_file():
        yield root
        return
    for p in root.rglob("*"):
        if not p.is_file() or p.suffix.lower() in _IGNORE_SUFFIX:
            continue
        if any(part in _IGNORE_DIRS for part in p.parts):
            continue
        if suffix and p.suffix.lower() != suffix:
            continue
        yield p


class _Scoped(BaseTool):
    max_tool_calls: int = Field(default=30)
    _call_counts: dict[str, int] = PrivateAttr(default_factory=dict)

    def reset(self) -> None:
        self._call_counts.clear()

    def _resolve(self, rel: str, tid: str) -> tuple[Path | None, str]:
        ws = ensure_workspace(tid)
        sub = (rel or "").strip()
        if not sub:
            return ws, ""
        if sub.startswith("/") or (len(sub) > 1 and sub[1] == ":"):
            return None, f"path 必须是相对路径（workspace 内）：{sub}"
        target = (ws / sub).resolve()
        if not is_inside(target, ws):
            return None, f"path 越界 workspace：{sub}"
        return target, ""

    def _gated(self, rel: str, work: Callable[[Path], str]) -> str:
        tid = current_thread_id()
        ok, n, rem = bump_budget(self._call_counts, tid, self.max_tool_calls)
        if not ok:
            return f"Tool call limit reached ({self.max_tool_calls}) for thread {tid}. {self.name} 预算耗尽。"
        root, err = self._resolve(rel, tid)
        body = err or work(root)
        return f"{body}\n\n[Tool call {n}/{self.max_tool_calls}, remaining: {rem}]"


# RepoMap ===============================================================

def _parse_py(py: Path) -> tuple[list[str], list[str]]:
    try:
        tree = ast.parse(py.read_text(encoding="utf-8", errors="ignore"))
    except (SyntaxError, UnicodeDecodeError):
        return [], []
    sigs, imports = [], []
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            bases = ", ".join(ast.unparse(b) for b in node.bases)
            sigs.append(f"class {node.name}({bases})" if bases else f"class {node.name}")
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    sigs.append(f"    def {sub.name}({ast.unparse(sub.args)})")
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            sigs.append(f"def {node.name}({ast.unparse(node.args)})")
        elif isinstance(node, ast.Import):
            imports.extend(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.append(node.module)
    return sigs, imports


def _pagerank(edges: dict[str, set[str]], damping: float = 0.85, iters: int = 30) -> dict[str, float]:
    nodes = list(edges)
    n = len(nodes) or 1
    score = {k: 1.0 / n for k in nodes}
    outdeg = {k: len(edges[k]) or 1 for k in nodes}
    inbound: dict[str, list[str]] = {k: [] for k in nodes}
    for src, dsts in edges.items():
        for d in dsts:
            if d in inbound:
                inbound[d].append(src)
    for _ in range(iters):
        score = {
            k: (1 - damping) / n + damping * sum(score[s] / outdeg[s] for s in inbound[k])
            for k in nodes
        }
    return score


def _mod_key(rel_path: str) -> str:
    mod = rel_path[:-3] if rel_path.endswith(".py") else rel_path
    if mod.endswith("/__init__"):
        mod = mod[:-9]
    return mod.replace("/", ".")


def _truncate_to_chars(text: str, max_chars: int) -> str:
    """整体输出超过 max_chars 时从尾部按行截断（top_n 展开块在前，最有价值，保头丢尾），并加一行明确提示。"""
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    note = f"\n... [repo_map 输出超过 {max_chars} 字符已从尾部截断；用 path= 缩小范围或 grep 精确定位后再 read_file]"
    budget = max(max_chars - len(note), 0)
    head = text[:budget]
    cut = head.rfind("\n")  # 回退到上一个完整行边界，避免截在半行
    if cut > 0:
        head = head[:cut]
    return head + note


def _build_repo_map(root: Path, top_n: int, max_syms: int, max_listed: int = RM_MAX_LISTED,
                    max_chars: int = RM_MAX_CHARS) -> str:
    files = sorted(_iter_files(root, ".py"))
    if not files:
        return f"未在 {root} 下找到 .py 文件。"
    rel_of = {f: f.relative_to(root).as_posix() for f in files}
    mod_of = {f: _mod_key(r) for f, r in rel_of.items()}
    all_mods = set(mod_of.values())

    sigs_of: dict[Path, list[str]] = {}
    edges: dict[str, set[str]] = {m: set() for m in all_mods}
    for f in files:
        sigs, imps = _parse_py(f)
        sigs_of[f] = sigs
        src = mod_of[f]
        for imp in imps:
            if imp in all_mods and imp != src:
                edges[src].add(imp)
                continue
            best = max((c for c in all_mods if imp.startswith(c + ".")),
                       key=len, default="")
            if best and best != src:
                edges[src].add(best)

    scores = _pagerank(edges)
    ranked = sorted(files, key=lambda f: scores.get(mod_of[f], 0.0), reverse=True)
    top = set(ranked[:top_n])

    out = [
        f"# Repo Map: {root}",
        f"# files={len(files)}  edges={sum(len(v) for v in edges.values())}  "
        f"展开 {min(top_n, len(files))} / {len(files)}",
        "",
    ]
    listed, omitted = 0, 0
    for f in ranked:
        rel, sc, sigs = rel_of[f], scores.get(mod_of[f], 0.0), sigs_of[f]
        if f in top:
            out.append(f"## {rel}  (rank={sc:.4f})")
            out.extend(f"  {s}" for s in sigs[:max_syms])
            if len(sigs) > max_syms:
                out.append(f"  ... (+{len(sigs) - max_syms} more)")
            out.append("")
        elif listed < max_listed:
            out.append(f"- {rel}  (rank={sc:.4f}, {len(sigs)} symbols)")
            listed += 1
        else:
            omitted += 1
    if omitted:
        out.append(f"- …（+{omitted} 个低相关文件已省略；用 path= 缩小范围或 grep 精确定位后再 read_file）")
    return _truncate_to_chars("\n".join(out), max_chars)


class RepoMapInput(BaseModel):
    path: str = Field(default="", description="子目录（workspace 内相对路径），空=workspace 根。")
    top_n: int = Field(default=RM_TOP_N, description="按 PageRank 取前 N 个文件展开签名；其余只列文件名。")
    max_symbols_per_file: int = Field(default=RM_MAX_SYMS, description="单文件最多列出的类/函数签名数。")


class RepoMap(_Scoped):
    name: str = "repo_map"
    description: str = (
        "Aider 风格 Python 项目 overview：AST 抽类/函数签名 + import 图 PageRank，"
        "**只展开 top_n 个核心文件**，其余只列路径。第一次进项目先调它摸结构（大仓库 "
        "top_n 10~15 足够），定位到目标文件/行号后再 read_file 读小段；"
        "**不替代 read_file**——只有签名，没有实现。"
    )
    args_schema: Type[BaseModel] = RepoMapInput
    max_tool_calls: int = Field(default=RM_LIMIT)

    def _run(self, path: str = "", top_n: int = RM_TOP_N, max_symbols_per_file: int = RM_MAX_SYMS) -> str:
        return self._gated(path, lambda root: _build_repo_map(root, top_n, max_symbols_per_file))

    async def _arun(self, path: str = "", top_n: int = RM_TOP_N, max_symbols_per_file: int = RM_MAX_SYMS) -> str:
        return await asyncio.to_thread(self._run, path, top_n, max_symbols_per_file)  # 全仓 AST 扫描不阻塞 event loop


# Grep ==================================================================

def _do_grep(root: Path, pat: re.Pattern, glob: str,
             max_results: int, max_per_file: int) -> str:
    hits: list[str] = []
    truncated: list[str] = []
    scanned = 0
    for f in _iter_files(root):
        rel = f.relative_to(root).as_posix()
        if glob and not (fnmatch.fnmatch(f.name, glob) or fnmatch.fnmatch(rel, glob)):
            continue
        scanned += 1
        try:
            text = f.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        file_hits = 0
        for i, line in enumerate(text.splitlines(), 1):
            if not pat.search(line):
                continue
            if file_hits >= max_per_file:
                truncated.append(rel)
                break
            snippet = line.rstrip()
            if len(snippet) > 240:
                snippet = snippet[:240] + "…"
            hits.append(f"{rel}:{i}:{snippet}")
            file_hits += 1
            if len(hits) >= max_results:
                break
        if len(hits) >= max_results:
            break
    if not hits:
        return f"未匹配（扫描 {scanned} 个文件）。"
    out = [f"# {len(hits)} hits in {scanned} files (root={root})", *hits]
    if len(hits) >= max_results:
        out.append(f"... 命中已达上限 {max_results}，请缩小 path / glob 或加严 pattern。")
    if truncated:
        sample = ", ".join(truncated[:5]) + (" ..." if len(truncated) > 5 else "")
        out.append(f"... 单文件命中超过 {max_per_file} 已截断：{sample}")
    return "\n".join(out)


class GrepInput(BaseModel):
    pattern: str = Field(description="字符串或正则（见 regex）。")
    path: str = Field(default="", description="搜索根（workspace 内相对路径），空=workspace 根。")
    glob: str = Field(default="", description="只搜匹配该 glob 的文件名/相对路径，如 '*.py'、'**/*.md'。")
    regex: bool = Field(default=False, description="True 把 pattern 当 Python re 正则。")
    ignore_case: bool = Field(default=False, description="忽略大小写。")
    max_results: int = Field(default=GREP_MAX_RESULTS, description="返回最大行数（建议 ≤500）。")
    max_per_file: int = Field(default=GREP_MAX_PER_FILE, description="单文件最多返回行数（建议 ≤50）。")


class Grep(_Scoped):
    name: str = "grep"
    description: str = (
        "跨文件文本/正则搜索，返回 `path:line:content`，**带总条数 + 单文件条数双上限**，"
        "超出截断。搜大目录前先用 glob 收窄（如 glob='*.py'）；命中被截断时**收窄 "
        "path/glob 或加严 pattern，不要调大 max_results 拼全量**。只返回命中行本身，"
        "看上下文用 read_file 读那一段；找定义 pattern 用 'class Foo' / 'def foo'。"
    )
    args_schema: Type[BaseModel] = GrepInput
    max_tool_calls: int = Field(default=GREP_LIMIT)

    def _run(self, pattern: str, path: str = "", glob: str = "", regex: bool = False,
             ignore_case: bool = False,
             max_results: int = GREP_MAX_RESULTS, max_per_file: int = GREP_MAX_PER_FILE) -> str:
        try:
            pat = re.compile(pattern if regex else re.escape(pattern),
                             re.IGNORECASE if ignore_case else 0)
        except re.error as e:
            return self._gated(path, lambda _r: f"正则编译失败：{e}")
        return self._gated(
            path,
            lambda root: _do_grep(root, pat, glob.strip(), max_results, max_per_file),
        )

    async def _arun(self, pattern: str, path: str = "", glob: str = "", regex: bool = False,
                    ignore_case: bool = False,
                    max_results: int = GREP_MAX_RESULTS, max_per_file: int = GREP_MAX_PER_FILE) -> str:
        return await asyncio.to_thread(self._run, pattern, path, glob, regex, ignore_case, max_results, max_per_file)


# Glob ==================================================================

def _do_glob(root: Path, pattern: str, cap: int) -> str:
    hits = []
    for f in _iter_files(root):
        rel = f.relative_to(root).as_posix()
        if fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(f.name, pattern):
            hits.append(rel)
            if len(hits) >= cap:
                break
    if not hits:
        return f"未匹配 pattern={pattern}（root={root}）。"
    body = f"# {len(hits)} files (root={root})\n" + "\n".join(hits)
    if len(hits) >= cap:
        body += f"\n... 已达上限 {cap}，请收窄 path 或加严 pattern。"
    return body


class GlobInput(BaseModel):
    pattern: str = Field(description="glob 模式，匹配相对 path 的路径或纯文件名。如 '*.py'、'Tools/**/*.py'。")
    path: str = Field(default="", description="搜索根（workspace 内相对路径），空=workspace 根。")
    max_results: int = Field(default=GLOB_MAX_RESULTS, description="返回最大文件数（建议 ≤1000）。")


class Glob(_Scoped):
    name: str = "glob"
    description: str = (
        "按 glob 列出 workspace 内文件路径（自动跳过 .venv / __pycache__ / node_modules 等）。"
        "**有条数上限**，超出截断。先收窄文件集，再交给 grep / read_file。"
    )
    args_schema: Type[BaseModel] = GlobInput
    max_tool_calls: int = Field(default=GLOB_LIMIT)

    def _run(self, pattern: str, path: str = "", max_results: int = GLOB_MAX_RESULTS) -> str:
        return self._gated(path, lambda root: _do_glob(root, pattern, max_results))

    async def _arun(self, pattern: str, path: str = "", max_results: int = GLOB_MAX_RESULTS) -> str:
        return await asyncio.to_thread(self._run, pattern, path, max_results)
