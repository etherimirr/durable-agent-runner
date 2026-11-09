import { useEffect, useState } from "react";
import type { Config } from "../types";

type Props = {
  config: Config;
  columns: string[];
  submitLabel: string;
  disabled: boolean;
  onSubmit: (config: Config) => void;
  onCancel?: () => void;
};

export function SettingsForm({ config, columns, submitLabel, disabled, onSubmit, onCancel }: Props) {
  const [draft, setDraft] = useState(config);
  const [threshold, setThreshold] = useState(String(config.threshold));
  useEffect(() => {
    setDraft(config);
    setThreshold(String(config.threshold));
  }, [config]);

  const value = Number(threshold);
  const valid = threshold.trim() !== "" && Number.isFinite(value) && draft.score_column !== draft.label_column
    && draft.positive_label.trim() !== "";
  const pick = (field: "score_column" | "label_column", label: string) => (
    <label>
      <span>{label}</span>
      <select value={draft[field]} onChange={(e) => setDraft({ ...draft, [field]: e.target.value })}>
        {columns.map((c) => <option key={c} value={c}>{c}</option>)}
      </select>
    </label>
  );

  return (
    <form
      className="settings"
      onSubmit={(e) => {
        e.preventDefault();
        if (valid) onSubmit({ ...draft, threshold: value });
      }}
    >
      {pick("score_column", "Score column")}
      {pick("label_column", "Label column")}
      <label>
        <span>Positive label</span>
        <input value={draft.positive_label} onChange={(e) => setDraft({ ...draft, positive_label: e.target.value })} />
      </label>
      <label>
        <span>Threshold</span>
        <input inputMode="decimal" value={threshold} onChange={(e) => setThreshold(e.target.value)} />
      </label>
      <label>
        <span>Slice by</span>
        <select value={draft.slice_column ?? ""} onChange={(e) => setDraft({ ...draft, slice_column: e.target.value || null })}>
          <option value="">None</option>
          {columns.filter((c) => c !== draft.score_column).map((c) => <option key={c} value={c}>{c}</option>)}
        </select>
      </label>
      <div className="settings-actions">
        <button className="primary" type="submit" disabled={disabled || !valid}>{submitLabel}</button>
        {onCancel && <button type="button" onClick={onCancel}>Back</button>}
        {draft.score_column === draft.label_column && <span className="hint">Score and label must be different columns.</span>}
      </div>
    </form>
  );
}

export function ConfigChips({ config }: { config: Config }) {
  return (
    <div className="chips">
      <span>score <b>{config.score_column}</b></span>
      <span>label <b>{config.label_column}</b> = <b>{config.positive_label}</b></span>
      <span>threshold <b>{config.threshold}</b></span>
      {config.slice_column && <span>by <b>{config.slice_column}</b></span>}
    </div>
  );
}
