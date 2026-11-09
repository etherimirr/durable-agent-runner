import asyncio
import csv
import os
import random
import types

import pytest
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from mcp import Client

from dar import Store, Worker
from harness.advisor import ClaudeAdvisor, Instruction, Proposal, RulesAdvisor
from harness.mcp_server import build_server
from harness.service import Harness, ThreadBusy
from harness.toolclient import DirectToolClient, McpToolClient
from harness.tools import ToolError, Tools


# -- fixtures -------------------------------------------------------------------

def make_csv(path, n=4000, seed=3):
    """An evaluation export with a known answer."""
    rng = random.Random(seed)
    rows = []
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["item_id", "region", "model_score", "gt_label", "note"])
        for i in range(n):
            positive = rng.random() < 0.3
            score = min(1.0, max(0.0, rng.gauss(0.68 if positive else 0.35, 0.18)))
            region = rng.choice(["us", "eu", "apac"])
            if i % 500 == 499:
                w.writerow([i, region, "", "violation" if positive else "ok", "missing score"])
                rows.append((None, positive, region))
            else:
                w.writerow([i, region, f"{score:.4f}", "violation" if positive else "ok", ""])
                rows.append((float(f"{score:.4f}"), positive, region))
    return rows


def expected(rows, threshold, region=None):
    tp = fp = fn = tn = 0
    for score, positive, r in rows:
        if score is None or (region and r != region):
            continue
        predicted = score >= threshold
        tp += predicted and positive
        fp += predicted and not positive
        fn += (not predicted) and positive
        tn += (not predicted) and not positive
    return {"tp": tp, "fp": fp, "fn": fn, "tn": tn}


@pytest.fixture
def world(tmp_path):
    data = tmp_path / "data"
    store = Store(str(tmp_path / "dar.db"))
    tools = Tools(store, str(data / "datasets"), str(data / "ws"), str(data / "outbox"))
    rows = make_csv(os.path.join(tools.datasets_dir, "export.csv"))
    return types.SimpleNamespace(store=store, tools=tools, rows=rows, cp=str(tmp_path / "harness.db"),
                                 worker=Worker(store, tools.workspace, "w1"), tmp=tmp_path)


def run(coro):
    return asyncio.run(coro)


async def settle(h, world):
    """Let the background side catch up: run queued work, then wake waiting threads."""
    while await asyncio.to_thread(world.worker.run_once):
        pass
    await h.reconcile_once()


def kinds(view):
    return [(e["role"], e["kind"]) for e in view["transcript"]]


def cells(report):
    at = report["metrics"]["at_threshold"]
    return {k: at[k] for k in ("tp", "fp", "fn", "tn")}


# -- the whole conversation -------------------------------------------------------

def test_preview_confirm_run_report_redo_submit(world):
    async def main():
        async with AsyncSqliteSaver.from_conn_string(world.cp) as cp:
            h = Harness(world.store, DirectToolClient(world.tools), RulesAdvisor(), cp)

            v = await h.start("t1", "export.csv")
            assert v["pending"]["kind"] == "confirm_config"
            assert v["config"] == {"score_column": "model_score", "label_column": "gt_label",
                                   "positive_label": "violation", "threshold": 0.5, "slice_column": None}
            assert kinds(v) == [("user", "start"), ("agent", "preview"), ("agent", "proposal")]
            assert len(v["transcript"][1]["rows"]) == 5
            assert world.store.latest_for_thread("t1") is None          # nothing ran yet

            v = await h.resume("t1", {"action": "message", "text": "slice by region"})
            assert v["pending"]["kind"] == "confirm_config" and v["config"]["slice_column"] == "region"

            v = await h.resume("t1", {"action": "confirm"})
            assert v["pending"]["kind"] == "await_run" and v["run"]["status"] == "queued"
            first_run = v["run"]["run_id"]

            await settle(h, world)
            v = await h.view("t1")
            assert v["pending"]["kind"] == "review_report" and v["phase"] == "review"
            report = v["transcript"][-1]["report"]
            assert cells(report) == expected(world.rows, 0.5)
            assert report["profile"]["rows"] == 4000 and report["profile"]["rows_without_numeric_score"] == 8
            by_region = {s["value"]: {k: s[k] for k in ("tp", "fp", "fn", "tn")} for s in report["metrics"]["slices"]}
            assert by_region == {r: expected(world.rows, 0.5, r) for r in ("us", "eu", "apac")}
            assert 0.8 < report["metrics"]["roc_auc_approx"] < 1.0
            assert "precision" in v["transcript"][-1]["text"]
            assert os.listdir(world.tools.outbox_dir) == []             # a report is not a submission

            v = await h.resume("t1", {"action": "message", "text": "set threshold to 0.7 and run again"})
            assert v["pending"]["kind"] == "await_run" and v["run"]["run_id"] != first_run
            await settle(h, world)
            v = await h.view("t1")
            assert cells(v["transcript"][-1]["report"]) == expected(world.rows, 0.7)

            v = await h.resume("t1", {"action": "submit"})
            assert v["phase"] == "submitted" and v["pending"] is None
            out = v["transcript"][-1]
            assert out["files"] == ["enhanced.csv", "report.json", "summary.md"]
            with open(os.path.join(out["path"], "enhanced.csv")) as f:
                outcomes = [r["_outcome"] for r in csv.DictReader(f)]
            want = expected(world.rows, 0.7)
            assert {k: outcomes.count(k.upper()) for k in want} == want and outcomes.count("no_score") == 8

            v = await h.start("t1", "export.csv")                       # a finished thread can start again
            assert v["pending"]["kind"] == "confirm_config"
    run(main())


def test_bad_input_never_starts_a_run(world):
    async def main():
        async with AsyncSqliteSaver.from_conn_string(world.cp) as cp:
            h = Harness(world.store, DirectToolClient(world.tools), RulesAdvisor(), cp)
            v = await h.start("t1", "export.csv")
            v = await h.resume("t1", {"action": "confirm", "config": {"score_column": "nope"}})
            assert v["pending"]["kind"] == "confirm_config" and "not a column" in v["transcript"][-1]["text"]
            v = await h.resume("t1", {"action": "confirm", "config": {"label_column": "model_score"}})
            assert "same column" in v["transcript"][-1]["text"]
            v = await h.resume("t1", {"action": "confirm", "config": {"threshold": "high"}})
            assert "must be a number" in v["transcript"][-1]["text"]
            assert v["config"]["score_column"] == "model_score"         # rejected changes were not kept
            assert world.store.latest_for_thread("t1") is None
            with pytest.raises(ThreadBusy):
                await h.start("t1", "export.csv")

            v = await h.start("t2", "../../etc/passwd")
            assert v["phase"] == "failed" and v["pending"] is None
            v = await h.start("t3", "missing.csv")
            assert v["phase"] == "failed" and "no dataset" in v["transcript"][-1]["text"]
    run(main())


def test_cancel_and_failed_run_return_to_the_settings(world):
    async def main():
        async with AsyncSqliteSaver.from_conn_string(world.cp) as cp:
            h = Harness(world.store, DirectToolClient(world.tools), RulesAdvisor(), cp)
            await h.start("t1", "export.csv")
            await h.resume("t1", {"action": "confirm"})
            v = await h.resume("t1", {"action": "message", "text": "how long will it take?"})
            assert v["pending"]["kind"] == "await_run" and "still running" in v["transcript"][-1]["text"]
            v = await h.resume("t1", {"action": "message", "text": "please cancel"})
            assert v["run"]["status"] == "cancelled" and v["pending"]["kind"] == "await_run"
            await h.reconcile_once()
            v = await h.view("t1")
            assert v["pending"]["kind"] == "confirm_config" and v["transcript"][-1]["kind"] == "run_ended"

            # A run that fails in the sandbox: the label column is fine in the header but the score is never numeric.
            v = await h.resume("t1", {"action": "confirm", "config": {"score_column": "region"}})
            await settle(h, world)
            v = await h.view("t1")
            assert v["pending"]["kind"] == "confirm_config"
            assert "no row has a numeric value" in v["transcript"][-1]["text"]
            assert v["run"]["status"] == "failed"
    run(main())


def test_restart_between_launch_and_result_loses_nothing(world):
    async def before():
        async with AsyncSqliteSaver.from_conn_string(world.cp) as cp:
            h = Harness(world.store, DirectToolClient(world.tools), RulesAdvisor(), cp)
            await h.start("t1", "export.csv")
            v = await h.resume("t1", {"action": "confirm"})
            return v["run"]["run_id"]

    async def after(run_id):
        async with AsyncSqliteSaver.from_conn_string(world.cp) as cp:
            h = Harness(world.store, DirectToolClient(world.tools), RulesAdvisor(), cp)   # a new process
            assert await h.reconcile_once() == 1
            v = await h.view("t1")
            assert v["pending"]["kind"] == "review_report" and v["run"]["run_id"] == run_id
            assert cells(v["transcript"][-1]["report"]) == expected(world.rows, 0.5)
            assert await h.reconcile_once() == 0

    run_id = run(before())
    while world.worker.run_once():          # the run finishes while the harness process is down
        pass
    run(after(run_id))


# -- tools ------------------------------------------------------------------------

def test_preview_is_a_bounded_read(world):
    big = os.path.join(world.tools.datasets_dir, "big.csv")
    with open(big, "w") as f:
        f.write("a,b\n")
        for i in range(400_000):
            f.write(f"{i},{i % 7}\n")
    p = world.tools.preview_dataset("big.csv")
    assert os.path.getsize(big) > 3_000_000
    assert p["bytes_read"] == 64 * 1024 and p["whole_file_read"] is False
    assert p["columns"] == ["a", "b"] and len(p["rows"]) == 20 and p["rows"][0] == ["0", "0"]
    for bad in ("../export.csv", "/etc/passwd", "", ".hidden"):
        with pytest.raises(ToolError):
            world.tools.preview_dataset(bad)


def test_same_conversation_over_mcp(world):
    async def main():
        async with AsyncSqliteSaver.from_conn_string(world.cp) as cp, Client(build_server(world.tools)) as client:
            listed = await client.list_tools()
            assert sorted(t.name for t in listed.tools) == sorted(
                ["list_datasets", "preview_dataset", "start_analysis", "get_run", "cancel_run", "read_report", "submit_report"])
            h = Harness(world.store, McpToolClient(client), RulesAdvisor(), cp)
            v = await h.start("t1", "export.csv")
            assert v["config"]["score_column"] == "model_score"
            await h.resume("t1", {"action": "confirm", "config": {"threshold": 0.6}})
            await settle(h, world)
            v = await h.view("t1")
            assert cells(v["transcript"][-1]["report"]) == expected(world.rows, 0.6)
            v = await h.resume("t1", {"action": "submit"})
            assert v["phase"] == "submitted"
            v = await h.start("t9", "missing.csv")                     # a tool error crosses MCP as a message
            assert v["phase"] == "failed" and "no dataset" in v["transcript"][-1]["text"]
    run(main())


# -- advisor ----------------------------------------------------------------------

COLUMNS = ["item_id", "region", "model_score", "gt_label", "note"]
CONFIG = {"score_column": "model_score", "label_column": "gt_label", "positive_label": "violation",
          "threshold": 0.5, "slice_column": None}


@pytest.mark.parametrize("text, action, changes", [
    ("threshold 0.8", "update", {"threshold": 0.8}),
    ("0.65", "update", {"threshold": 0.65}),
    ("阈值 0.3 然后开始", "run", {"threshold": 0.3}),
    ("slice by region and run", "run", {"slice_column": "region"}),
    ("use note as the label column", "update", {"label_column": "note"}),
    ("positive label is ok", "update", {"positive_label": "ok"}),
    ("looks good, submit it", "submit", {}),
    ("cancel", "cancel", {}),
    ("what does recall mean?", "unclear", {}),
])
def test_rules_understand_plain_instructions(text, action, changes):
    ins, source = RulesAdvisor().interpret(text, "confirm", CONFIG, COLUMNS)
    assert source == "rules" and ins.action == action
    got = {k: v for k, v in ins.model_dump(exclude={"action", "reply"}).items() if v is not None}
    assert got == changes


class FakeMessages:
    def __init__(self, reply):
        self.reply, self.calls = reply, []

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        out = self.reply(kwargs) if callable(self.reply) else self.reply
        if isinstance(out, Exception):
            raise out
        return out


def fake_client(reply):
    messages = FakeMessages(reply)
    return types.SimpleNamespace(beta=types.SimpleNamespace(messages=messages)), messages


def answer(parsed, stop_reason="end_turn"):
    return types.SimpleNamespace(parsed_output=parsed, stop_reason=stop_reason)


PREVIEW = {"columns": COLUMNS, "rows": [["1", "us", "0.91", "violation", ""], ["2", "eu", "0.12", "ok", ""]]}


def test_claude_advisor_request_shape_and_use_of_output():
    good = Proposal(score_column="model_score", label_column="gt_label", positive_label="violation",
                    threshold=0.6, slice_column="region", rationale="Scores look calibrated.")
    client, messages = fake_client(answer(good))
    config, why, source = ClaudeAdvisor(client).propose(PREVIEW)
    assert source == "claude" and config["threshold"] == 0.6 and config["slice_column"] == "region" and why == good.rationale
    call = messages.calls[0]
    assert call["model"] == "claude-opus-5-5"
    assert call["output_format"] is Proposal
    assert call["fallbacks"] == "default" and call["betas"] == ["server-side-fallback-2026-07-01"]
    assert call["output_config"] == {"effort": "low"}
    assert "thinking" not in call and "tool_choice" not in call and "temperature" not in call
    assert '"model_score"' in call["messages"][0]["content"]


def test_claude_advisor_falls_back_to_rules():
    import anthropic
    import httpx

    hallucinated = Proposal(score_column="confidence", label_column="gt_label", positive_label="violation",
                            threshold=0.5, slice_column=None, rationale="x")
    for reply in (answer(None, stop_reason="refusal"), answer(hallucinated), answer(None)):
        client, _ = fake_client(reply)
        config, _, source = ClaudeAdvisor(client).propose(PREVIEW)
        assert source == "rules" and config["score_column"] == "model_score"

    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    denied = anthropic.AuthenticationError("bad key", response=httpx.Response(401, request=request), body=None)
    client, messages = fake_client(denied)
    advisor = ClaudeAdvisor(client)
    assert advisor.propose(PREVIEW)[2] == "rules"
    assert advisor.interpret("threshold 0.9", "confirm", CONFIG, COLUMNS)[0].threshold == 0.9
    assert len(messages.calls) == 1 and advisor.disabled_reason        # it stops calling after a credential error

    busy = anthropic.RateLimitError("slow down", response=httpx.Response(429, request=request), body=None)
    client, messages = fake_client(busy)
    advisor = ClaudeAdvisor(client)
    assert advisor.propose(PREVIEW)[2] == "rules" and advisor.summarize is not None
    advisor.propose(PREVIEW)
    assert len(messages.calls) == 2 and advisor.disabled_reason is None  # a rate limit is not permanent


def test_claude_instruction_is_validated_against_the_columns():
    client, _ = fake_client(answer(Instruction(action="update", score_column="made_up", reply="ok")))
    ins, source = ClaudeAdvisor(client).interpret("use the other score", "confirm", CONFIG, COLUMNS)
    assert source == "rules" and ins.score_column is None
    client, _ = fake_client(answer(Instruction(action="run", threshold=0.75, reply="Running at 0.75.")))
    ins, source = ClaudeAdvisor(client).interpret("try 0.75", "review", CONFIG, COLUMNS)
    assert source == "claude" and ins.action == "run" and ins.threshold == 0.75
