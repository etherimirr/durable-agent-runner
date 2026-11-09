export type Config = {
  score_column: string;
  label_column: string;
  positive_label: string;
  threshold: number;
  slice_column: string | null;
};

export type Cells = {
  tp: number;
  fp: number;
  fn: number;
  tn: number;
  precision: number | null;
  recall: number | null;
  f1: number | null;
  fpr: number | null;
  accuracy: number | null;
};

export type CurvePoint = {
  threshold: number;
  precision: number | null;
  recall: number | null;
  f1: number | null;
  fpr: number | null;
};

export type Report = {
  config: Config;
  profile: {
    rows: number;
    scored_rows: number;
    rows_without_numeric_score: number;
    rows_without_label: number;
    score_min: number;
    score_max: number;
    distinct_labels: number;
    label_values: Record<string, number>;
  };
  metrics: {
    threshold: number;
    at_threshold: Cells;
    positives: number;
    negatives: number;
    roc_auc_approx: number | null;
    best_f1: { threshold: number; f1: number | null };
    curve: CurvePoint[];
    slice_column: string | null;
    slices: (Cells & { value: string; rows: number })[];
    slice_values_total: number;
  };
};

export type Entry = {
  id: string;
  role: "user" | "agent";
  kind: string;
  ts: number;
  text?: string;
  source?: string;
  config?: Config;
  dataset_id?: string;
  columns?: string[];
  rows?: string[][];
  bytes?: number;
  bytes_read?: number;
  run_id?: string;
  status?: string;
  report?: Report;
  path?: string;
  files?: string[];
};

export type StepView = { idx: number; name: string; status: string; attempts: number; output: string | null };

export type RunView = {
  run_id: string;
  status: "queued" | "running" | "succeeded" | "failed" | "cancelled";
  attempt: number;
  error: string | null;
  step_names: string[];
  steps: StepView[];
  config: Config | null;
};

export type Pending =
  | { kind: "confirm_config"; config: Config; columns: string[] }
  | { kind: "await_run"; run_id: string }
  | { kind: "review_report"; run_id: string; config: Config; columns: string[] };

export type View = {
  thread_id: string;
  phase: string | null;
  dataset_id: string | null;
  columns: string[];
  config: Config | null;
  pending: Pending | null;
  run: RunView | null;
  transcript: Entry[];
  advisor: string;
};

export type Dataset = { dataset_id: string; bytes: number };
export type Info = { advisor: string; tools: string; model: string | null };
export type Answer = { action: "confirm" | "redo" | "submit" | "cancel" | "message"; config?: Partial<Config>; text?: string };
