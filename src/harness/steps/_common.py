"""Shared helpers for analysis steps. Standard library only: these files run
inside the step sandbox with `python -I`."""
import csv
import json
import os

csv.field_size_limit(1 << 24)


def load_input():
    with open(os.environ["DAR_INPUT"]) as f:
        spec = json.load(f)
    return spec["dataset_path"], spec["config"]


def prev(step_idx, name):
    return os.path.join(os.environ["DAR_STEPS_DIR"], str(step_idx), name)


def rows(path):
    """Yield (row_dict) for every data row, streaming."""
    with open(path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            yield row


def parse_score(value):
    try:
        x = float(value)
    except (TypeError, ValueError):
        return None
    return x if x == x and x not in (float("inf"), float("-inf")) else None


def is_positive(value, positive_label):
    return (value or "").strip() == positive_label


def ratio(a, b):
    return a / b if b else None


def confusion_metrics(tp, fp, fn, tn):
    p, r = ratio(tp, tp + fp), ratio(tp, tp + fn)
    return {
        "tp": tp, "fp": fp, "fn": fn, "tn": tn,
        "precision": p,
        "recall": r,
        "f1": (2 * p * r / (p + r)) if p and r else (0.0 if p is not None and r is not None else None),
        "fpr": ratio(fp, fp + tn),
        "accuracy": ratio(tp + tn, tp + fp + fn + tn),
    }


def write_json(name, obj):
    with open(name, "w") as f:
        json.dump(obj, f, indent=1)
