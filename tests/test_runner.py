import json
import os
import signal
import threading
import time

import pytest
from fastapi.testclient import TestClient

from dar import LeaseLost, Store, Worker
from dar.api import create_app

from conftest import event_types, pid_alive, wait_for


def py(name, code, **kw):
    return {"name": name, "python": code, **kw}


WRITE = "open('out.txt','w').write('{0}'); print('{0}')"
# Appends one line to a file outside the step directory every time it runs,
# then sleeps. The line count tells us how many times the step really executed.
COUNTED_SLEEP = (
    "import os,time\n"
    "open(os.path.join(os.environ['DAR_RUN_DIR'],'{0}.count'),'a').write(str(os.getpid())+'\\n')\n"
    "time.sleep({1})\n"
    "open('out.txt','w').write('{0}')\n"
)


# Hangs for 30 s the first time it runs and returns at once on a retry. If the
# first attempt were left running after its worker died, it would still be
# alive long after the test's deadline.
HANGS_ON_FIRST_ATTEMPT = (
    "import os,time\n"
    "p=os.path.join(os.environ['DAR_RUN_DIR'],'second.count')\n"
    "open(p,'a').write(str(os.getpid())+'\\n')\n"
    "time.sleep(30 if len(open(p).read().split())==1 else 0)\n"
    "open('out.txt','w').write('second')\n"
)


def status(store, run_id):
    return store.get_run(run_id)["status"]


# -- the basics ---------------------------------------------------------------

def test_runs_steps_in_order_and_later_steps_see_earlier_output(env):
    store = env["store"]
    job = {
        "input": {"n": 3},
        "steps": [
            py("load", "import json,os; n=json.load(open(os.environ['DAR_INPUT']))['n']; open('n.txt','w').write(str(n))"),
            py("square", "import os; n=int(open(os.path.join(os.environ['DAR_STEPS_DIR'],'0','n.txt')).read()); print(n*n)"),
        ],
    }
    run, created = store.submit("t1", job)
    assert created and run["status"] == "queued"

    assert Worker(store, env["ws"], "w1").run_once()

    assert status(store, run["id"]) == "succeeded"
    steps = store.steps(run["id"])
    assert [s["status"] for s in steps] == ["done", "done"]
    assert steps[1]["output"] == "9"
    assert event_types(store, run["id"]) == [
        "run.queued", "run.claimed",
        "step.started", "step.finished",
        "step.started", "step.finished",
        "run.succeeded",
    ]


def test_failed_step_fails_the_run_and_stops(env):
    store = env["store"]
    run, _ = store.submit("t1", {"steps": [py("boom", "raise SystemExit(3)"), py("never", "print(1)")]})
    Worker(store, env["ws"]).run_once()
    r = store.get_run(run["id"])
    assert r["status"] == "failed" and "exited with 3" in r["error"]
    assert len(store.steps(run["id"])) == 1


def test_step_timeout_kills_the_step(env):
    store = env["store"]
    run, _ = store.submit("t1", {"steps": [py("slow", "import time; time.sleep(30)", timeout_s=0.3)]})
    t0 = time.monotonic()
    Worker(store, env["ws"]).run_once()
    assert time.monotonic() - t0 < 5
    assert "timed out" in store.get_run(run["id"])["error"]


def test_one_active_run_per_thread(env):
    store = env["store"]
    job = {"steps": [py("a", "print(1)")]}
    first, created1 = store.submit("t1", job)
    second, created2 = store.submit("t1", job)
    assert created1 and not created2 and first["id"] == second["id"]
    other, created3 = store.submit("t2", job)
    assert created3 and other["id"] != first["id"]
    Worker(store, env["ws"]).run_once()
    Worker(store, env["ws"]).run_once()
    third, created4 = store.submit("t1", job)
    assert created4 and third["id"] != first["id"]


# -- crash recovery -------------------------------------------------------------

def test_killed_worker_is_replaced_and_finished_steps_are_not_rerun(env, spawn_worker):
    store = env["store"]
    job = {"steps": [
        py("first", COUNTED_SLEEP.format("first", 0)),
        py("second", HANGS_ON_FIRST_ATTEMPT),
        py("third", COUNTED_SLEEP.format("third", 0)),
    ]}
    run, _ = store.submit("t1", job)
    run_dir = os.path.join(env["ws"], run["id"])

    a = spawn_worker("worker-a", lease_ttl=1.0)
    count_file = os.path.join(run_dir, "second.count")
    wait_for(lambda: os.path.exists(count_file))
    step_pid = int(open(count_file).read().split()[0])

    a.send_signal(signal.SIGKILL)       # no cleanup, no goodbye
    a.wait()
    wait_for(lambda: not pid_alive(step_pid), timeout=1.5)   # its step must not outlive it

    spawn_worker("worker-b", lease_ttl=1.0)
    wait_for(lambda: status(store, run["id"]) == "succeeded")

    lines = lambda name: len(open(os.path.join(run_dir, name + ".count")).read().split())
    assert lines("first") == 1          # checkpointed, never repeated
    assert lines("second") == 2         # interrupted, repeated once
    assert lines("third") == 1
    attempts = {s["name"]: s["attempts"] for s in store.steps(run["id"])}
    assert attempts == {"first": 1, "second": 2, "third": 1}

    types = event_types(store, run["id"])
    assert types.count("run.recovered") == 1 and types[-1] == "run.succeeded"
    recovered = next(e for e in store.events(run["id"]) if e["type"] == "run.recovered")
    assert recovered["data"] == {"worker": "worker-b", "attempt": 2, "previous_owner": "worker-a"}
    # No leftovers from the dead attempt are visible to later steps.
    assert sorted(os.listdir(os.path.join(run_dir, "steps"))) == ["0", "1", "2"]


def test_stale_worker_cannot_write_after_its_lease_was_taken(env):
    """Fencing. Worker A stalls past its lease, B takes over, A wakes up."""
    store = env["store"]
    run, _ = store.submit("t1", {"steps": [py("a", "print(1)"), py("b", "print(2)")]})

    a = store.claim("worker-a", lease_ttl=0.05)
    store.step_started(a, 0, "a")
    time.sleep(0.1)                                   # A is stalled; lease runs out
    b = store.claim("worker-b", lease_ttl=5)
    assert b.run_id == a.run_id and b.token == a.token + 1 and b.recovered

    for write in (
        lambda: store.step_finished(a, 0, True, "from the past"),
        lambda: store.finish(a, "succeeded"),
        lambda: store.heartbeat(a, 5),
        lambda: store.step_started(a, 1, "b"),
    ):
        with pytest.raises(LeaseLost):
            write()

    assert status(store, run["id"]) == "running"
    assert store.steps(run["id"])[0]["output"] is None
    store.step_finished(b, 0, True, "from b")
    assert store.steps(run["id"])[0]["output"] == "from b"


def test_only_one_of_many_racing_workers_claims_a_run(env):
    store = env["store"]
    run, _ = store.submit("t1", {"steps": [py("a", "print(1)")]})
    start = threading.Barrier(16)
    winners = []

    def race(i):
        s = Store(env["db"])
        start.wait()
        c = s.claim(f"w{i}", lease_ttl=30)
        if c:
            winners.append(c)

    threads = [threading.Thread(target=race, args=(i,)) for i in range(16)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(winners) == 1 and winners[0].token == 1


# -- cancellation -----------------------------------------------------------------

def test_cancel_running_run_kills_its_process(env, spawn_worker):
    store = env["store"]
    run, _ = store.submit("t1", {"steps": [py("long", COUNTED_SLEEP.format("long", 60)), py("never", "print(1)")]})
    spawn_worker("w1", lease_ttl=0.6)
    count_file = os.path.join(env["ws"], run["id"], "long.count")
    wait_for(lambda: os.path.exists(count_file))
    step_pid = int(open(count_file).read().split()[0])

    t0 = time.monotonic()
    store.request_cancel(run["id"])
    wait_for(lambda: status(store, run["id"]) == "cancelled", timeout=5)
    assert time.monotonic() - t0 < 2
    wait_for(lambda: not pid_alive(step_pid), timeout=2)
    assert event_types(store, run["id"])[-2:] == ["run.cancel_requested", "run.cancelled"]
    assert [s["name"] for s in store.steps(run["id"])] == ["long"]


def test_cancel_queued_run_is_immediate(env):
    store = env["store"]
    run, _ = store.submit("t1", {"steps": [py("a", "print(1)")]})
    assert store.request_cancel(run["id"])["status"] == "cancelled"
    assert store.claim("w1", 5) is None


def test_cancel_requested_then_worker_dies(env):
    store = env["store"]
    run, _ = store.submit("t1", {"steps": [py("a", "print(1)")]})
    store.claim("w1", lease_ttl=0.05)
    store.request_cancel(run["id"])
    time.sleep(0.1)
    assert store.claim("w2", 5) is None            # nothing to resume
    assert status(store, run["id"]) == "cancelled"


# -- HTTP -------------------------------------------------------------------------

def sse(text):
    out = []
    for block in text.strip().split("\n\n"):
        fields = dict(line.split(": ", 1) for line in block.split("\n") if ": " in line and not line.startswith(":"))
        if "id" in fields:
            out.append((int(fields["id"]), fields["event"], json.loads(fields["data"])))
    return out


def test_http_submit_ack_stream_and_resume(env):
    store = env["store"]
    client = TestClient(create_app(store))
    job = {"steps": [py("a", WRITE.format("a")), py("b", WRITE.format("b"))]}

    r = client.post("/threads/t1/runs", json=job)
    assert r.status_code == 202 and r.json()["created"] is True
    run_id = r.json()["run"]["id"]
    assert client.post("/threads/t1/runs", json=job).json() == {"run": r.json()["run"], "created": False}

    Worker(store, env["ws"]).run_once()

    full = sse(client.get(f"/runs/{run_id}/stream").text)
    assert [e[1] for e in full][0] == "run.queued" and full[-1][1] == "run.succeeded"
    seqs = [e[0] for e in full]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)

    # Reconnect in the middle: exactly the events after the cursor, no gap, no repeat.
    cut = full[3][0]
    rest = sse(client.get(f"/runs/{run_id}/stream", headers={"Last-Event-ID": str(cut)}).text)
    assert rest == full[4:]
    polled = client.get(f"/runs/{run_id}/events", params={"after": cut}).json()["events"]
    assert [e["seq"] for e in polled] == [e[0] for e in full[4:]]

    # A reloaded page finds its run again from the thread id alone.
    t = client.get("/threads/t1").json()
    assert t["run"]["id"] == run_id and t["run"]["status"] == "succeeded"
    assert [s["output"] for s in t["steps"]] == ["a", "b"]


def test_http_validation_and_404(env):
    client = TestClient(create_app(env["store"]))
    assert client.post("/threads/t1/runs", json={"steps": []}).status_code == 422
    assert client.post("/threads/t1/runs", json={"steps": [{"name": "x"}]}).status_code == 422
    assert client.get("/runs/nope").status_code == 404
    assert client.post("/runs/nope/cancel").status_code == 404
    assert client.get("/threads/empty").status_code == 404
