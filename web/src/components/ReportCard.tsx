import { fixed, num, pct } from "../format";
import type { Report } from "../types";
import { SweepChart } from "./SweepChart";

export function ReportCard({ report }: { report: Report }) {
  const { metrics: m, profile: p, config } = report;
  const at = m.at_threshold;
  const total = at.tp + at.fp + at.fn + at.tn;
  const share = (n: number) => pct(total ? n / total : null);
  const better = m.best_f1.f1 != null && at.f1 != null && m.best_f1.f1 - at.f1 > 0.01;
  return (
    <div className="report">
      <div className="tiles">
        <div><span>Precision</span><b>{pct(at.precision)}</b></div>
        <div><span>Recall</span><b>{pct(at.recall)}</b></div>
        <div><span>F1</span><b>{fixed(at.f1)}</b></div>
        <div><span>ROC AUC, approx.</span><b>{fixed(m.roc_auc_approx)}</b></div>
      </div>
      <p className="muted small">
        {num(p.scored_rows)} scored rows at threshold {config.threshold}: {num(m.positives)} positive, {num(m.negatives)} negative.
        {p.rows_without_numeric_score > 0 && ` ${num(p.rows_without_numeric_score)} rows had no numeric score and were left out.`}
        {better && ` Best F1 in the sweep is ${fixed(m.best_f1.f1)} near threshold ${m.best_f1.threshold}.`}
      </p>

      {m.curve.length > 1 && <SweepChart curve={m.curve} threshold={config.threshold} />}

      <div className="split">
        <div>
          <h4>Confusion matrix</h4>
          <table className="matrix">
            <thead>
              <tr><th /><th>Predicted positive</th><th>Predicted negative</th></tr>
            </thead>
            <tbody>
              <tr>
                <th>Actually positive</th>
                <td><b>{num(at.tp)}</b><span>true positive · {share(at.tp)}</span></td>
                <td><b>{num(at.fn)}</b><span>false negative · {share(at.fn)}</span></td>
              </tr>
              <tr>
                <th>Actually negative</th>
                <td><b>{num(at.fp)}</b><span>false positive · {share(at.fp)}</span></td>
                <td><b>{num(at.tn)}</b><span>true negative · {share(at.tn)}</span></td>
              </tr>
            </tbody>
          </table>
        </div>
        {m.slices.length > 0 && (
          <div>
            <h4>By {m.slice_column}{m.slice_values_total > m.slices.length ? ` (top ${m.slices.length} of ${m.slice_values_total})` : ""}</h4>
            <div className="scroll">
              <table>
                <thead><tr><th>{m.slice_column}</th><th>Rows</th><th>Precision</th><th>Recall</th><th>F1</th></tr></thead>
                <tbody>
                  {m.slices.map((s) => (
                    <tr key={s.value}><td>{s.value}</td><td>{num(s.rows)}</td><td>{pct(s.precision)}</td><td>{pct(s.recall)}</td><td>{fixed(s.f1)}</td></tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
