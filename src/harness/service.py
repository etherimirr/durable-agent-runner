"""Drives the graph for the HTTP layer.

Three entry points change a thread: `start`, `resume` (something the user
did) and `reconcile_once` (a background run reached a terminal state). They
are serialized per thread, and each returns the thread's current view.

The graph's checkpoint is the only record of where a conversation stands.
The table of threads waiting on a run is rebuilt from it at startup, so a
restart of this process loses nothing.
"""
from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import Any

from langgraph.types import Command

from dar.store import TERMINAL, Store

from .advisor import Advisor
from .graph import build_graph, entry
from .toolclient import ToolClient
from .tools import ToolError


class ThreadBusy(Exception):
    pass


class UnknownThread(Exception):
    pass


class Harness:
    def __init__(self, store: Store, tools: ToolClient, advisor: Advisor, checkpointer):
        self.store = store
        self.tools = tools
        self.advisor = advisor
        self.checkpointer = checkpointer
        self.graph = build_graph(tools, advisor).compile(checkpointer=checkpointer)
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._waiting: dict[str, str] = {}      # thread id -> run id it is waiting on
        self._loaded = False

    @staticmethod
    def _cfg(thread_id: str) -> dict[str, Any]:
        return {"configurable": {"thread_id": thread_id}}

    async def _pending(self, thread_id: str) -> tuple[Any, dict[str, Any] | None]:
        state = await self.graph.aget_state(self._cfg(thread_id))
        pending = state.interrupts[0].value if state.interrupts else None
        if pending and pending.get("kind") == "await_run":
            self._waiting[thread_id] = pending["run_id"]
        else:
            self._waiting.pop(thread_id, None)
        return state, pending

    async def load(self) -> None:
        """Rebuild the waiting table from the checkpoints. Idempotent."""
        # Finish listing before reading any state: the saver holds its lock
        # for the whole listing, and reading a state needs that lock too.
        thread_ids = {item.config["configurable"]["thread_id"] async for item in self.checkpointer.alist(None)}
        for thread_id in thread_ids:
            await self._pending(thread_id)
        self._loaded = True

    # -- entry points ------------------------------------------------------

    async def start(self, thread_id: str, dataset_id: str) -> dict[str, Any]:
        async with self._locks[thread_id]:
            state, pending = await self._pending(thread_id)
            if pending is not None or state.next:
                raise ThreadBusy("this thread is in the middle of an analysis")
            await self.graph.ainvoke(
                {
                    "thread_id": thread_id,
                    "dataset_id": dataset_id,
                    "phase": "preview",
                    "transcript": [entry("user", "start", dataset_id=dataset_id)],
                },
                self._cfg(thread_id),
            )
            return await self._view_locked(thread_id)

    async def resume(self, thread_id: str, answer: dict[str, Any]) -> dict[str, Any]:
        async with self._locks[thread_id]:
            _, pending = await self._pending(thread_id)
            if pending is None:
                raise ThreadBusy("nothing is waiting for input in this thread")
            await self.graph.ainvoke(Command(resume=answer), self._cfg(thread_id))
            return await self._view_locked(thread_id)

    async def reconcile_once(self) -> int:
        """Wake every thread whose background run has ended. Returns how many."""
        if not self._loaded:
            await self.load()
        woken = 0
        for thread_id, run_id in list(self._waiting.items()):
            run = await asyncio.to_thread(self.store.get_run, run_id)
            if run is None or run["status"] not in TERMINAL:
                continue
            async with self._locks[thread_id]:
                _, pending = await self._pending(thread_id)
                if not pending or pending.get("kind") != "await_run" or pending.get("run_id") != run_id:
                    continue
                detail = None
                if run["status"] == "failed":
                    steps = await asyncio.to_thread(self.store.steps, run_id)
                    detail = next((s["output"] for s in steps if s["status"] == "failed" and s["output"]), None)
                await self.graph.ainvoke(
                    Command(resume={"action": "run_ended", "run_id": run_id, "status": run["status"],
                                    "error": run["error"], "detail": detail}),
                    self._cfg(thread_id),
                )
                await self._pending(thread_id)
                woken += 1
        return woken

    # -- reading -----------------------------------------------------------

    async def view(self, thread_id: str) -> dict[str, Any]:
        async with self._locks[thread_id]:
            return await self._view_locked(thread_id)

    async def _view_locked(self, thread_id: str) -> dict[str, Any]:
        state, pending = await self._pending(thread_id)
        values = state.values
        if not values:
            raise UnknownThread(thread_id)
        run = None
        if values.get("run_id"):
            try:
                run = await self.tools.call("get_run", run_id=values["run_id"])
            except ToolError:
                run = None
        return {
            "thread_id": thread_id,
            "phase": values.get("phase"),
            "dataset_id": values.get("dataset_id"),
            "columns": (values.get("preview") or {}).get("columns", []),
            "config": values.get("config"),
            "pending": pending,
            "run": run,
            "transcript": values.get("transcript", []),
            "advisor": self.advisor.name,
        }
