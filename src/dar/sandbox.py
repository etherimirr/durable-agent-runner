"""Process sandbox for a step.

Each step runs in its own session and working directory, with a scrubbed
environment and CPU and file-size limits. Killing the session kills
everything the step started.

This is isolation against accidents, not a security boundary against hostile
code. A container backend would slot in behind the same three calls.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from dataclasses import dataclass


@dataclass
class StepProcess:
    proc: subprocess.Popen
    cwd: str

    def poll(self) -> int | None:
        return self.proc.poll()

    def kill(self) -> None:
        try:
            os.killpg(self.proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        self.proc.wait()

    def output(self, limit: int = 4000) -> str:
        parts = []
        for name in ("stdout.txt", "stderr.txt"):
            path = os.path.join(self.cwd, name)
            if os.path.exists(path):
                with open(path, "rb") as f:
                    f.seek(0, os.SEEK_END)
                    f.seek(max(0, f.tell() - limit))
                    text = f.read().decode("utf-8", "replace").strip()
                if text:
                    parts.append(text)
        return "\n".join(parts)


def step_argv(step: dict) -> list[str]:
    if "python" in step:
        return [sys.executable, "-I", "-c", step["python"]]
    return list(step["cmd"])


def start(step: dict, cwd: str, env: dict[str, str]) -> StepProcess:
    os.makedirs(cwd, exist_ok=True)
    spec = {
        "argv": step_argv(step),
        "cwd": cwd,
        "env": env,
        "cpu_seconds": int(step.get("cpu_seconds", 600)),
    }
    spec_path = os.path.join(cwd, ".step.json")
    with open(spec_path, "w") as f:
        json.dump(spec, f)
    child_env = dict(os.environ)
    src = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    child_env["PYTHONPATH"] = src + os.pathsep + child_env.get("PYTHONPATH", "")
    proc = subprocess.Popen(
        [sys.executable, "-m", "dar._child", spec_path],
        env=child_env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    return StepProcess(proc, cwd)
