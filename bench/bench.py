"""Measure the two numbers that matter for this design.

1. Recovery time: from SIGKILL of the worker that owns a run to another
   worker taking it over, for several lease lengths.
2. Throughput: short runs per second as workers are added.

    python bench/bench.py            # prints two markdown tables
"""
from __future__ import annotations

import os
import platform
import signal
import statistics
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from dar import Store  # noqa: E402

HANG_FIRST = (
    "import os,time\n"
    "p=os.path.join(os.environ['DAR_RUN_DIR'],'n')\n"
    "open(p,'a').write('x')\n"
    "time.sleep(60 if len(open(p).read())==1 else 0)\n"
)


def worker(db, ws, wid, ttl):
    return subprocess.Popen(
        [sys.executable, "-m", "dar.worker", "--db", db, "--workspace", ws, "--id", wid, "--lease-ttl", str(ttl)],
        env={**os.environ, "PYTHONPATH": os.path.join(ROOT, "src")},
    )


def wait(pred, timeout=120):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        v = pred()
        if v:
            return v
        time.sleep(0.005)
    raise TimeoutError


def recovery(ttl: float, trials: int) -> list[float]:
    out = []
    for i in range(trials):
        with tempfile.TemporaryDirectory() as d:
            db, ws = os.path.join(d, "dar.db"), os.path.join(d, "ws")
            os.makedirs(ws)
            store = Store(db)
            run, _ = store.submit("t", {"steps": [{"name": "hang", "python": HANG_FIRST}]})
            a = worker(db, ws, "a", ttl)
            wait(lambda: any(e["type"] == "step.started" for e in store.events(run["id"])))
            b = worker(db, ws, "b", ttl)      # standby, already polling
            time.sleep(0.3 + (i % 5) * ttl / 15)   # kill at different points in the heartbeat cycle
            a.send_signal(signal.SIGKILL)
            killed = time.time()
            a.wait()
            ev = wait(lambda: next((e for e in store.events(run["id"]) if e["type"] == "run.recovered"), None))
            out.append(ev["ts"] - killed)
            wait(lambda: store.get_run(run["id"])["status"] == "succeeded")
            b.kill()
            b.wait()
    return out


def throughput(workers: int, runs: int) -> float:
    with tempfile.TemporaryDirectory() as d:
        db, ws = os.path.join(d, "dar.db"), os.path.join(d, "ws")
        os.makedirs(ws)
        store = Store(db)
        job = {"steps": [{"name": "a", "python": "pass"}, {"name": "b", "python": "pass"}]}
        ids = [store.submit(f"t{i}", job)[0]["id"] for i in range(runs)]
        t0 = time.monotonic()
        procs = [worker(db, ws, f"w{i}", 5) for i in range(workers)]
        wait(lambda: all(store.get_run(i)["status"] == "succeeded" for i in ids[-workers * 2:])
             and store._conn().execute("SELECT COUNT(*) FROM runs WHERE status!='succeeded'").fetchone()[0] == 0)
        dt = time.monotonic() - t0
        for p in procs:
            p.kill()
            p.wait()
        return runs / dt


def pct(xs, q):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(q * (len(xs) - 1))))]


def main():
    print(f"Machine: {platform.machine()}, {os.cpu_count()} cores, {platform.system()} {platform.release()}, "
          f"Python {platform.python_version()}\n")
    print("| Lease | Trials | Median takeover | p95 | Max |")
    print("|---|---|---|---|---|")
    for ttl in (0.5, 1.0, 2.0, 5.0):
        xs = recovery(ttl, 10)
        print(f"| {ttl:g} s | {len(xs)} | {statistics.median(xs):.2f} s | {pct(xs, .95):.2f} s | {max(xs):.2f} s |", flush=True)
    print()
    print("| Workers | Runs | Runs per second |")
    print("|---|---|---|")
    for w in (1, 2, 4, 8):
        print(f"| {w} | 200 | {throughput(w, 200):.1f} |", flush=True)


if __name__ == "__main__":
    main()
