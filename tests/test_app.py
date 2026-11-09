import os
import threading
import time

import pytest
from fastapi.testclient import TestClient

from dar import Store, Worker
from harness.advisor import RulesAdvisor
from harness.app import create_app


def until(client, thread_id, kind, timeout=30.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        v = client.get(f"/api/threads/{thread_id}").json()
        if (v.get("pending") or {}).get("kind") == kind:
            return v
        time.sleep(0.05)
    raise AssertionError(f"thread never reached {kind}; last view: {v}")


@pytest.mark.parametrize("mode", ["mcp", "direct"])
def test_http_conversation_end_to_end(tmp_path, mode):
    data = str(tmp_path / "data")
    app = create_app(data, tools_mode=mode, advisor=RulesAdvisor(), reconcile_every=0.05)
    stop = threading.Event()
    worker = threading.Thread(
        target=lambda: Worker(Store(os.path.join(data, "dar.db")), os.path.join(data, "ws"), "w1", lease_ttl=2).run_forever(stop),
        daemon=True,
    )
    with TestClient(app) as client:
        assert client.get("/api/info").json() == {"advisor": "rules", "tools": mode, "model": None}

        made = client.post("/api/datasets/sample", params={"rows": 3000})
        assert made.status_code == 201 and made.json()["dataset_id"] == "sample_3000.csv"
        up = client.post("/api/datasets", files={"file": ("my export (1).csv", b"score,label\n0.9,1\n0.1,0\n")})
        assert up.json() == {"dataset_id": "my_export__1_.csv", "bytes": 24}
        assert [d["dataset_id"] for d in client.get("/api/datasets").json()["datasets"]] == ["my_export__1_.csv", "sample_3000.csv"]

        v = client.post("/api/threads/t1/start", json={"dataset_id": "sample_3000.csv"}).json()
        assert v["pending"]["kind"] == "confirm_config" and v["config"]["score_column"] == "model_score"
        assert client.post("/api/threads/t1/start", json={"dataset_id": "sample_3000.csv"}).status_code == 409

        v = client.post("/api/threads/t1/resume", json={"action": "confirm", "config": {"slice_column": "region"}}).json()
        assert v["pending"]["kind"] == "await_run"
        worker.start()                                   # the run is picked up only now
        v = until(client, "t1", "review_report")         # nobody asked: the reconciler woke the thread
        report = v["transcript"][-1]["report"]
        assert report["profile"]["rows"] == 3000 and len(report["metrics"]["slices"]) == 4
        assert v["run"]["status"] == "succeeded" and [s["status"] for s in v["run"]["steps"]] == ["done"] * 4

        v = client.post("/api/threads/t1/resume", json={"action": "submit"}).json()
        assert v["phase"] == "submitted"
        assert sorted(os.listdir(v["transcript"][-1]["path"])) == ["enhanced.csv", "report.json", "summary.md"]

        assert client.post("/api/threads/t1/resume", json={"action": "submit"}).status_code == 409
        assert client.get("/api/threads/nobody").status_code == 404
        assert client.get("/api/threads/bad id!").status_code in (400, 404)
        assert client.post("/api/datasets", files={"file": ("e.csv", b"  ")}).status_code == 400
        # The runner's own API is still there underneath.
        run_id = v["run"]["run_id"]
        assert client.get(f"/runs/{run_id}").json()["run"]["status"] == "succeeded"
    stop.set()
