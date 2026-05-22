from llama_index.core import SimpleDirectoryReader, Document
from llama_index.core.schema import NodeRelationship, RelatedNodeInfo, TextNode
from llama_index.core.node_parser import SentenceSplitter, HierarchicalNodeParser
from llama_index.core.node_parser import get_leaf_nodes
from pathlib import Path
import re

origin_path = Path(__file__).resolve().parent / "origin_file"

def clean_text(text: str) -> str:
    text = text.replace("\u3000", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"^[^\S\n]+|[^\S\n]+$", "", text, flags=re.M)
    return text.strip()

def read_documents(input_files=None):
    if input_files is None:
        reader = SimpleDirectoryReader(
            input_dir=str(origin_path),
            required_exts=[".docx"],
            recursive=False,
        )
    else:
        reader = SimpleDirectoryReader(input_files=[str(p) for p in input_files])
    return [
        Document(text=clean_text(d.text), metadata=d.metadata)
        for d in reader.load_data()
    ]


CN_NUM = r"[一二三四五六七八九十百千零〇\d]"
HEADING = re.compile(rf"^第{CN_NUM}+([编章节])\s*.*$", re.M)
ARTICLE = re.compile(rf"^(第{CN_NUM}+条(?:之{CN_NUM}+)?)", re.M)

LEVELS = ["part", "chapter", "section"]
LEVEL_OF = {"编": "part", "章": "chapter", "节": "section"}


def strip_toc(text: str) -> str:
    """删除文档开头的「目录」块：从「目录」行到正文重新出现首个标题处。
    否则 update_ctx 会把目录里列出的所有标题都扫进去，污染上下文。
    """
    m = re.search(r"^目\s*录\s*$", text, re.M)
    if not m:
        return text
    after = text[m.end():]
    first = HEADING.search(after)
    if not first:
        return text
    first_heading = first.group(0).strip()
    for mm in HEADING.finditer(after):
        if mm.start() > first.end() and mm.group(0).strip() == first_heading:
            return text[:m.start()] + after[mm.start():]
    return text

def update_ctx(segment, ctx):
    """按文档顺序应用标题，遇到上级标题时重置其下级（换章时清掉旧节）。"""
    for m in HEADING.finditer(segment):
        level = LEVEL_OF[m.group(1)]
        ctx[level] = m.group(0).strip()
        for lower in LEVELS[LEVELS.index(level) + 1:]:
            ctx[lower] = ""
    return ctx

def split_by_article(doc: Document):
    text = strip_toc(doc.text)
    parts = ARTICLE.split(text)
    preamble = parts[0]
    chunks = []
    file_name = Path(doc.metadata.get("file_path", "")).name or doc.metadata.get("file_name", "")
    current_ctx = {"part": "", "chapter": "", "section": ""}
    update_ctx(preamble, current_ctx)
    for i in range(1, len(parts), 2):
        article_no = parts[i]
        body = parts[i + 1] if i + 1 < len(parts) else ""
        body_clean = HEADING.sub("", body)
        chunks.append(Document(
            id_=f"{file_name}:{article_no}",
            text=f"{article_no} {body_clean.strip()}",
            metadata={
                **doc.metadata,
                **current_ctx,
                "article": article_no,
            }
        ))
        update_ctx(body, current_ctx)
    return chunks


def get_splitter(chunk_size: int):
    return SentenceSplitter(
        chunk_size=chunk_size,
        chunk_overlap=chunk_size // 10,
        paragraph_separator="\n",
        secondary_chunking_regex=r"[^,。;:!?，。；：！？]+[,。;:!?，。；：！？]?",
    )

node_parser = HierarchicalNodeParser.from_defaults(
    node_parser_ids=["l0", "l1"],
    node_parser_map={
        "l0": get_splitter(1024),
        "l1": get_splitter(256),
    },
)

def _set_chunk_id(node, chunk_id):
    node.node_id = chunk_id
    node.metadata["chunk_id"] = chunk_id
    node.relationships[NodeRelationship.SOURCE] = RelatedNodeInfo(node_id=chunk_id)

def _remap_relationship_ids(nodes, id_map):
    for node in nodes:
        for rel in node.relationships.values():
            if isinstance(rel, list):
                for item in rel:
                    if item.node_id in id_map:
                        item.node_id = id_map[item.node_id]
            elif rel.node_id in id_map:
                rel.node_id = id_map[rel.node_id]

def build_nodes(docs=None):
    if docs is None or (docs and isinstance(docs[0], (str, Path))):
        docs = read_documents(docs)
    all_nodes, nodes = [], []
    for doc in docs:
        for art_doc in split_by_article(doc):
            if len(art_doc.text) <= 512:
                node = TextNode(
                        id_=art_doc.doc_id,
                        text=art_doc.get_content(),
                        metadata=dict(art_doc.metadata),
                    )
                _set_chunk_id(node, art_doc.doc_id)
                all_nodes.append(node)
                nodes.append(node)
            else:
                sub_nodes = node_parser.get_nodes_from_documents([art_doc])
                leaves = get_leaf_nodes(sub_nodes)
                id_map = {}
                for i, node in enumerate(leaves):
                    chunk_id = f"{art_doc.doc_id}:{i}"
                    id_map[node.node_id] = chunk_id
                    _set_chunk_id(node, chunk_id)
                _remap_relationship_ids(sub_nodes, id_map)
                all_nodes.extend(sub_nodes)
                nodes.extend(leaves)
    return all_nodes, nodes


if __name__ == "__main__":
    all_nodes, nodes = build_nodes()
    print(len(nodes))
    for node in nodes[:10]:
        print(node.text)
        print(node.metadata)
        print("-" * 100)
