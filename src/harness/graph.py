"""The agent as a LangGraph state machine.

    preview ─▶ confirm ─▶ launch ─▶ wait_run ─▶ report ─▶ review ─▶ submit
                 ▲  ▲                  │                    │
                 │  └── run failed ────┘                    │
                 └────────── redo with new settings ────────┘

Three nodes stop and wait: `confirm` (the user agrees on the settings),
`wait_run` (the background run finishes, or the user cancels) and `review`
(the user reads the report, then repeats the run or approves submission).
Each wait is a LangGraph interrupt, so the whole conversation is a checkpoint
on disk keyed by thread id and survives a restart of this process.

Nodes that wait do nothing before they wait, because LangGraph runs a node
again from its first line when it is resumed.
"""
from __future__ import annotations

import asyncio
import operator
import time
import uuid
from typing import Annotated, Any, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from .advisor import Advisor, Instruction
from .toolclient import ToolClient
from .tools import CONFIG_FIELDS, ToolError, clean_config, validate_config


class State(TypedDict, total=False):
    thread_id: str
    dataset_id: str
    preview: dict[str, Any]
    config: dict[str, Any]
    run_id: str
    report: dict[str, Any]
    summary: str
    phase: str
    transcript: Annotated[list[dict[str, Any]], operator.add]


def entry(role: str, kind: str, **payload: Any) -> dict[str, Any]:
    return {"id": uuid.uuid4().hex[:12], "role": role, "kind": kind, "ts": time.time(), **payload}


def say(text: str, source: str | None = None) -> dict[str, Any]:
    return entry("agent", "text", text=text, **({"source": source} if source else {}))


def merge(config: dict[str, Any], ins: Instruction) -> dict[str, Any]:
    changes = {f: getattr(ins, f) for f in CONFIG_FIELDS if getattr(ins, f) is not None}
    return {**config, **changes}


def build_graph(tools: ToolClient, advisor: Advisor) -> StateGraph:
    async def preview(state: State):
        try:
            p = await tools.call("preview_dataset", dataset_id=state["dataset_id"])
        except ToolError as e:
            return Command(goto=END, update={"phase": "failed", "transcript": [say(f"I could not open that file: {e}")]})
        config, why, source = await asyncio.to_thread(advisor.propose, p)
        return Command(goto="confirm", update={
            "preview": p,
            "config": config,
            "phase": "confirm",
            "transcript": [
                entry("agent", "preview", dataset_id=p["dataset_id"], columns=p["columns"], rows=p["rows"][:5],
                      bytes=p["bytes"], bytes_read=p["bytes_read"]),
                entry("agent", "proposal", config=config, text=why, source=source),
            ],
        })

    async def confirm(state: State):
        columns = state["preview"]["columns"]
        answer = interrupt({"kind": "confirm_config", "config": state["config"], "columns": columns})
        action = answer.get("action")
        if action == "confirm":
            config = {**state["config"], **(answer.get("config") or {})}
            errors = validate_config(config, columns)
            if errors:
                return Command(goto="confirm", update={"transcript": [say("I can't run with that: " + "; ".join(errors) + ".")]})
            config = clean_config(config)
            return Command(goto="launch", update={"config": config, "transcript": [entry("user", "confirmed", config=config)]})
        if action == "message":
            text = str(answer.get("text", ""))
            ins, source = await asyncio.to_thread(advisor.interpret, text, "confirm", state["config"], columns)
            heard = [entry("user", "text", text=text)]
            config = merge(state["config"], ins)
            errors = validate_config(config, columns)
            if errors:
                return Command(goto="confirm", update={"transcript": heard + [say("I can't use that: " + "; ".join(errors) + ".")]})
            config = clean_config(config)
            goto = "launch" if ins.action == "run" else "confirm"
            return Command(goto=goto, update={"config": config, "transcript": heard + [say(ins.reply, source)]})
        return Command(goto="confirm", update={"transcript": [say("That action is not available at this step.")]})

    async def launch(state: State):
        try:
            r = await tools.call("start_analysis", thread_id=state["thread_id"], dataset_id=state["dataset_id"],
                                 config=state["config"])
        except ToolError as e:
            return Command(goto="confirm", update={"phase": "confirm", "transcript": [say(f"I could not start the run: {e}")]})
        return Command(goto="wait_run", update={
            "run_id": r["run_id"],
            "phase": "running",
            "transcript": [entry("agent", "run", run_id=r["run_id"], config=state["config"])],
        })

    async def wait_run(state: State):
        run_id = state["run_id"]
        answer = interrupt({"kind": "await_run", "run_id": run_id})
        action = answer.get("action")
        if action == "run_ended":
            if answer.get("run_id") != run_id:
                return Command(goto="wait_run")
            if answer.get("status") == "succeeded":
                return Command(goto="report")
            reason = answer.get("error") or f"it was {answer.get('status')}"
            detail = (answer.get("detail") or "").strip().splitlines()
            text = f"The run did not finish: {reason}."
            if detail:
                text += f" The step said: {detail[-1][:300]}"
            return Command(goto="confirm", update={
                "phase": "confirm",
                "transcript": [entry("agent", "run_ended", run_id=run_id, status=answer.get("status"),
                                     text=text + " Change the settings or run it again.")],
            })
        heard = []
        if action == "message":
            text = str(answer.get("text", ""))
            heard = [entry("user", "text", text=text)]
            ins, _ = await asyncio.to_thread(advisor.interpret, text, "running", state["config"], state["preview"]["columns"])
            action = "cancel" if ins.action == "cancel" else None
        if action == "cancel":
            await tools.call("cancel_run", run_id=run_id)
            return Command(goto="wait_run", update={"transcript": heard + [entry("user", "cancel_requested", run_id=run_id)]})
        return Command(goto="wait_run", update={
            "transcript": heard + [say("The analysis is still running in the background. You can cancel it, or wait.")]})

    async def report(state: State):
        rep = await tools.call("read_report", run_id=state["run_id"])
        summary, source = await asyncio.to_thread(advisor.summarize, rep)
        return {
            "report": rep,
            "summary": summary,
            "phase": "review",
            "transcript": [entry("agent", "report", run_id=state["run_id"], report=rep, text=summary, source=source)],
        }

    async def review(state: State):
        columns = state["preview"]["columns"]
        answer = interrupt({"kind": "review_report", "run_id": state["run_id"], "config": state["config"], "columns": columns})
        action = answer.get("action")
        heard, source, reply = [], None, None
        config = state["config"]
        if action == "message":
            text = str(answer.get("text", ""))
            heard = [entry("user", "text", text=text)]
            ins, source = await asyncio.to_thread(advisor.interpret, text, "review", config, columns)
            config, reply = merge(config, ins), ins.reply
            action = {"run": "redo", "submit": "submit", "update": "update"}.get(ins.action)
        elif action == "redo":
            config = {**config, **(answer.get("config") or {})}
        if action == "submit":
            return Command(goto="submit", update={"transcript": heard + [entry("user", "approved", run_id=state["run_id"])]})
        if action in ("redo", "update"):
            errors = validate_config(config, columns)
            if errors:
                return Command(goto="review", update={"transcript": heard + [say("I can't use that: " + "; ".join(errors) + ".")]})
            config = clean_config(config)
            if action == "update":
                return Command(goto="review", update={"config": config, "transcript": heard + [
                    say("Settings updated. Say run to repeat the analysis with them.", source)]})
            return Command(goto="launch", update={"config": config, "transcript": heard + [entry("user", "redo", config=config)]})
        return Command(goto="review", update={"transcript": heard + [
            say(reply or "You can repeat the analysis with different settings, or approve the report for submission.", source)]})

    async def submit(state: State):
        r = await tools.call("submit_report", run_id=state["run_id"], summary_md=state["summary"])
        return {"phase": "submitted", "transcript": [entry("agent", "submitted", run_id=state["run_id"], path=r["path"], files=r["files"])]}

    g = StateGraph(State)
    for name, fn in (("preview", preview), ("confirm", confirm), ("launch", launch), ("wait_run", wait_run),
                     ("report", report), ("review", review), ("submit", submit)):
        g.add_node(name, fn)
    # preview, confirm, launch, wait_run and review choose their successor with
    # Command(goto=...). Only the unconditional hops are static edges.
    g.add_edge(START, "preview")
    g.add_edge("report", "review")
    g.add_edge("submit", END)
    return g
