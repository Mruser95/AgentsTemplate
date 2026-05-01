import asyncio
import glob
import json
import os
import sys
from pathlib import Path
from typing import Optional, Type
import jieba
import numpy as np
import psycopg
import yaml
from langchain_core.tools import BaseTool
from pgvector.psycopg import register_vector
from pydantic import BaseModel, Field, PrivateAttr
from rank_bm25 import BM25Okapi
from sentence_transformers import CrossEncoder, SentenceTransformer

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from Tools.utils import bump_budget, current_thread_id  # noqa: E402

with open(PROJECT_ROOT / "config.yaml", encoding="utf-8") as _f:
    _cfg = yaml.safe_load(_f) or {}

DSN: str = os.getenv("RAG_DSN") or _cfg.get(
    "dsn", "postgresql://postgres:postgres@localhost:5432/rag"
)
DIM: int = int(_cfg.get("dim", 1024))
EMBED_MODEL: str = _cfg.get("embed_model", "BAAI/bge-m3")
RERANK_MODEL: str = _cfg.get("rerank_model", "BAAI/bge-reranker-v2-m3")
RETRIEVE_TOP_K: int = int(_cfg.get("retrieve_top_k", 5))
RETRIEVE_CALL_LIMIT: int = int(_cfg.get("retrieve_call_limit", 20))
VEC_K: int = int(_cfg.get("vec_k", 30))
BM25_K: int = int(_cfg.get("bm25_k", 30))
RRF_C: int = int(_cfg.get("rrf_c", 60))

def _tokenize(text: str) -> list[str]:
    return [w for w in jieba.cut_for_search(text.lower()) if w.strip()]


def _load_docs(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        records = (
            [json.loads(line) for line in f if line.strip()]
            if path.endswith(".jsonl")
            else json.load(f)
        )
    return [r for r in records if r.get("content")]


def _format_hits(hits: list[dict], snippet: int = 500) -> str:
    if not hits:
        return "No results."
    parts = []
    for i, h in enumerate(hits, 1):
        body = h["content"].strip()
        if len(body) > snippet:
            body = body[:snippet] + "...[truncated]"
        parts.append(f"[{i}] id={h['id']}  score={h['score']:.4f}\n{body}")
    return "\n\n".join(parts)


class HybridRetriever:
    def __init__(self, dsn: str = DSN) -> None:
        self.dsn = dsn
        self._conn: Optional[psycopg.Connection] = None
        self._embedder: Optional[SentenceTransformer] = None
        self._reranker: Optional[CrossEncoder] = None
        self._bm25: Optional[BM25Okapi] = None
        self._ids: list[int] = []

    @property
    def conn(self) -> psycopg.Connection:
        if self._conn is None or self._conn.closed:
            c = psycopg.connect(self.dsn, autocommit=True)
            register_vector(c)
            c.execute("CREATE EXTENSION IF NOT EXISTS vector")
            c.execute(
                f"CREATE TABLE IF NOT EXISTS chunks (id SERIAL PRIMARY KEY, "
                f"content TEXT NOT NULL, metadata JSONB, embedding vector({DIM}))"
            )
            c.execute(
                "CREATE INDEX IF NOT EXISTS chunks_emb_idx ON chunks "
                "USING hnsw (embedding vector_cosine_ops)"
            )
            self._conn = c
        return self._conn

    @property
    def embedder(self) -> SentenceTransformer:
        if self._embedder is None:
            self._embedder = SentenceTransformer(EMBED_MODEL)
        return self._embedder

    @property
    def reranker(self) -> CrossEncoder:
        if self._reranker is None:
            self._reranker = CrossEncoder(RERANK_MODEL)
        return self._reranker

    def count(self) -> int:
        return int(self.conn.execute("SELECT count(*) FROM chunks").fetchone()[0])

    def ingest(self, files: list[str], batch: int = 64) -> int:
        docs = [d for p in files for d in _load_docs(p)]
        if not docs:
            return 0
        texts = [d["content"] for d in docs]
        metas = [json.dumps(d.get("metadata") or {}, ensure_ascii=False) for d in docs]
        embs = self.embedder.encode(
            texts, normalize_embeddings=True, batch_size=batch, show_progress_bar=False
        )
        with self.conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO chunks (content, metadata, embedding) VALUES (%s, %s, %s)",
                list(zip(texts, metas, embs)),
            )
        self._bm25 = None
        return len(docs)

    def _load_bm25(self) -> None:
        rows = self.conn.execute("SELECT id, content FROM chunks").fetchall()
        self._ids = [r[0] for r in rows]
        corpus = [_tokenize(r[1]) for r in rows]
        self._bm25 = BM25Okapi(corpus) if corpus else None

    def search(self, query: str, k: int = RETRIEVE_TOP_K, 
        vec_k: int = VEC_K, bm25_k: int = BM25_K, rrf_c: int = RRF_C
    ) -> list[dict]:
        if self._bm25 is None:
            self._load_bm25()
        if not self._ids or self._bm25 is None:
            return []

        qv = self.embedder.encode(query, normalize_embeddings=True)
        vec_hits = self.conn.execute(
            "SELECT id FROM chunks ORDER BY embedding <=> %s LIMIT %s", (qv, vec_k)
        ).fetchall()
        bm_scores = self._bm25.get_scores(_tokenize(query))
        bm_top = np.argsort(bm_scores)[-bm25_k:][::-1]

        rrf: dict[int, float] = {}
        for r, (cid,) in enumerate(vec_hits):
            rrf[cid] = rrf.get(cid, 0.0) + 1.0 / (rrf_c + r)
        for r, i in enumerate(bm_top):
            cid = self._ids[int(i)]
            rrf[cid] = rrf.get(cid, 0.0) + 1.0 / (rrf_c + r)

        cand = sorted(rrf, key=rrf.get, reverse=True)[: max(vec_k, bm25_k)]
        rows = self.conn.execute(
            "SELECT id, content, metadata FROM chunks WHERE id = ANY(%s)", (cand,)
        ).fetchall()
        by_id = {r[0]: (r[1], r[2]) for r in rows}

        pairs = [(query, by_id[cid][0]) for cid in cand if cid in by_id]
        if not pairs:
            return []
        scores = self.reranker.predict(pairs)
        ranked = sorted(zip(cand, scores), key=lambda x: x[1], reverse=True)[:k]
        return [
            {"id": cid, "content": by_id[cid][0], "metadata": by_id[cid][1], "score": float(s)}
            for cid, s in ranked
        ]


# Tools =======================================================================


_retriever: Optional[HybridRetriever] = None

def get_retriever() -> HybridRetriever:
    global _retriever
    if _retriever is None:
        _retriever = HybridRetriever()
    return _retriever


class IngestInput(BaseModel):
    pattern: str = Field(
        description='Glob for chunk JSON/JSONL files, e.g. "Knowledge/chunks/*.json". '
                    'Each record must be {"content": str, "metadata": dict} (metadata optional).'
    )

class SearchInput(BaseModel):
    query: str = Field(description="Natural-language query in Chinese or English.")
    k: int = Field(default=RETRIEVE_TOP_K, ge=1, le=20, description="Top-K after rerank.")


class KnowledgeIngest(BaseTool):
    name: str = "knowledge_ingest"
    description: str = (
        "Ingest chunk JSON/JSONL files into the local pgvector store. "
        "Run once per new dataset; do not re-ingest the same files."
    )
    args_schema: Type[BaseModel] = IngestInput

    def _run(self, pattern: str) -> str:
        files = glob.glob(pattern)
        if not files:
            return f"No files matched: {pattern}"
        try:
            r = get_retriever()
            before = r.count()
            added = r.ingest(files)
            after = r.count()
        except Exception as e:
            return f"knowledge_ingest failed: {e!r}"
        return f"Ingested {added} chunks from {len(files)} file(s). Store: {before} -> {after}."

    async def _arun(self, pattern: str) -> str:
        return await asyncio.to_thread(self._run, pattern)


class KnowledgeSearch(BaseTool):
    name: str = "knowledge_search"
    description: str = (
        "Hybrid (vector + BM25 + CrossEncoder rerank) search over the local knowledge store. "
        "Use for domain facts already ingested; do NOT use for open-web lookups."
    )
    args_schema: Type[BaseModel] = SearchInput
    max_tool_calls: int = Field(default=RETRIEVE_CALL_LIMIT)
    _call_counts: dict[str, int] = PrivateAttr(default_factory=dict)

    def reset(self) -> None:
        self._call_counts.clear()

    def _run(self, query: str, k: int = RETRIEVE_TOP_K) -> str:
        tid = current_thread_id()
        ok, n, rem = bump_budget(self._call_counts, tid, self.max_tool_calls)
        if not ok:
            return (
                f"Tool call limit reached ({self.max_tool_calls}) for thread {tid}. "
                "Stop using knowledge_search."
            )
        try:
            body = _format_hits(get_retriever().search(query, k=k))
        except Exception as e:
            body = f"knowledge_search failed: {e!r}"
        return f"{body}\n\n[Tool call {n}/{self.max_tool_calls}, remaining: {rem}]"

    async def _arun(self, query: str, k: int = RETRIEVE_TOP_K) -> str:
        return await asyncio.to_thread(self._run, query, k)
