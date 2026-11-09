"""Step 3. Merge the earlier outputs into one report file."""
import json
import sys

sys.path.insert(0, __file__.rsplit("/", 1)[0])
from _common import load_input, prev, write_json

_, cfg = load_input()
profile = json.load(open(prev(0, "profile.json")))
metrics = json.load(open(prev(1, "metrics.json")))
write_json("report.json", {"config": cfg, "profile": profile, "metrics": metrics})
at = metrics["at_threshold"]
fmt = lambda x: "n/a" if x is None else f"{x:.4f}"
print(f"precision {fmt(at['precision'])} recall {fmt(at['recall'])} f1 {fmt(at['f1'])}")
