"""A synthetic evaluation export, so the demo works without anyone's data."""
from __future__ import annotations

import csv
import io
import random


def sample_export(rows: int = 200_000, seed: int = 7) -> bytes:
    rng = random.Random(seed)
    regions = [("us", 0.00), ("eu", -0.03), ("apac", -0.10), ("latam", -0.06)]
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["item_id", "region", "surface", "model_score", "gt_label"])
    for i in range(rows):
        region, shift = rng.choice(regions)
        positive = rng.random() < 0.22
        mean = (0.70 + shift) if positive else 0.30
        score = min(1.0, max(0.0, rng.gauss(mean, 0.17)))
        w.writerow([f"it_{i:07d}", region, rng.choice(["feed", "search", "live"]),
                    "" if rng.random() < 0.001 else f"{score:.4f}", "violation" if positive else "ok"])
    return out.getvalue().encode()
