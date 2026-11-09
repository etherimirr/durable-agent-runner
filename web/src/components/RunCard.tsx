import type { RunView } from "../types";

const LABEL: Record<string, string> = {
  profile: "Profile the file",
  metrics: "Compute metrics",
  enhance: "Write enhanced CSV",
  report: "Assemble report",
};

export function RunCard({ run, onCancel, busy }: { run: RunView; onCancel?: () => void; busy: boolean }) {
  const byIdx = new Map(run.steps.map((s) => [s.idx, s]));
  const active = run.status === "queued" || run.status === "running";
  return (
    <div className="run">
      <div className="run-head">
        <span className={`badge ${run.status}`}>{run.status}</span>
        <span className="muted">run {run.run_id.slice(0, 8)}</span>
        {run.attempt > 1 && <span className="muted">taken over after a worker was lost, attempt {run.attempt}</span>}
        {active && onCancel && <button onClick={onCancel} disabled={busy}>Cancel</button>}
      </div>
      <ol className="steps">
        {run.step_names.map((name, idx) => {
          const s = byIdx.get(idx);
          const status = s ? s.status : run.status === "queued" || run.status === "running" ? "waiting" : "skipped";
          return (
            <li key={name} className={status}>
              <span className="dot" aria-hidden="true" />
              <span className="step-name">{LABEL[name] ?? name}</span>
              <span className="step-out">
                {s?.output ? s.output.split("\n").slice(-1)[0] : status}
                {s && s.attempts > 1 ? ` · attempt ${s.attempts}` : ""}
              </span>
            </li>
          );
        })}
      </ol>
      {run.error && <p className="error">{run.error}</p>}
    </div>
  );
}
