"""The agent's tools.

Two kinds, and the split is the point of the design:

* quick reads (`preview_dataset`, `get_run`, `read_report`) answer inside the
  request. `preview_dataset` reads a bounded prefix of the file and never
  scans it.
* anything that touches the whole file goes through `start_analysis`, which
  only queues a durable run and returns its id.

Nothing leaves the workspace until `submit_report` is called, and the graph
only calls that after the user has approved the report.
"""
from __future__ import annotations

import csv
import io
import json
import os
import re
import shutil
import sys
from typing import Any

from dar.store import Store

STEPS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "steps")
ANALYSIS_STEPS = ["profile", "metrics", "enhance", "report"]
REPORT_STEP = ANALYSIS_STEPS.index("report")
ENHANCE_STEP = ANALYSIS_STEPS.index("enhance")
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,120}$")

CONFIG_FIELDS = ("score_column", "label_column", "positive_label", "threshold", "slice_column")


class ToolError(Exception):
    """A tool refused the request. The message is safe to show to the user."""


def validate_config(config: dict[str, Any], columns: list[str]) -> list[str]:
    errors = []
    for field in ("score_column", "label_column"):
        value = config.get(field)
        if not value:
            errors.append(f"{field} is missing")
        elif value not in columns:
            errors.append(f"{field} {value!r} is not a column in this file")
    if config.get("score_column") and config.get("score_column") == config.get("label_column"):
        errors.append("score_column and label_column are the same column")
    if config.get("slice_column") and config["slice_column"] not in columns:
        errors.append(f"slice_column {config['slice_column']!r} is not a column in this file")
    if config.get("positive_label") in (None, ""):
        errors.append("positive_label is missing")
    try:
        float(config.get("threshold"))
    except (TypeError, ValueError):
        errors.append("threshold must be a number")
    return errors


def clean_config(config: dict[str, Any]) -> dict[str, Any]:
    out = {k: config.get(k) for k in CONFIG_FIELDS}
    out["threshold"] = float(out["threshold"])
    out["positive_label"] = str(out["positive_label"]).strip()
    out["slice_column"] = out["slice_column"] or None
    return out


class Tools:
    def __init__(self, store: Store, datasets_dir: str, workspace: str, outbox_dir: str):
        self.store = store
        self.datasets_dir = datasets_dir
        self.workspace = workspace
        self.outbox_dir = outbox_dir
        for d in (datasets_dir, workspace, outbox_dir):
            os.makedirs(d, exist_ok=True)

    # -- datasets ----------------------------------------------------------

    def _dataset_path(self, dataset_id: str) -> str:
        if not _ID.match(dataset_id or ""):
            raise ToolError("invalid dataset id")
        path = os.path.join(self.datasets_dir, dataset_id)
        if not os.path.isfile(path):
            raise ToolError(f"no dataset named {dataset_id!r}")
        return path

    def list_datasets(self) -> dict[str, Any]:
        items = []
        for name in sorted(os.listdir(self.datasets_dir)):
            path = os.path.join(self.datasets_dir, name)
            if os.path.isfile(path) and _ID.match(name):
                items.append({"dataset_id": name, "bytes": os.path.getsize(path)})
        return {"datasets": items}

    def save_dataset(self, name: str, data: bytes) -> dict[str, Any]:
        base = re.sub(r"[^A-Za-z0-9_.-]", "_", os.path.basename(name or "upload.csv")).lstrip(".") or "upload.csv"
        if not base.lower().endswith(".csv"):
            base += ".csv"
        path = os.path.join(self.datasets_dir, base)
        tmp = path + ".part"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
        return {"dataset_id": base, "bytes": len(data)}

    def preview_dataset(self, dataset_id: str, max_rows: int = 20, max_bytes: int = 64 * 1024) -> dict[str, Any]:
        """Header and the first rows. Reads at most `max_bytes` of the file."""
        path = self._dataset_path(dataset_id)
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            chunk = f.read(max_bytes)
        truncated = size > len(chunk)
        text = chunk.decode("utf-8-sig", "replace")
        if truncated and "\n" in text:
            text = text[: text.rindex("\n")]          # drop the cut-off last line
        reader = csv.reader(io.StringIO(text))
        try:
            columns = next(reader)
        except StopIteration:
            raise ToolError("the file is empty")
        rows = []
        for row in reader:
            if len(rows) >= max_rows:
                break
            if row:
                rows.append((row + [""] * len(columns))[: len(columns)])
        return {
            "dataset_id": dataset_id,
            "bytes": size,
            "columns": [c.strip() for c in columns],
            "rows": rows,
            "bytes_read": len(chunk),
            "whole_file_read": not truncated,
        }

    # -- analysis runs -----------------------------------------------------

    def start_analysis(self, thread_id: str, dataset_id: str, config: dict[str, Any]) -> dict[str, Any]:
        path = self._dataset_path(dataset_id)
        columns = self.preview_dataset(dataset_id, max_rows=0)["columns"]
        errors = validate_config(config, columns)
        if errors:
            raise ToolError("; ".join(errors))
        job = {
            "input": {"dataset_path": os.path.abspath(path), "dataset_id": dataset_id, "config": clean_config(config)},
            "steps": [
                {"name": name, "cmd": [sys.executable, "-I", os.path.join(STEPS_DIR, name + ".py")]}
                for name in ANALYSIS_STEPS
            ],
        }
        run, created = self.store.submit(thread_id, job)
        return {"run_id": run["id"], "created": created, "status": run["status"]}

    def get_run(self, run_id: str) -> dict[str, Any]:
        run = self.store.get_run(run_id)
        if run is None:
            raise ToolError("unknown run")
        steps = [
            {k: s[k] for k in ("idx", "name", "status", "attempts", "output")}
            for s in self.store.steps(run_id)
        ]
        names = [s["name"] for s in run["job"]["steps"]]
        return {
            "run_id": run_id,
            "thread_id": run["thread_id"],
            "status": run["status"],
            "attempt": run["attempt"],
            "error": run["error"],
            "step_names": names,
            "steps": steps,
            "config": run["job"].get("input", {}).get("config"),
        }

    def cancel_run(self, run_id: str) -> dict[str, Any]:
        run = self.store.request_cancel(run_id)
        if run is None:
            raise ToolError("unknown run")
        return {"run_id": run_id, "status": run["status"]}

    def _succeeded_run(self, run_id: str) -> dict[str, Any]:
        run = self.store.get_run(run_id)
        if run is None:
            raise ToolError("unknown run")
        if run["status"] != "succeeded":
            raise ToolError(f"run is {run['status']}, not succeeded")
        return run

    def read_report(self, run_id: str) -> dict[str, Any]:
        self._succeeded_run(run_id)
        path = os.path.join(self.workspace, run_id, "steps", str(REPORT_STEP), "report.json")
        with open(path) as f:
            return json.load(f)

    def submit_report(self, run_id: str, summary_md: str) -> dict[str, Any]:
        """Copy the report, the summary and the enhanced file to the outbox."""
        run = self._succeeded_run(run_id)
        steps = os.path.join(self.workspace, run_id, "steps")
        dest = os.path.join(self.outbox_dir, re.sub(r"[^A-Za-z0-9_.-]", "_", run["thread_id"]), run_id)
        tmp = dest + ".part"
        shutil.rmtree(tmp, ignore_errors=True)
        os.makedirs(tmp)
        shutil.copy(os.path.join(steps, str(REPORT_STEP), "report.json"), os.path.join(tmp, "report.json"))
        shutil.copy(os.path.join(steps, str(ENHANCE_STEP), "enhanced.csv"), os.path.join(tmp, "enhanced.csv"))
        with open(os.path.join(tmp, "summary.md"), "w", encoding="utf-8") as f:
            f.write(summary_md)
        shutil.rmtree(dest, ignore_errors=True)
        os.replace(tmp, dest)
        return {"run_id": run_id, "path": dest, "files": sorted(os.listdir(dest))}


TOOL_NAMES = ("list_datasets", "preview_dataset", "start_analysis", "get_run", "cancel_run", "read_report", "submit_report")
