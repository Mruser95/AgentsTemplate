import asyncio
import io
import json
import os
import sqlite3
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator, Optional

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

PROJECT_ROOT = Path(__file__).resolve().parent
load_dotenv(PROJECT_ROOT / ".env")

from Agents.manager import CHECKPOINT_DB, manager_session  # noqa: E402
from Tools.utils import ensure_workspace, is_inside, read_ckpt_msgs  # noqa: E402
from schedule import scheduler  # noqa: E402

API_KEY = os.getenv("API_KEY")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    CHECKPOINT_DB.parent.mkdir(parents=True, exist_ok=True)
    async with AsyncSqliteSaver.from_conn_string(str(CHECKPOINT_DB)) as saver:
        await saver.setup()
    try:
        yield
    finally:
        scheduler.shutdown()


app = FastAPI(title="Agent API", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


def _auth(x_api_key: Optional[str] = Header(default=None)) -> None:
    if API_KEY and x_api_key != API_KEY:
        raise HTTPException(401, "invalid api key")


class ChatBody(BaseModel):
    message: str


def _sse(event: str, data) -> str:
    payload = json.dumps(data, ensure_ascii=False, default=str)
    return f"event: {event}\ndata: {payload}\n\n"


# 线程列表（SessionDB 中所有会话，全局，不按 API key 划分）=================
def _distinct_thread_ids() -> list[str]:
    """从 checkpoints.db 读所有不同的 thread_id；库/表不存在时返回空。"""
    if not CHECKPOINT_DB.exists():
        return []
    conn = sqlite3.connect(str(CHECKPOINT_DB))
    try:
        cur = conn.execute("SELECT DISTINCT thread_id FROM checkpoints")
        return [r[0] for r in cur.fetchall() if r[0]]
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()


def _thread_title(msgs: list) -> str:
    """取第一条用户消息前若干字符作为会话标题。"""
    for m in msgs:
        if getattr(m, "type", None) != "human":
            continue
        content = getattr(m, "content", "")
        if isinstance(content, list):
            content = " ".join(
                str(p.get("text", "")) if isinstance(p, dict) else str(p) for p in content
            )
        text = str(content or "").strip().replace("\n", " ")
        if text:
            return text[:48]
    return ""


async def _list_thread_summaries() -> list[dict]:
    """列出 SessionDB 中所有线程概要（thread_id / title / message_count / updated_at），按最近活动降序。"""
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    tids = await asyncio.to_thread(_distinct_thread_ids)
    if not tids:
        return []
    out: list[dict] = []
    async with AsyncSqliteSaver.from_conn_string(str(CHECKPOINT_DB)) as saver:
        for tid in tids:
            tup = await saver.aget_tuple({"configurable": {"thread_id": tid}})
            ckpt = (getattr(tup, "checkpoint", None) or {}) if tup else {}
            msgs = ckpt.get("channel_values", {}).get("messages")
            msgs = msgs if isinstance(msgs, list) else []
            out.append({
                "thread_id": tid,
                "title": _thread_title(msgs),
                "message_count": len(msgs),
                "updated_at": ckpt.get("ts") or "",
            })
    out.sort(key=lambda x: x["updated_at"], reverse=True)
    return out


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/threads", dependencies=[Depends(_auth)])
async def list_threads() -> dict:
    """列出 SessionDB 中所有会话线程（全局，不按 key 区分），供 WebUI 左侧列表使用。"""
    return {"threads": await _list_thread_summaries()}


@app.get("/threads/{thread_id}/messages", dependencies=[Depends(_auth)])
async def list_messages(thread_id: str) -> dict:
    msgs = await read_ckpt_msgs(thread_id)
    out = []
    for m in msgs:
        item = {"type": m.type, "content": getattr(m, "content", "")}
        tcs = getattr(m, "tool_calls", None)
        if tcs:
            item["tool_calls"] = [
                {"id": tc.get("id"), "name": tc.get("name"), "args": tc.get("args")}
                for tc in tcs
            ]
        tcid = getattr(m, "tool_call_id", None)
        if tcid:
            item["tool_call_id"] = tcid
        name = getattr(m, "name", None)
        if name:
            item["name"] = name
        out.append(item)
    return {"thread_id": thread_id, "messages": out}


@app.get("/threads/{thread_id}/files", dependencies=[Depends(_auth)])
def list_files(thread_id: str) -> dict:
    ws = ensure_workspace(thread_id)
    items = [
        {
            "name": p.name,
            "path": str(p.relative_to(ws)).replace(os.sep, "/"),
            "type": "dir" if p.is_dir() else "file",
            "size": p.stat().st_size if p.is_file() else 0,
        }
        for p in sorted(ws.rglob("*"))
    ]
    return {"thread_id": thread_id, "workspace": str(ws), "files": items}


def _zip_dir(directory: Path, archive_name: str) -> StreamingResponse:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in directory.rglob("*"):
            if p.is_file():
                arc = Path(archive_name) / p.relative_to(directory)
                zf.write(p, arcname=str(arc).replace(os.sep, "/"))
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{archive_name}.zip"'},
    )


@app.get("/threads/{thread_id}/files/{file_path:path}", dependencies=[Depends(_auth)])
def get_file(thread_id: str, file_path: str = ""):
    ws = ensure_workspace(thread_id).resolve()
    target = (ws / file_path.lstrip("/\\")).resolve()
    if not is_inside(target, ws):
        raise HTTPException(403, "path 越界，必须落在 workspace 内")
    if not target.exists():
        raise HTTPException(404, f"path 不存在：{file_path}")
    if target.is_file():
        return FileResponse(target, filename=target.name)
    return _zip_dir(target, target.name if target != ws else thread_id)


@app.post("/threads/{thread_id}/messages", dependencies=[Depends(_auth)])
async def create_message(thread_id: str, body: ChatBody) -> StreamingResponse:
    async def gen() -> AsyncIterator[str]:
        try:
            async with manager_session(thread_id) as sess:
                async for name, payload in sess.astream(body.message):
                    yield _sse(name, payload)
            yield _sse("done", "[DONE]")
        except Exception as e:
            yield _sse("error", {"error": str(e)})

    return StreamingResponse(gen(), media_type="text/event-stream")
