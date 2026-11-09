"""HTTP surface.

Submitting returns a short ACK at once. Progress arrives over SSE, and every
event carries its sequence number as the SSE id, so a browser that reconnects
(or reloads the page) sends Last-Event-ID and picks up exactly where it
stopped. The same log is readable by plain polling for clients behind
proxies that break long-lived connections.
"""
from __future__ import annotations

import asyncio
import json
import os
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel, Field, model_validator

from .store import TERMINAL, Store


class Step(BaseModel):
    name: str
    python: str | None = None
    cmd: list[str] | None = None
    timeout_s: float | None = None

    @model_validator(mode="after")
    def one_of(self) -> "Step":
        if (self.python is None) == (self.cmd is None):
            raise ValueError("a step needs exactly one of 'python' or 'cmd'")
        return self


class Job(BaseModel):
    steps: list[Step] = Field(min_length=1)
    input: dict[str, Any] = Field(default_factory=dict)
    timeout_s: float | None = None


def _public(run: dict[str, Any]) -> dict[str, Any]:
    return {k: run[k] for k in ("id", "thread_id", "status", "attempt", "error", "created_at", "updated_at")}


def create_app(store: Store) -> FastAPI:
    app = FastAPI(title="durable-agent-runner")

    @app.post("/threads/{thread_id}/runs", status_code=202)
    def submit(thread_id: str, job: Job):
        run, created = store.submit(thread_id, job.model_dump(exclude_none=True))
        return {"run": _public(run), "created": created}

    @app.get("/threads/{thread_id}")
    def thread(thread_id: str):
        run = store.latest_for_thread(thread_id)
        if run is None:
            raise HTTPException(404, "no runs in this thread")
        return {"run": _public(run), "steps": store.steps(run["id"])}

    @app.get("/runs/{run_id}")
    def get_run(run_id: str):
        run = store.get_run(run_id)
        if run is None:
            raise HTTPException(404, "unknown run")
        return {"run": _public(run), "steps": store.steps(run_id)}

    @app.post("/runs/{run_id}/cancel", status_code=202)
    def cancel(run_id: str):
        run = store.request_cancel(run_id)
        if run is None:
            raise HTTPException(404, "unknown run")
        return {"run": _public(run)}

    @app.get("/runs/{run_id}/events")
    def poll_events(run_id: str, after: int = Query(0, ge=0)):
        if store.get_run(run_id) is None:
            raise HTTPException(404, "unknown run")
        return {"events": store.events(run_id, after)}

    @app.get("/runs/{run_id}/stream")
    async def stream(run_id: str, last_event_id: str | None = Header(None), after: int = Query(0, ge=0)):
        if store.get_run(run_id) is None:
            raise HTTPException(404, "unknown run")
        cursor = int(last_event_id) if last_event_id and last_event_id.isdigit() else after

        async def gen():
            nonlocal cursor
            idle = 0.0
            while True:
                batch = await asyncio.to_thread(store.events, run_id, cursor)
                for e in batch:
                    cursor = e["seq"]
                    yield f"id: {e['seq']}\nevent: {e['type']}\ndata: {json.dumps(e['data'])}\n\n"
                    if e["type"].removeprefix("run.") in TERMINAL:
                        return
                if batch:
                    idle = 0.0
                else:
                    idle += 0.2
                    if idle >= 10:
                        idle = 0.0
                        yield ": keepalive\n\n"
                    await asyncio.sleep(0.2)

        return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-store"})

    @app.get("/", response_class=HTMLResponse)
    def panel():
        with open(os.path.join(os.path.dirname(__file__), "panel.html"), encoding="utf-8") as f:
            return f.read()

    return app


def app_from_env() -> FastAPI:
    return create_app(Store(os.environ.get("DAR_DB", "data/dar.db")))
