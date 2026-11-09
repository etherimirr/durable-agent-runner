"""The tools as an MCP server.

    python -m harness.mcp_server        # stdio; reads DAR_DATA for its directories
"""
from __future__ import annotations

import os
from typing import Any

from mcp.server.mcpserver import MCPServer

from dar.store import Store

from .tools import ToolError, Tools


def _safe(fn, *args: Any, **kwargs: Any) -> dict[str, Any]:
    """A refused request is a normal result with an `error` field. MCP masks
    the text of raised exceptions, and the user needs to read this one."""
    try:
        return fn(*args, **kwargs)
    except ToolError as e:
        return {"error": str(e)}


def build_server(tools: Tools) -> MCPServer:
    server = MCPServer(
        "evaluation-analysis",
        instructions=(
            "Tools for analysing a CSV export of model scores and labels. preview_dataset is a bounded read. "
            "start_analysis queues a background run and returns at once; poll get_run. "
            "Call submit_report only after the user has approved the report."
        ),
    )

    @server.tool()
    def list_datasets() -> dict[str, Any]:
        """List the CSV files available for analysis."""
        return _safe(tools.list_datasets)

    @server.tool()
    def preview_dataset(dataset_id: str, max_rows: int = 20) -> dict[str, Any]:
        """Return the header and the first rows of a dataset. Reads at most 64 KB and never scans the file."""
        return _safe(tools.preview_dataset, dataset_id, max_rows=max_rows)

    @server.tool()
    def start_analysis(thread_id: str, dataset_id: str, config: dict[str, Any]) -> dict[str, Any]:
        """Queue the full analysis as a background run and return its id immediately.
        config needs score_column, label_column, positive_label, threshold and optionally slice_column.
        A thread has at most one active run; a second call returns the run that is already active."""
        return _safe(tools.start_analysis, thread_id, dataset_id, config)

    @server.tool()
    def get_run(run_id: str) -> dict[str, Any]:
        """Status of a run and of each of its steps."""
        return _safe(tools.get_run, run_id)

    @server.tool()
    def cancel_run(run_id: str) -> dict[str, Any]:
        """Ask a run to stop. A queued run stops at once, a running one within a heartbeat."""
        return _safe(tools.cancel_run, run_id)

    @server.tool()
    def read_report(run_id: str) -> dict[str, Any]:
        """The finished report of a succeeded run: settings, data profile and metrics."""
        return _safe(tools.read_report, run_id)

    @server.tool()
    def submit_report(run_id: str, summary_md: str) -> dict[str, Any]:
        """Publish the report, summary and enhanced CSV to the outbox. Only after explicit user approval."""
        return _safe(tools.submit_report, run_id, summary_md)

    return server


def tools_from_env() -> Tools:
    data = os.environ.get("DAR_DATA", "data")
    return Tools(
        Store(os.path.join(data, "dar.db")),
        datasets_dir=os.path.join(data, "datasets"),
        workspace=os.path.join(data, "ws"),
        outbox_dir=os.path.join(data, "outbox"),
    )


if __name__ == "__main__":
    os.makedirs(os.environ.get("DAR_DATA", "data"), exist_ok=True)
    build_server(tools_from_env()).run()
