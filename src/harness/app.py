"""The whole service: runner API, agent API, and the web panel.

    DAR_DATA=data uvicorn --factory harness.app:app_from_env

By default the graph reaches its tools through an MCP server started as a
subprocess over stdio. `DAR_TOOLS=direct` calls them in-process instead.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import sys
from typing import Any

from fastapi import FastAPI, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from pydantic import BaseModel

from dar.api import create_app as create_runner_app
from dar.store import Store

from .advisor import make_advisor
from .sample import sample_export
from .service import Harness, ThreadBusy, UnknownThread
from .toolclient import DirectToolClient, McpToolClient
from .tools import ToolError, Tools

SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB_DIST = os.path.join(os.path.dirname(SRC), "web", "dist")
MAX_UPLOAD = 200 * 1024 * 1024
_THREAD = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class StartBody(BaseModel):
    dataset_id: str


class ResumeBody(BaseModel):
    action: str
    config: dict[str, Any] | None = None
    text: str | None = None


def create_app(data_dir: str = "data", tools_mode: str | None = None, advisor=None, reconcile_every: float = 0.3) -> FastAPI:
    os.makedirs(data_dir, exist_ok=True)
    data_dir = os.path.abspath(data_dir)
    mode = tools_mode or os.environ.get("DAR_TOOLS", "mcp")
    store = Store(os.path.join(data_dir, "dar.db"))
    tools = Tools(store, os.path.join(data_dir, "datasets"), os.path.join(data_dir, "ws"), os.path.join(data_dir, "outbox"))

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        async with contextlib.AsyncExitStack() as stack:
            saver = await stack.enter_async_context(AsyncSqliteSaver.from_conn_string(os.path.join(data_dir, "harness.db")))
            if mode == "mcp":
                from mcp import Client, StdioServerParameters

                params = StdioServerParameters(
                    command=sys.executable,
                    args=["-m", "harness.mcp_server"],
                    env={**os.environ, "DAR_DATA": data_dir, "PYTHONPATH": SRC + os.pathsep + os.environ.get("PYTHONPATH", "")},
                )
                client = await stack.enter_async_context(Client(params))
                tool_client = McpToolClient(client)
            else:
                tool_client = DirectToolClient(tools)
            harness = Harness(store, tool_client, advisor or make_advisor(), saver)
            await harness.load()
            app.state.harness = harness

            async def reconciler():
                while True:
                    try:
                        await harness.reconcile_once()
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:      # keep the loop alive; the next pass retries
                        print(f"reconcile failed: {e!r}", file=sys.stderr)
                    await asyncio.sleep(reconcile_every)

            task = asyncio.create_task(reconciler())
            try:
                yield
            finally:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    app = create_runner_app(store, lifespan=lifespan, panel_path="/panel")

    def harness() -> Harness:
        return app.state.harness

    def check_thread(thread_id: str) -> None:
        if not _THREAD.match(thread_id):
            raise HTTPException(400, "thread id must be 1-64 letters, digits, dashes or underscores")

    @app.get("/api/info")
    def info():
        h = harness()
        return {"advisor": h.advisor.name, "tools": mode, "model": getattr(h.advisor, "model", None)}

    @app.get("/api/datasets")
    def datasets():
        return tools.list_datasets()

    @app.post("/api/datasets", status_code=201)
    async def upload(file: UploadFile):
        data = await file.read(MAX_UPLOAD + 1)
        if len(data) > MAX_UPLOAD:
            raise HTTPException(413, "file is larger than 200 MB")
        if not data.strip():
            raise HTTPException(400, "file is empty")
        return await asyncio.to_thread(tools.save_dataset, file.filename or "upload.csv", data)

    @app.post("/api/datasets/sample", status_code=201)
    async def sample(rows: int = 200_000):
        if not 1000 <= rows <= 2_000_000:
            raise HTTPException(400, "rows must be between 1,000 and 2,000,000")
        data = await asyncio.to_thread(sample_export, rows)
        return await asyncio.to_thread(tools.save_dataset, f"sample_{rows}.csv", data)

    @app.post("/api/threads/{thread_id}/start")
    async def start(thread_id: str, body: StartBody):
        check_thread(thread_id)
        try:
            return await harness().start(thread_id, body.dataset_id)
        except ThreadBusy as e:
            raise HTTPException(409, str(e))

    @app.post("/api/threads/{thread_id}/resume")
    async def resume(thread_id: str, body: ResumeBody):
        check_thread(thread_id)
        try:
            return await harness().resume(thread_id, body.model_dump(exclude_none=True))
        except ThreadBusy as e:
            raise HTTPException(409, str(e))
        except ToolError as e:
            raise HTTPException(400, str(e))

    @app.get("/api/threads/{thread_id}")
    async def thread(thread_id: str):
        check_thread(thread_id)
        try:
            return await harness().view(thread_id)
        except UnknownThread:
            raise HTTPException(404, "no such thread")

    @app.get("/api/threads/{thread_id}/events")
    async def thread_events(thread_id: str):
        """The thread's state as a stream of snapshots, sent whenever it changes."""
        check_thread(thread_id)

        async def gen():
            last, idle = None, 0.0
            while True:
                try:
                    snapshot = json.dumps(await harness().view(thread_id), sort_keys=True)
                except UnknownThread:
                    snapshot = "null"
                if snapshot != last:
                    last, idle = snapshot, 0.0
                    yield f"event: view\ndata: {snapshot}\n\n"
                else:
                    idle += 0.4
                    if idle >= 10:
                        idle = 0.0
                        yield ": keepalive\n\n"
                await asyncio.sleep(0.4)

        return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-store"})

    if os.path.isdir(WEB_DIST):
        app.mount("/", StaticFiles(directory=WEB_DIST, html=True), name="web")
    return app


def app_from_env() -> FastAPI:
    return create_app(os.environ.get("DAR_DATA", "data"))
