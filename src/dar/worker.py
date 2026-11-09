"""Worker: claims runs and executes their steps.

Resume rule: a step that was checkpointed as done is never run again. The
step that was in flight when a worker died is run again from scratch, in a
fresh directory, and the half-written directory of the dead attempt is
ignored. A step's files become visible to later steps only through an atomic
rename that happens right before its checkpoint.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import threading
import time
import uuid

from . import sandbox
from .store import Claim, LeaseLost, Store


class Worker:
    def __init__(
        self,
        store: Store,
        workspace: str,
        worker_id: str | None = None,
        lease_ttl: float = 5.0,
        poll_interval: float = 0.1,
    ):
        self.store = store
        self.workspace = workspace
        self.worker_id = worker_id or f"w-{uuid.uuid4().hex[:8]}"
        self.lease_ttl = lease_ttl
        self.poll_interval = poll_interval

    def run_forever(self, stop: threading.Event | None = None) -> None:
        while stop is None or not stop.is_set():
            if not self.run_once():
                time.sleep(self.poll_interval)

    def run_once(self) -> bool:
        claim = self.store.claim(self.worker_id, self.lease_ttl)
        if claim is None:
            return False
        try:
            self._execute(claim)
        except LeaseLost:
            pass  # someone else owns the run now; they will finish it
        return True

    # ---------------------------------------------------------------------

    def _execute(self, claim: Claim) -> None:
        run_dir = os.path.join(self.workspace, claim.run_id)
        steps_dir = os.path.join(run_dir, "steps")
        os.makedirs(steps_dir, exist_ok=True)
        input_path = os.path.join(run_dir, "input.json")
        if not os.path.exists(input_path):
            tmp = input_path + f".tmp-{claim.token}"
            with open(tmp, "w") as f:
                json.dump(claim.job.get("input", {}), f)
            os.replace(tmp, input_path)

        done = {s["idx"] for s in self.store.steps(claim.run_id) if s["status"] == "done"}
        default_timeout = float(claim.job.get("timeout_s", 3600))

        for idx, step in enumerate(claim.job["steps"]):
            if idx in done:
                continue
            if self.store.heartbeat(claim, self.lease_ttl):
                self.store.finish(claim, "cancelled")
                return

            final_dir = os.path.join(steps_dir, str(idx))
            work_dir = os.path.join(steps_dir, f"{idx}.tmp-{claim.token}")
            # Clear what earlier attempts left behind. final_dir can exist
            # without a checkpoint if a worker died between the rename and the
            # commit. The checkpoint is the truth, so the directory goes.
            shutil.rmtree(final_dir, ignore_errors=True)
            for name in os.listdir(steps_dir):
                if name.startswith(f"{idx}.tmp-"):
                    shutil.rmtree(os.path.join(steps_dir, name), ignore_errors=True)

            self.store.step_started(claim, idx, step["name"])
            proc = sandbox.start(
                step,
                work_dir,
                {"DAR_RUN_DIR": run_dir, "DAR_STEPS_DIR": steps_dir, "DAR_INPUT": input_path},
            )
            outcome = self._wait(claim, proc, float(step.get("timeout_s", default_timeout)))

            if outcome == "lost":
                raise LeaseLost(claim.run_id)
            if outcome == "cancelled":
                self.store.finish(claim, "cancelled")
                return
            if outcome == "timeout":
                self.store.step_finished(claim, idx, False, proc.output())
                self.store.finish(claim, "failed", f"step {idx} ({step['name']}) timed out")
                return
            if outcome != 0:
                self.store.step_finished(claim, idx, False, proc.output())
                self.store.finish(claim, "failed", f"step {idx} ({step['name']}) exited with {outcome}")
                return

            output = proc.output()
            os.replace(work_dir, final_dir)
            self.store.step_finished(claim, idx, True, output)

        self.store.finish(claim, "succeeded")

    def _wait(self, claim: Claim, proc: sandbox.StepProcess, timeout: float):
        """Wait for the step while keeping the lease alive.
        Returns the exit code, or 'cancelled', 'timeout', 'lost'."""
        started = time.monotonic()
        beat_every = self.lease_ttl / 3
        next_beat = started + beat_every
        while True:
            code = proc.poll()
            if code is not None:
                return code
            now = time.monotonic()
            if now - started > timeout:
                proc.kill()
                return "timeout"
            if now >= next_beat:
                next_beat = now + beat_every
                try:
                    if self.store.heartbeat(claim, self.lease_ttl):
                        proc.kill()
                        return "cancelled"
                except LeaseLost:
                    proc.kill()
                    return "lost"
            time.sleep(0.02)


def main() -> None:
    p = argparse.ArgumentParser(description="durable-agent-runner worker")
    p.add_argument("--db", required=True)
    p.add_argument("--workspace", required=True)
    p.add_argument("--id", default=None)
    p.add_argument("--lease-ttl", type=float, default=5.0)
    a = p.parse_args()
    Worker(Store(a.db), a.workspace, a.id, a.lease_ttl).run_forever()


if __name__ == "__main__":
    main()
