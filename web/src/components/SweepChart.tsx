import { useMemo, useRef, useState } from "react";
import { fixed, pct } from "../format";
import type { CurvePoint } from "../types";

const W = 620;
const H = 250;
const M = { left: 44, right: 78, top: 22, bottom: 34 };
const SERIES = [
  { key: "precision", name: "Precision", color: "var(--series-1)" },
  { key: "recall", name: "Recall", color: "var(--series-2)" },
] as const;

/** Precision and recall against the decision threshold, on one shared 0-100% axis. */
export function SweepChart({ curve, threshold }: { curve: CurvePoint[]; threshold: number }) {
  const [hover, setHover] = useState<number | null>(null);
  const svg = useRef<SVGSVGElement>(null);
  const x0 = curve[0].threshold;
  const x1 = curve[curve.length - 1].threshold;
  const sx = (t: number) => M.left + ((t - x0) / (x1 - x0 || 1)) * (W - M.left - M.right);
  const sy = (v: number) => M.top + (1 - v) * (H - M.top - M.bottom);

  const paths = useMemo(
    () =>
      SERIES.map((s) => {
        const pts = curve.filter((p) => p[s.key] != null);
        return {
          ...s,
          d: pts.map((p, i) => `${i ? "L" : "M"}${sx(p.threshold).toFixed(1)},${sy(p[s.key] as number).toFixed(1)}`).join(""),
          end: pts[pts.length - 1],
        };
      }),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [curve],
  );

  // Keep the two end labels from sitting on top of each other.
  const ends = paths.map((p) => (p.end ? sy(p.end[p.key] as number) : 0));
  if (Math.abs(ends[0] - ends[1]) < 14) {
    const mid = (ends[0] + ends[1]) / 2;
    const first = ends[0] <= ends[1] ? 0 : 1;
    ends[first] = mid - 7;
    ends[1 - first] = mid + 7;
  }

  const onMove = (e: React.PointerEvent) => {
    const box = svg.current!.getBoundingClientRect();
    const t = x0 + (((e.clientX - box.left) / box.width) * W - M.left) / (W - M.left - M.right) * (x1 - x0);
    let best = 0;
    curve.forEach((p, i) => {
      if (Math.abs(p.threshold - t) < Math.abs(curve[best].threshold - t)) best = i;
    });
    setHover(best);
  };
  const h = hover == null ? null : curve[hover];
  const inRange = threshold >= x0 && threshold <= x1;
  const xticks = [0, 0.2, 0.4, 0.6, 0.8, 1].filter((t) => t >= x0 - 1e-9 && t <= x1 + 1e-9);
  const tipLeft = h ? (sx(h.threshold) / W) * 100 : 0;

  return (
    <figure className="chart">
      <figcaption>
        <span>Precision and recall by threshold</span>
        <span className="legend">
          {SERIES.map((s) => (
            <span key={s.key}><i style={{ background: s.color }} />{s.name}</span>
          ))}
        </span>
      </figcaption>
      <div className="chart-box">
        <svg ref={svg} viewBox={`0 0 ${W} ${H}`} role="img"
          aria-label="Line chart of precision and recall as the decision threshold changes"
          onPointerMove={onMove} onPointerLeave={() => setHover(null)}>
          {[0, 0.25, 0.5, 0.75, 1].map((v) => (
            <g key={v}>
              <line x1={M.left} x2={W - M.right} y1={sy(v)} y2={sy(v)} className={v === 0 ? "axis" : "grid"} />
              <text x={M.left - 8} y={sy(v)} className="tick" textAnchor="end" dominantBaseline="middle">{100 * v}%</text>
            </g>
          ))}
          {xticks.map((t) => (
            <text key={t} x={sx(t)} y={H - M.bottom + 16} className="tick" textAnchor="middle">{t.toFixed(1)}</text>
          ))}
          <text x={(M.left + W - M.right) / 2} y={H - 4} className="tick" textAnchor="middle">threshold</text>
          {inRange && (
            <g>
              <line x1={sx(threshold)} x2={sx(threshold)} y1={M.top - 4} y2={H - M.bottom} className="marker" />
              <text x={sx(threshold)} y={M.top - 9} className="marker-label" textAnchor="middle">chosen {threshold}</text>
            </g>
          )}
          {paths.map((p, i) => (
            <g key={p.key}>
              <path d={p.d} fill="none" stroke={p.color} strokeWidth={2} strokeLinejoin="round" strokeLinecap="round" />
              {p.end && <text x={W - M.right + 8} y={ends[i]} className="end-label" dominantBaseline="middle">{p.name}</text>}
            </g>
          ))}
          {h && (
            <g pointerEvents="none">
              <line x1={sx(h.threshold)} x2={sx(h.threshold)} y1={M.top} y2={H - M.bottom} className="crosshair" />
              {SERIES.map((s) => h[s.key] != null && (
                <circle key={s.key} cx={sx(h.threshold)} cy={sy(h[s.key] as number)} r={4.5} fill={s.color}
                  stroke="var(--surface)" strokeWidth={2} />
              ))}
            </g>
          )}
        </svg>
        {h && (
          <div className="tip" style={{ left: `${Math.min(Math.max(tipLeft, 14), 78)}%` }}>
            <b>threshold {h.threshold.toFixed(2)}</b>
            {SERIES.map((s) => (
              <span key={s.key}><i style={{ background: s.color }} />{s.name} {pct(h[s.key])}</span>
            ))}
            <span>F1 {fixed(h.f1)}</span>
          </div>
        )}
      </div>
      <details>
        <summary>Show as a table</summary>
        <div className="scroll">
          <table>
            <thead><tr><th>Threshold</th><th>Precision</th><th>Recall</th><th>F1</th></tr></thead>
            <tbody>
              {curve.filter((_, i) => i % 5 === 0).map((p) => (
                <tr key={p.threshold}><td>{p.threshold.toFixed(2)}</td><td>{pct(p.precision)}</td><td>{pct(p.recall)}</td><td>{fixed(p.f1)}</td></tr>
              ))}
            </tbody>
          </table>
        </div>
      </details>
    </figure>
  );
}
