"""Step 0. One pass over the whole file: size, bad rows, score range, label values."""
import sys
from collections import Counter

sys.path.insert(0, __file__.rsplit("/", 1)[0])
from _common import is_positive, load_input, parse_score, rows, write_json

path, cfg = load_input()
n = bad_score = missing_label = positives = 0
lo, hi = None, None
labels = Counter()
columns = None
for row in rows(path):
    if columns is None:
        columns = list(row.keys())
        for col in (cfg["score_column"], cfg["label_column"], cfg.get("slice_column")):
            if col and col not in row:
                sys.exit(f"column {col!r} is not in the file; columns are {columns}")
    n += 1
    score = parse_score(row[cfg["score_column"]])
    label = row[cfg["label_column"]]
    if label is None or label.strip() == "":
        missing_label += 1
    else:
        labels[label.strip()] += 1
    if score is None:
        bad_score += 1
        continue
    lo = score if lo is None else min(lo, score)
    hi = score if hi is None else max(hi, score)
    positives += is_positive(label, cfg["positive_label"])
if n == 0:
    sys.exit("the file has no data rows")
if bad_score == n:
    sys.exit(f"no row has a numeric value in {cfg['score_column']!r}")
write_json("profile.json", {
    "rows": n,
    "columns": columns,
    "rows_without_numeric_score": bad_score,
    "rows_without_label": missing_label,
    "scored_rows": n - bad_score,
    "score_min": lo,
    "score_max": hi,
    "label_values": dict(labels.most_common(50)),
    "distinct_labels": len(labels),
})
print(f"{n} rows, {n - bad_score} with a score, {len(labels)} label values")
