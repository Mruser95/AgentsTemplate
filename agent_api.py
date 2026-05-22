import io
import json
import os
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


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


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
