"""Step 1. One pass: exact confusion matrix at the chosen threshold, a
threshold sweep from a fixed-size histogram, and a breakdown by slice."""
import json
import sys
from collections import defaultdict

sys.path.insert(0, __file__.rsplit("/", 1)[0])
from _common import confusion_metrics, is_positive, load_input, parse_score, prev, rows, write_json

BINS = 200
path, cfg = load_input()
profile = json.load(open(prev(0, "profile.json")))
threshold = float(cfg["threshold"])
lo = min(0.0, profile["score_min"])
hi = max(1.0, profile["score_max"])
width = (hi - lo) / BINS

pos_hist, neg_hist = [0] * BINS, [0] * BINS
tp = fp = fn = tn = 0
slices = defaultdict(lambda: [0, 0, 0, 0])
slice_col = cfg.get("slice_column")
for row in rows(path):
    score = parse_score(row[cfg["score_column"]])
    if score is None:
        continue
    actual = is_positive(row[cfg["label_column"]], cfg["positive_label"])
    predicted = score >= threshold
    b = min(BINS - 1, int((score - lo) / width))
    (pos_hist if actual else neg_hist)[b] += 1
    cell = 0 if (predicted and actual) else 1 if predicted else 2 if actual else 3
    tp += cell == 0; fp += cell == 1; fn += cell == 2; tn += cell == 3
    if slice_col:
        slices[row[slice_col].strip() or "(empty)"][cell] += 1

total_pos, total_neg = sum(pos_hist), sum(neg_hist)
curve = []
above_pos = above_neg = 0
for b in range(BINS - 1, -1, -1):          # threshold = lower edge of bin b
    above_pos += pos_hist[b]
    above_neg += neg_hist[b]
    m = confusion_metrics(above_pos, above_neg, total_pos - above_pos, total_neg - above_neg)
    curve.append({"threshold": round(lo + b * width, 6), **{k: m[k] for k in ("precision", "recall", "f1", "fpr")}})
curve.reverse()
best = max(curve, key=lambda c: (c["f1"] or 0))
# ROC area by trapezoid over the sweep, from (0,0) to (1,1).
pts = sorted({(c["fpr"] or 0.0, c["recall"] or 0.0) for c in curve} | {(0.0, 0.0), (1.0, 1.0)})
auc = sum((x2 - x1) * (y1 + y2) / 2 for (x1, y1), (x2, y2) in zip(pts, pts[1:])) if total_pos and total_neg else None

top = sorted(slices.items(), key=lambda kv: -sum(kv[1]))[:20]
write_json("metrics.json", {
    "threshold": threshold,
    "at_threshold": confusion_metrics(tp, fp, fn, tn),
    "positives": total_pos,
    "negatives": total_neg,
    "roc_auc_approx": auc,
    "best_f1": {"threshold": best["threshold"], "f1": best["f1"]},
    "curve": curve[:: max(1, BINS // 50)],
    "slice_column": slice_col,
    "slices": [{"value": k, "rows": sum(v), **confusion_metrics(*v)} for k, v in top],
    "slice_values_total": len(slices),
})
m = confusion_metrics(tp, fp, fn, tn)
print(f"tp {tp} fp {fp} fn {fn} tn {tn}")
