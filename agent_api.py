import json
import os
from pathlib import Path
from typing import AsyncIterator, Optional

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from langchain_core.messages import HumanMessage
from pydantic import BaseModel

PROJECT_ROOT = Path(__file__).resolve().parent
load_dotenv(PROJECT_ROOT / ".env")

from Agents.manager import manager_session  # noqa: E402
from Agents.collator_scheduler import _read_checkpoint_messages, scheduler  # noqa: E402

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
    return {
        "thread_id": thread_id,
        "messages": [{"type": m.type, "content": getattr(m, "content", "")} for m in msgs],
    }


@app.post("/chat/{thread_id}", dependencies=[Depends(_auth)])
async def chat(thread_id: str, body: ChatBody) -> StreamingResponse:
    async def gen() -> AsyncIterator[str]:
        try:
            async with manager_session(thread_id) as sess:
                async for ev in sess.agent.astream_events(
                    {"messages": [HumanMessage(content=body.message)]},
                    config={"configurable": {"thread_id": thread_id}},
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
