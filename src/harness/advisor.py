"""The parts of the agent that need judgment.

The graph decides what happens next. The advisor only fills in three things:
which columns are the score and the label, what a typed instruction means,
and how to word the summary of a finished report. Each has a rule-based
version that always works, and a Claude-backed version that is tried first
when credentials are present. Model output is validated before it is used,
and any failure falls back to the rules.
"""
from __future__ import annotations

import json
import os
import re
from typing import Any, Literal, Protocol

from pydantic import BaseModel

from .tools import validate_config

Action = Literal["update", "run", "submit", "cancel", "unclear"]


class Proposal(BaseModel):
    score_column: str
    label_column: str
    positive_label: str
    threshold: float
    slice_column: str | None
    rationale: str


class Instruction(BaseModel):
    action: Action
    score_column: str | None = None
    label_column: str | None = None
    positive_label: str | None = None
    threshold: float | None = None
    slice_column: str | None = None
    reply: str = ""


class Advisor(Protocol):
    name: str

    def propose(self, preview: dict[str, Any]) -> tuple[dict[str, Any], str, str]: ...
    def interpret(self, text: str, phase: str, config: dict[str, Any], columns: list[str]) -> tuple[Instruction, str]: ...
    def summarize(self, report: dict[str, Any]) -> tuple[str, str]: ...


# -- rules --------------------------------------------------------------------

_SCORE_NAME = re.compile(r"score|prob|pred|conf|logit|likelihood", re.I)
_LABEL_NAME = re.compile(r"label|truth|^gt$|_gt$|^gt_|target|actual|^y$|^class$|is_", re.I)
_POSITIVE = ("1", "true", "True", "TRUE", "positive", "pos", "yes", "Yes", "violation", "bad", "spam", "unsafe")


def _floats(values: list[str]) -> list[float] | None:
    out = []
    for v in values:
        try:
            out.append(float(v))
        except ValueError:
            return None
    return out or None


class RulesAdvisor:
    name = "rules"

    def propose(self, preview):
        cols = preview["columns"]
        by_col = {c: [r[i] for r in preview["rows"] if r[i] != ""] for i, c in enumerate(cols)}
        numeric = {c: _floats(v) for c, v in by_col.items()}

        def score_rank(c):
            xs = numeric[c]
            if not xs:
                return -1
            fractional = any(x != int(x) for x in xs)
            return 4 * bool(_SCORE_NAME.search(c)) + 2 * fractional + (min(xs) >= 0 and max(xs) <= 1)

        def label_rank(c, score):
            if c == score or not by_col[c]:
                return -1
            distinct = len(set(by_col[c]))
            return 4 * bool(_LABEL_NAME.search(c)) + 2 * (distinct <= 2) + (distinct <= 10)

        score = max(cols, key=score_rank)
        label = max(cols, key=lambda c: label_rank(c, score))
        values = list(dict.fromkeys(by_col.get(label, [])))
        positive = next((p for p in _POSITIVE if p in values), values[0] if values else "1")
        config = {"score_column": score, "label_column": label, "positive_label": positive,
                  "threshold": 0.5, "slice_column": None}
        why = (f"`{score}` looks like the model score and `{label}` looks like the ground truth, "
               f"with `{positive}` as the positive value. I picked these from the column names and the first "
               f"{len(preview['rows'])} rows only, so check them before the full run.")
        return config, why, self.name

    def interpret(self, text, phase, config, columns):
        t = text.strip()
        low = t.lower()
        ins = Instruction(action="unclear")
        m = re.search(r"(?:threshold|阈值|cut[- ]?off)\D{0,12}?(\d*\.\d+|\d+)", low) or re.fullmatch(r"\s*(\d*\.\d+|\d+)\s*", low)
        if m:
            ins.threshold = float(m.group(1))
        # "label column is note", "slice by region", "use note as the label column"
        for field, words in (("score_column", r"score|分数"), ("label_column", r"label|truth|标签"), ("slice_column", r"slice|切片|分组")):
            for c in sorted(columns, key=len, reverse=True):
                name = re.escape(c.lower())
                after = rf"(?:{words})(?:\s+column)?\s*(?:is|=|:|to|by|on|为|是|用|按)?\s*`?{name}`?(?![\w.-])"
                before = rf"(?<![\w.-])`?{name}`?\s+(?:as|for|is)\s+(?:the\s+)?(?:{words})"
                if re.search(after, low) or re.search(before, low):
                    setattr(ins, field, c)
                    break
        m = re.search(r"(?:positive|正例|正样本)(?:\s+(?:label|value|class))?\s*(?:is|=|:|to|为|是)\s*[`'\"]?([\w.-]+)", low)
        if m:
            ins.positive_label = t[m.start(1): m.end(1)]
        changed = any(getattr(ins, f) is not None for f in ("threshold", "score_column", "label_column", "positive_label", "slice_column"))
        if re.search(r"\b(cancel|stop|abort)\b|取消|停止", low):
            ins.action = "cancel"
        elif re.search(r"\b(submit|upload|send|publish)\b|提交|上传", low):
            ins.action = "submit"
        elif (re.search(r"\b(run|start|confirm|redo|rerun|again|go ahead)\b|开始|确认|重做|重跑|再跑", low)
              or re.fullmatch(r"(ok|okay|yes|y|sure|go)[.! ]*", low)):
            ins.action = "run"
        elif changed:
            ins.action = "update"
        ins.reply = {
            "update": "Updated. Confirm to run, or change something else.",
            "run": "Starting the full analysis.",
            "submit": "Submitting the report.",
            "cancel": "Cancelling.",
            "unclear": "I can change the score column, label column, positive label, threshold or slice column, "
                       "run the analysis, cancel it, or submit the report. Which one?",
        }[ins.action]
        return ins, self.name

    def summarize(self, report):
        m, p, cfg = report["metrics"], report["profile"], report["config"]
        at = m["at_threshold"]
        pct = lambda x: "n/a" if x is None else f"{100 * x:.1f}%"
        lines = [
            f"At threshold {cfg['threshold']:g} on `{cfg['score_column']}`, precision is {pct(at['precision'])} "
            f"and recall is {pct(at['recall'])} over {p['scored_rows']:,} scored rows "
            f"({m['positives']:,} positive, {m['negatives']:,} negative).",
        ]
        best = m["best_f1"]
        if best["f1"] is not None and at["f1"] is not None and best["f1"] - at["f1"] > 0.01:
            lines.append(f"F1 is {at['f1']:.3f} here. The sweep peaks at {best['f1']:.3f} near threshold "
                         f"{best['threshold']:g}, so the threshold is worth a second look.")
        if p["rows_without_numeric_score"]:
            lines.append(f"{p['rows_without_numeric_score']:,} rows had no numeric score and were left out.")
        if p["distinct_labels"] > 2:
            lines.append(f"The label column has {p['distinct_labels']} distinct values. Everything other than "
                         f"`{cfg['positive_label']}` was counted as negative.")
        scored = [s for s in m["slices"] if s["recall"] is not None and s["rows"] >= 50]
        if len(scored) >= 2:
            worst = min(scored, key=lambda s: s["recall"])
            if at["recall"] is not None and at["recall"] - worst["recall"] > 0.05:
                lines.append(f"Recall is lowest for `{m['slice_column']}` = `{worst['value']}` at "
                             f"{pct(worst['recall'])} ({worst['rows']:,} rows).")
        return " ".join(lines), self.name


# -- Claude -------------------------------------------------------------------

SYSTEM = """You help an analyst evaluate a binary classifier from an exported CSV of model scores and ground-truth labels.
Work only from the data in the message. Column names you return must be copied exactly from the list of columns.
A row is predicted positive when its score is at or above the threshold."""


class ClaudeAdvisor:
    """Uses the Anthropic SDK. Every method falls back to `fallback` on a
    refusal, an API error, or output that does not pass validation."""

    name = "claude"

    def __init__(self, client, model: str = "claude-opus-5-5", effort: str = "low", fallback: Advisor | None = None):
        self.client = client
        self.model = model
        self.effort = effort
        self.fallback = fallback or RulesAdvisor()
        self.disabled_reason: str | None = None

    def _parse(self, schema: type[BaseModel], prompt: str):
        import anthropic

        if self.disabled_reason:
            return None
        try:
            response = self.client.beta.messages.parse(
                model=self.model,
                max_tokens=16000,
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
                output_config={"effort": self.effort},
                system=SYSTEM,
                messages=[{"role": "user", "content": prompt}],
                output_format=schema,
            )
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as e:
            self.disabled_reason = f"credentials rejected ({e.status_code})"
            return None
        except (anthropic.RateLimitError, anthropic.APIStatusError, anthropic.APIConnectionError):
            return None
        except TypeError as e:
            # No credentials at all: the SDK cannot build auth headers.
            self.disabled_reason = str(e)
            return None
        if response.stop_reason == "refusal":
            return None
        return response.parsed_output

    def propose(self, preview):
        head = json.dumps({"columns": preview["columns"], "first_rows": preview["rows"]}, ensure_ascii=False)
        out = self._parse(Proposal, (
            "Here is the header and the first rows of the export. Pick the score column, the ground-truth label "
            "column, the label value that means positive, a starting threshold, and optionally one low-cardinality "
            "column worth slicing by. In `rationale`, say in two sentences what you chose and what the analyst "
            f"should double-check.\n\n{head}"))
        if out is not None:
            config = out.model_dump(exclude={"rationale"})
            if not validate_config(config, preview["columns"]):
                return config, out.rationale, self.name
        return self.fallback.propose(preview)

    def interpret(self, text, phase, config, columns):
        out = self._parse(Instruction, (
            f"The analyst is at the '{phase}' stage. Current settings: {json.dumps(config)}. "
            f"Columns in the file: {json.dumps(columns)}.\n"
            "Read their message and return what they want. Use action 'update' when they only change settings, "
            "'run' when they want the analysis started or repeated (include any settings they changed), 'submit' "
            "when they approve submitting the report, 'cancel' to stop a running analysis, and 'unclear' otherwise. "
            "Leave a setting null unless they asked to change it. `reply` is one short sentence back to them.\n\n"
            f"Message: {text}"))
        if out is not None:
            merged = {**config, **{k: v for k, v in out.model_dump(exclude={"action", "reply"}).items() if v is not None}}
            if not validate_config(merged, columns):
                return out, self.name
        return self.fallback.interpret(text, phase, config, columns)

    def summarize(self, report):
        class Summary(BaseModel):
            summary: str

        slim = {"config": report["config"], "profile": report["profile"],
                "metrics": {k: v for k, v in report["metrics"].items() if k != "curve"}}
        out = self._parse(Summary, (
            "Write a summary of this evaluation for the analyst in at most five plain sentences. Lead with precision "
            "and recall at the chosen threshold. Mention the best-F1 threshold only if it is clearly better, any rows "
            "that were excluded, and the weakest slice if one stands out. Use only numbers that appear below.\n\n"
            f"{json.dumps(slim)}"))
        if out is not None and out.summary.strip():
            return out.summary.strip(), self.name
        return self.fallback.summarize(report)


def make_advisor() -> Advisor:
    """Claude when credentials can be resolved, rules otherwise. `DAR_LLM=off` forces rules."""
    if os.environ.get("DAR_LLM", "auto") == "off":
        return RulesAdvisor()
    has_env = os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")
    has_profile = os.path.isdir(os.path.expanduser("~/.config/anthropic"))
    if not (has_env or has_profile):
        return RulesAdvisor()
    try:
        import anthropic
    except ImportError:
        return RulesAdvisor()
    return ClaudeAdvisor(
        anthropic.Anthropic(),
        model=os.environ.get("DAR_MODEL", "claude-opus-5-5"),
        effort=os.environ.get("DAR_EFFORT", "low"),
    )
