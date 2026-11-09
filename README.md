# durable-agent-runner

A small runner for long agent jobs that have to survive the things that go wrong: a worker dies halfway, the user reloads the page, someone clicks Run twice, someone clicks Cancel.

An agent that analyzes a large file can run for minutes or hours. An HTTP request cannot stay open that long, and a process cannot be trusted to stay alive that long. This project separates the three things that usually get tangled together: accepting the request, doing the work, and telling the user how it is going.

```
browser ──POST /threads/{id}/runs──▶ API ──▶ 202 + run id          (milliseconds)
browser ◀──SSE, resumable by event id── API ◀── event log
                                               ▲
                              SQLite (runs, steps, events)
                                               ▲
                  worker A ── lease + heartbeat ──┤
                  worker B ── takes over when A's lease runs out
                       └── each step runs in its own process group
```

## What it guarantees

| Situation | What happens | Test |
|---|---|---|
| Worker is killed with SIGKILL mid-step | Another worker takes the run over when the lease expires. Finished steps are not run again. The interrupted step is run again from a clean directory. | `test_killed_worker_is_replaced_and_finished_steps_are_not_rerun` |
| The dead worker's step is still running | It is not. A supervisor process watches its parent and kills the step's process group when the worker disappears. | same test, 1.5 s deadline |
| A stalled worker wakes up after being replaced | Every write carries a fencing token. Its writes are rejected and it stops. | `test_stale_worker_cannot_write_after_its_lease_was_taken` |
| 16 workers race for one run | Exactly one gets it. | `test_only_one_of_many_racing_workers_claims_a_run` |
| Run is submitted twice for the same thread | The second request returns the run that is already active. | `test_one_active_run_per_thread` |
| Cancel while running | The step's process group is killed and the run ends as cancelled, within one heartbeat. | `test_cancel_running_run_kills_its_process` |
| Browser reconnects or reloads | It sends the last event id it saw and receives exactly the events after it. The thread id alone is enough to find the run again. | `test_http_submit_ack_stream_and_resume` |

Each of the first three was checked the other way round too: remove the mechanism, and the test fails.

## Measured

Apple silicon laptop, 14 cores, macOS, Python 3.12. Reproduce with `python bench/bench.py`.

Time from SIGKILL of the owning worker to another worker taking over, with a standby worker already polling:

| Lease | Trials | Median takeover | p95 | Max |
|---|---|---|---|---|
| 0.5 s | 10 | 0.44 s | 0.49 s | 0.49 s |
| 1 s | 10 | 0.88 s | 1.01 s | 1.01 s |
| 2 s | 10 | 1.70 s | 2.04 s | 2.04 s |
| 5 s | 10 | 4.06 s | 4.72 s | 4.72 s |

Takeover time is bounded by the lease length, because a worker renews every third of a lease. A shorter lease recovers faster and costs more heartbeat writes.

Throughput on trivial two-step runs:

| Workers | Runs | Runs per second |
|---|---|---|
| 1 | 200 | 6.7 |
| 2 | 200 | 13.5 |
| 4 | 200 | 25.9 |
| 8 | 200 | 50.8 |

It scales with workers up to 8. The per-run cost is almost entirely starting four processes (a supervisor and a step, twice), which is the right place for the cost to be when real steps take seconds or minutes.

## How it works

**Short ACK.** `POST /threads/{thread}/runs` writes one row and returns `202` with the run id. Nothing is executed in the request.

**Leases and fencing.** A worker claims the oldest run that is queued or whose lease has expired, inside a single write transaction. The claim increments the run's `attempt`, and the worker must present that number with every later write. A worker that was paused for longer than its lease finds that its number is stale and gives up without touching anything.

**Checkpoints.** A step runs in `steps/<n>.tmp-<attempt>/`. When it exits cleanly, the directory is renamed to `steps/<n>/` and then the step is marked done. If the worker dies between the two, the checkpoint wins and the directory is thrown away. Later steps read only from renamed directories, so they never see half-written output. On resume the worker skips steps marked done and reruns the first one that is not.

**One log.** Every state change writes its event in the same transaction. The SSE stream and the polling endpoint both read that log by sequence number, which is why reconnecting cannot lose or repeat an event.

**Cancellation.** A queued run is cancelled immediately. A running run gets a flag that its worker sees on the next heartbeat. The worker kills the step's process group and closes the run.

## Run it

```bash
pip install -e ".[dev]"
pytest -q
```

Start the API and one worker, then open http://127.0.0.1:8000.

```bash
DAR_DB=data/dar.db uvicorn --factory dar.api:app_from_env
```

```bash
python -m dar.worker --db data/dar.db --workspace data/ws --id a --lease-ttl 2
```

Press Run on the demo job, kill the worker while the `scan` step is running, and start a second worker with `--id b`. The panel shows the run taken over by `b` and finishing, with `scan` on its second attempt and the other steps on their first.

## API

| Method | Path | Purpose |
|---|---|---|
| POST | `/threads/{thread_id}/runs` | Submit a job. Returns `202` and the run. |
| GET | `/threads/{thread_id}` | Latest run of a thread, with its steps. |
| GET | `/runs/{run_id}` | A run and its steps. |
| POST | `/runs/{run_id}/cancel` | Request cancellation. |
| GET | `/runs/{run_id}/stream` | Server-sent events. Honors `Last-Event-ID`. |
| GET | `/runs/{run_id}/events?after=N` | The same events by polling. |

A job is a list of steps. Each step is either `python` source or a `cmd` argv, with an optional `timeout_s`.

## Limits

- The interrupted step runs again, so a step must be safe to repeat. Its files are isolated. Effects outside its directory, such as a call to another service, are not.
- State is in SQLite, which means one machine. The store is a single file, and the lease and fencing logic maps directly onto `SELECT ... FOR UPDATE SKIP LOCKED` in Postgres.
- The sandbox is a process group with a scrubbed environment and CPU and file-size limits. It protects against accidents. It is not a security boundary against hostile code, which would need a container or a microVM.
- A lease assumes clocks that move forward at roughly the same rate. All timestamps here come from one machine.

## License

MIT
