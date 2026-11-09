"""Step 2. Copy the file with two added columns: the prediction at the
threshold and which confusion cell the row falls in."""
import csv
import sys

sys.path.insert(0, __file__.rsplit("/", 1)[0])
from _common import is_positive, load_input, parse_score

path, cfg = load_input()
threshold = float(cfg["threshold"])
n = 0
with open(path, newline="", encoding="utf-8-sig") as src, open("enhanced.csv", "w", newline="", encoding="utf-8") as dst:
    reader = csv.DictReader(src)
    writer = csv.DictWriter(dst, fieldnames=list(reader.fieldnames) + ["_predicted", "_outcome"])
    writer.writeheader()
    for row in reader:
        score = parse_score(row[cfg["score_column"]])
        if score is None:
            row["_predicted"], row["_outcome"] = "", "no_score"
        else:
            predicted = score >= threshold
            actual = is_positive(row[cfg["label_column"]], cfg["positive_label"])
            row["_predicted"] = int(predicted)
            row["_outcome"] = ("TP" if actual else "FP") if predicted else ("FN" if actual else "TN")
        writer.writerow(row)
        n += 1
print(f"{n} rows written")
