import asyncio
import json
import math
import os
import sqlite3
from pathlib import Path
from typing import Callable, Optional
from dotenv import load_dotenv
from langchain_openai import OpenAIEmbeddings

load_dotenv(Path(__file__).resolve().parent.parent / ".env")


embeddings = OpenAIEmbeddings(
    model=os.getenv("embedding_model", "text-embedding-3-small"),
    api_key=os.getenv("small_llm_key"),
    base_url=os.getenv("small_llm_base_url"),
)


def cos(a: list[float], b: list[float]) -> float:
    if not a or not b:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


# sqlite =======================================================================


def open_db(db_path: str | Path, ddl: list[str]) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    for stmt in ddl:
        conn.execute(stmt)
    conn.commit()
    return conn


async def ensure_conn(
    factory: Callable[[], sqlite3.Connection], conn: Optional[sqlite3.Connection],
) -> tuple[sqlite3.Connection, bool]:
    if conn is not None:
        return conn, False
    new_conn = await asyncio.to_thread(factory)
    return new_conn, True


def rank_rows(
    rows: list[dict], query_vecs: list[list[float]], k: int, *, vec_key: str = "_vec", id_key: str = "id",
) -> list[dict]:
    by_id: dict = {}
    for q in query_vecs:
        scored = [(r, cos(q, r[vec_key])) for r in rows]
        scored.sort(key=lambda x: x[1], reverse=True)
        for r, s in scored[:k]:
            prev = by_id.get(r[id_key])
            if prev is None or s > prev["similarity"]:
                rec = {kk: vv for kk, vv in r.items() if kk != vec_key}
                rec["similarity"] = float(s)
                by_id[r[id_key]] = rec
    return list(by_id.values())


def loads_list(s: Optional[str]) -> list:
    return json.loads(s or "[]")


def dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False)
