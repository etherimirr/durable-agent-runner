"""Supervisor for one step. Run as `python -m dar._child <spec.json>`.

It starts the step as a child, applies resource limits, and watches its own
parent. If the worker that launched it disappears (crash, SIGKILL), the
supervisor kills the whole process group, so no orphaned step keeps running
after its worker is gone.
"""
from __future__ import annotations

import json
import os
import resource
import signal
import subprocess
import sys
import time


def _limits(cpu_seconds: int, max_file_bytes: int):
    def apply() -> None:
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds))
        resource.setrlimit(resource.RLIMIT_FSIZE, (max_file_bytes, max_file_bytes))
    return apply


def main() -> int:
    spec = json.loads(open(sys.argv[1]).read())
    parent = os.getppid()
    cwd = spec["cwd"]
    env = {
        "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
        "HOME": cwd,
        "TMPDIR": cwd,
        "LANG": "C.UTF-8",
        **spec.get("env", {}),
    }
    with open(os.path.join(cwd, "stdout.txt"), "wb") as out, open(os.path.join(cwd, "stderr.txt"), "wb") as err:
        proc = subprocess.Popen(
            spec["argv"],
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=err,
            preexec_fn=_limits(spec.get("cpu_seconds", 600), spec.get("max_file_bytes", 512 * 1024 * 1024)),
        )
        while True:
            code = proc.poll()
            if code is not None:
                return code if code >= 0 else 128 - code
            if os.getppid() != parent:
                os.killpg(os.getpgrp(), signal.SIGKILL)
            time.sleep(0.02)


if __name__ == "__main__":
    sys.exit(main())
