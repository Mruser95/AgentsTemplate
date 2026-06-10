---
name: doc_chunking
description: 通用结构化文档清洗+层级切分组件，产出 llama-index 节点。按「记录正则 + 层级标题」参数化，适配法律/合同/手册等任意结构化文本，组件本身不含领域词。一个完整功能：原始文档→可入库节点。
---

# doc_chunking — StructuredDocChunker

实现文件：`CompLib/doc_chunking/doc_chunking.py`（单文件，内含下列协作类）

## 用途
长文档清洗 → 剥目录 → 按记录边界切原子块并挂层级上下文 → 超长块层级细分 → `(all_nodes, leaf_nodes)`，
直接喂 llama-index 入库 + AutoMerging。领域规则全由构造参数注入。

## 接口
`from CompLib.doc_chunking.doc_chunking import StructuredDocChunker, HeadingTracker, HeadingLevel`

- `HeadingLevel(name, pattern)`：一个层级标题定义（dataclass）
- `HeadingTracker(levels, toc_pattern=None)`：层级上下文 + 剥目录 + 去标题（被 chunker 内部使用，可单测）
- `DocumentReader().read(input_files=None, *, input_dir=None, required_exts=None)`：读文件并清洗（`clean` 为静态方法）
- `StructuredDocChunker(unit_pattern=None, heading_tracker=None, *, reader=None, chunk_sizes=(1024,256), unit_max_chars=512)`
  - `unit_pattern`：记录边界正则，**须含且仅含一个捕获组**（如 `第N条`）；None=整篇一条
  - `split_by_unit(doc)->list[Document]` / `build_nodes(unit_docs)->(all,leaves)` / `run(docs=None, *, input_files=None, input_dir=None)->(all,leaves)`

## 依赖
`llama-index-core`（SimpleDirectoryReader / HierarchicalNodeParser / TextNode）

## 用法示例
```python
from CompLib.doc_chunking.doc_chunking import StructuredDocChunker, HeadingTracker, HeadingLevel
CN = r"[一二三四五六七八九十百千零〇\d]"
tracker = HeadingTracker([HeadingLevel("part", rf"^第{CN}+编.*$"), HeadingLevel("chapter", rf"^第{CN}+章.*$")],
                         toc_pattern=r"^目\s*录\s*$")
chunker = StructuredDocChunker(rf"^(第{CN}+条(?:之{CN}+)?)", tracker)
all_nodes, leaves = chunker.run(input_files=["law.docx"])   # 或 chunker.run([Document(...)])
```
