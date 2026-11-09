import os
import subprocess
import sys
import time

import pytest

from dar import Store

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")


@pytest.fixture
def env(tmp_path):
    db = str(tmp_path / "dar.db")
    ws = str(tmp_path / "ws")
    os.makedirs(ws)
    return {"db": db, "ws": ws, "store": Store(db), "tmp": tmp_path}


@pytest.fixture
def spawn_worker(env):
    procs = []

    def spawn(worker_id, lease_ttl=1.0):
        p = subprocess.Popen(
            [sys.executable, "-m", "dar.worker", "--db", env["db"], "--workspace", env["ws"],
             "--id", worker_id, "--lease-ttl", str(lease_ttl)],
            env={**os.environ, "PYTHONPATH": SRC},
        )
        procs.append(p)
        return p

    yield spawn
    for p in procs:
        if p.poll() is None:
            p.kill()
            p.wait()


def wait_for(predicate, timeout=20.0, interval=0.02):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    raise AssertionError("condition not met within %.1fs" % timeout)


def event_types(store, run_id):
    return [e["type"] for e in store.events(run_id)]


def pid_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True
