import io
import json
import os
import zipfile
from pathlib import Path
from typing import AsyncIterator, Optional

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from langchain_core.messages import HumanMessage
from pydantic import BaseModel

PROJECT_ROOT = Path(__file__).resolve().parent
load_dotenv(PROJECT_ROOT / ".env")

from Agents.manager import manager_recursion_limit, manager_session  # noqa: E402
from Agents.collator_scheduler import _read_checkpoint_messages, scheduler  # noqa: E402
from Tools._workspace import ensure_workspace, is_inside  # noqa: E402

API_KEY = os.getenv("API_KEY")

app = FastAPI(title="Agent API")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


def _auth(x_api_key: Optional[str] = Header(default=None)) -> None:
    if API_KEY and x_api_key != API_KEY:
        raise HTTPException(401, "invalid api key")


class ChatBody(BaseModel):
    message: str


def _sse(event: str, data) -> str:
    payload = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False, default=str)
    return f"event: {event}\ndata: {payload}\n\n"


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/history/{thread_id}", dependencies=[Depends(_auth)])
async def history(thread_id: str) -> dict:
    msgs = await _read_checkpoint_messages(thread_id)
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


@app.get("/files/{thread_id}", dependencies=[Depends(_auth)])
def files(thread_id: str) -> dict:
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


@app.get("/download/{thread_id}", dependencies=[Depends(_auth)])
def download(thread_id: str, path: str = ""):
    ws = ensure_workspace(thread_id).resolve()
    target = (ws / (path or "").lstrip("/\\")).resolve()
    if not is_inside(target, ws):
        raise HTTPException(403, "path 越界，必须落在 workspace 内")
    if not target.exists():
        raise HTTPException(404, f"path 不存在：{path}")
    if target.is_file():
        return FileResponse(target, filename=target.name)
    return _zip_dir(target, target.name if target != ws else thread_id)


@app.post("/chat/{thread_id}", dependencies=[Depends(_auth)])
async def chat(thread_id: str, body: ChatBody) -> StreamingResponse:
    async def gen() -> AsyncIterator[str]:
        try:
            async with manager_session(thread_id) as sess:
                async for ev in sess.agent.astream_events(
                    {"messages": [HumanMessage(content=body.message)]},
                    config={
                        "configurable": {"thread_id": thread_id},
                        "recursion_limit": manager_recursion_limit,
                    },
                    version="v2",
                ):
                    name, data = ev.get("event"), ev.get("data") or {}
                    if name == "on_chat_model_stream":
                        text = getattr(data.get("chunk"), "content", "")
                        if isinstance(text, str) and text:
                            yield _sse("token", json.dumps(text, ensure_ascii=False))
                    elif name == "on_tool_start":
                        yield _sse("tool_start", {"name": ev.get("name"), "args": data.get("input")})
                    elif name == "on_tool_end":
                        yield _sse("tool_end", {"name": ev.get("name"), "output": str(data.get("output"))})
            scheduler.notify(thread_id)
            yield _sse("done", "[DONE]")
        except Exception as e:
            yield _sse("error", {"error": str(e)})

    return StreamingResponse(gen(), media_type="text/event-stream")
