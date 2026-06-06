from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence

from llama_index.core import SimpleDirectoryReader, Document
from llama_index.core.schema import NodeRelationship, RelatedNodeInfo, TextNode
from llama_index.core.node_parser import (
    HierarchicalNodeParser,
    SentenceSplitter,
    get_leaf_nodes,
)


@dataclass
class HeadingLevel:
    """一个层级标题的定义：name 写入 metadata 的键，pattern 匹配整行标题（MULTILINE）。"""

    name: str
    pattern: str


class HeadingTracker:
    """标题结构相关的文本操作：维护层级上下文、剥目录、去标题行。领域规则由 HeadingLevel 注入。"""

    def __init__(self, levels: Sequence[HeadingLevel], toc_pattern: Optional[str] = None) -> None:
        self.levels = list(levels or [])
        self._res = [(h.name, re.compile(h.pattern, re.M)) for h in self.levels]
        self.toc_re = re.compile(toc_pattern, re.M) if toc_pattern else None

    def new_context(self) -> dict:
        return {h.name: "" for h in self.levels}

    def headings(self, segment: str) -> list[tuple[int, int, str, str]]:
        hits: list[tuple[int, int, str, str]] = []
        for idx, (name, rx) in enumerate(self._res):
            for m in rx.finditer(segment):
                hits.append((m.start(), idx, name, m.group(0).strip()))
        hits.sort(key=lambda x: x[0])
        return hits

    def apply(self, segment: str, ctx: dict) -> dict:
        """按顺序套用标题；遇上级标题重置其所有下级。"""
        for _, idx, name, text in self.headings(segment):
            ctx[name] = text
            for lower in self.levels[idx + 1:]:
                ctx[lower.name] = ""
        return ctx

    def strip_headings(self, text: str) -> str:
        for _, rx in self._res:
            text = rx.sub("", text)
        return text

    def strip_toc(self, text: str) -> str:
        """删除开头目录块：从目录行到正文首个标题再次出现处。"""
        if not self.toc_re or not self._res:
            return text
        m = self.toc_re.search(text)
        if not m:
            return text
        after = text[m.end():]
        heads = self.headings(after)
        if not heads:
            return text
        first_pos, _, _, first_text = heads[0]
        for pos, _, _, t in heads:
            if pos > first_pos and t == first_text:
                return text[: m.start()] + after[pos:]
        return text


class DocumentReader:
    """读文件为 llama-index Document 并清洗。"""

    @staticmethod
    def clean(text: str) -> str:
        text = text.replace("\u3000", " ")
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        text = re.sub(r"^[^\S\n]+|[^\S\n]+$", "", text, flags=re.M)
        return text.strip()

    def read(self, input_files=None, *, input_dir=None, required_exts=None, recursive=False) -> list[Document]:
        if input_files is not None:
            reader = SimpleDirectoryReader(input_files=[str(p) for p in input_files])
        elif input_dir is not None:
            reader = SimpleDirectoryReader(
                input_dir=str(input_dir),
                required_exts=list(required_exts) if required_exts else None,
                recursive=recursive,
            )
        else:
            raise ValueError("read 需要 input_files 或 input_dir")
        return [Document(text=self.clean(d.text), metadata=d.metadata) for d in reader.load_data()]


def _sentence_splitter(chunk_size: int) -> SentenceSplitter:
    return SentenceSplitter(
        chunk_size=chunk_size,
        chunk_overlap=chunk_size // 10,
        paragraph_separator="\n",
        secondary_chunking_regex=r"[^,。;:!?，。；：！？]+[,。;:!?，。；：！？]?",
    )


class StructuredDocChunker:
    """通用结构化文档切分（领域无关）：读取 → 剥目录 → 按记录正则切并挂层级上下文 → 超长块层级细分
    → 产出 ``(all_nodes, leaf_nodes)``。领域规则由 unit_pattern + HeadingTracker 注入，组件本身无领域词。

    ``unit_pattern`` 须含且仅含一个捕获组（记录标识，如 ``第N条``）；为空则整篇作为一条记录。
    """

    def __init__(
        self,
        unit_pattern: Optional[str] = None,
        heading_tracker: Optional[HeadingTracker] = None,
        *,
        reader: Optional[DocumentReader] = None,
        chunk_sizes: Sequence[int] = (1024, 256),
        unit_max_chars: int = 512,
    ) -> None:
        self.unit_re = re.compile(unit_pattern, re.M) if unit_pattern else None
        self.tracker = heading_tracker
        self.reader = reader or DocumentReader()
        self.unit_max_chars = unit_max_chars
        ids = [f"l{i}" for i in range(len(chunk_sizes))]
        self.parser = HierarchicalNodeParser.from_defaults(
            node_parser_ids=ids,
            node_parser_map={i: _sentence_splitter(s) for i, s in zip(ids, chunk_sizes)},
        )

    def split_by_unit(self, doc: Document) -> list[Document]:
        text = self.tracker.strip_toc(doc.text) if self.tracker else doc.text
        ctx = self.tracker.new_context() if self.tracker else {}
        file_name = Path(doc.metadata.get("file_path", "")).name or doc.metadata.get("file_name", "")
        if not self.unit_re:
            if self.tracker:
                self.tracker.apply(text, ctx)
            return [Document(id_=file_name or doc.doc_id, text=text.strip(), metadata={**doc.metadata, **ctx})]
        parts = self.unit_re.split(text)
        if self.tracker:
            self.tracker.apply(parts[0], ctx)
        chunks: list[Document] = []
        for i in range(1, len(parts), 2):
            unit_no = parts[i]
            body = parts[i + 1] if i + 1 < len(parts) else ""
            body_clean = self.tracker.strip_headings(body) if self.tracker else body
            chunks.append(Document(
                id_=f"{file_name}:{unit_no}",
                text=f"{unit_no} {body_clean.strip()}",
                metadata={**doc.metadata, **ctx, "unit": unit_no},
            ))
            if self.tracker:
                self.tracker.apply(body, ctx)
        return chunks

    def _set_id(self, node, cid: str) -> None:
        node.node_id = cid
        node.metadata["chunk_id"] = cid
        node.relationships[NodeRelationship.SOURCE] = RelatedNodeInfo(node_id=cid)

    @staticmethod
    def _remap(nodes, id_map: dict) -> None:
        for node in nodes:
            for rel in node.relationships.values():
                if isinstance(rel, list):
                    for it in rel:
                        if it.node_id in id_map:
                            it.node_id = id_map[it.node_id]
                elif rel.node_id in id_map:
                    rel.node_id = id_map[rel.node_id]

    def build_nodes(self, unit_docs) -> tuple[list, list]:
        all_nodes, leaves = [], []
        for ud in unit_docs:
            if len(ud.text) <= self.unit_max_chars:
                node = TextNode(id_=ud.doc_id, text=ud.get_content(), metadata=dict(ud.metadata))
                self._set_id(node, ud.doc_id)
                all_nodes.append(node)
                leaves.append(node)
            else:
                subs = self.parser.get_nodes_from_documents([ud])
                lv = get_leaf_nodes(subs)
                id_map: dict = {}
                for i, node in enumerate(lv):
                    cid = f"{ud.doc_id}:{i}"
                    id_map[node.node_id] = cid
                    self._set_id(node, cid)
                self._remap(subs, id_map)
                all_nodes.extend(subs)
                leaves.extend(lv)
        return all_nodes, leaves

    def run(self, docs=None, *, input_files=None, input_dir=None, required_exts=None) -> tuple[list, list]:
        """端到端：docs 为 None 时用注入的 reader 读取 input_files/input_dir。"""
        if docs is None:
            docs = self.reader.read(input_files, input_dir=input_dir, required_exts=required_exts)
        units: list = []
        for d in docs:
            units.extend(self.split_by_unit(d))
        return self.build_nodes(units)
