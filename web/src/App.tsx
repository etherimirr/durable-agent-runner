import { useEffect, useRef, useState } from "react";
import { getInfo, listDatasets, makeSample, uploadDataset } from "./api";
import { ReportCard } from "./components/ReportCard";
import { RunCard } from "./components/RunCard";
import { ConfigChips, SettingsForm } from "./components/SettingsForm";
import { bytes } from "./format";
import { useRunLog, useThread } from "./hooks";
import type { Answer, Config, Dataset, Entry, Info, View } from "./types";

/** Render `backticked` words as code. */
function Rich({ text }: { text?: string }) {
  if (!text) return null;
  return <p>{text.split(/(`[^`]+`)/).map((part, i) => (part.startsWith("`") ? <code key={i}>{part.slice(1, -1)}</code> : part))}</p>;
}

function newThreadId() {
  return "t-" + Math.random().toString(36).slice(2, 10);
}

function initialThread() {
  const fromHash = window.location.hash.slice(1);
  if (/^[A-Za-z0-9_-]{1,64}$/.test(fromHash)) return fromHash;
  let saved: string | null = null;
  try {
    saved = window.localStorage.getItem("dar_thread");
  } catch {
    /* storage may be unavailable */
  }
  return saved ?? newThreadId();
}

export function App() {
  const [threadId, setThreadId] = useState(initialThread);
  const { view, link, busy, error, start, resume } = useThread(threadId);
  const [info, setInfo] = useState<Info | null>(null);
  const [logOpen, setLogOpen] = useState(false);
  const bottom = useRef<HTMLDivElement>(null);

  useEffect(() => {
    window.history.replaceState(null, "", `#${threadId}`);
    try {
      window.localStorage.setItem("dar_thread", threadId);
    } catch {
      /* fine without it */
    }
  }, [threadId]);
  useEffect(() => {
    getInfo().then(setInfo, () => undefined);
    const onHash = () => {
      const id = window.location.hash.slice(1);
      if (/^[A-Za-z0-9_-]{1,64}$/.test(id)) setThreadId(id);
    };
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  const count = view?.transcript.length ?? 0;
  const pendingKind = view?.pending?.kind;
  useEffect(() => {
    bottom.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [count, pendingKind]);

  const idle = !view || (view.pending === null && (view.phase === "submitted" || view.phase === "failed"));
  return (
    <div className="app">
      <header>
        <div>
          <h1>Evaluation analysis agent</h1>
          <p>Upload an export of model scores and labels. The agent reads the header, agrees the settings with you, runs the full analysis in the background, and submits only what you approve.</p>
        </div>
        <div className="meta">
          <span className={`link ${link}`}>{link === "live" ? "live" : link === "polling" ? "reconnecting, polling every 5 s" : "connecting"}</span>
          {info && <span>judgment: {info.advisor === "claude" ? info.model : "built-in rules"}</span>}
          {info && <span>tools over {info.tools === "mcp" ? "MCP" : "direct calls"}</span>}
          <span>thread {threadId}</span>
          <button onClick={() => setThreadId(newThreadId())}>New thread</button>
        </div>
      </header>

      <main>
        <section className="conversation">
          {view?.transcript.map((e, i) => (
            <EntryView key={e.id} entry={e} view={view} last={i === view.transcript.length - 1} busy={busy} resume={resume} />
          ))}
          {idle && <DatasetPicker busy={busy} started={!!view} onStart={start} />}
          {error && <p className="error" role="alert">{error}</p>}
          <div ref={bottom} />
        </section>

        <aside>
          <button className="log-toggle" onClick={() => setLogOpen(!logOpen)} aria-expanded={logOpen}>
            {logOpen ? "Hide" : "Show"} run event log
          </button>
          {logOpen && <RunLog runId={view?.run?.run_id ?? null} />}
        </aside>
      </main>

      {view?.pending && <Composer pending={view.pending.kind} busy={busy} resume={resume} />}
    </div>
  );
}

type EntryProps = { entry: Entry; view: View; last: boolean; busy: boolean; resume: (a: Answer) => void };

function EntryView({ entry: e, view, last, busy, resume }: EntryProps) {
  const [editing, setEditing] = useState(false);
  const pending = view.pending;
  const body = (() => {
    switch (e.kind) {
      case "start":
        return <p>Analyze <b>{e.dataset_id}</b></p>;
      case "text":
        return <Rich text={e.text} />;
      case "preview":
        return (
          <>
            <p className="muted small">
              Read {bytes(e.bytes_read ?? 0)} of {bytes(e.bytes ?? 0)} to look at the header and the first rows. Nothing else has been read yet.
            </p>
            <div className="scroll">
              <table>
                <thead><tr>{e.columns?.map((c) => <th key={c}>{c}</th>)}</tr></thead>
                <tbody>{e.rows?.map((r, i) => <tr key={i}>{r.map((c, j) => <td key={j}>{c}</td>)}</tr>)}</tbody>
              </table>
            </div>
          </>
        );
      case "proposal":
        return (
          <>
            <Rich text={e.text} />
            {last && pending?.kind === "confirm_config" ? (
              <SettingsForm config={pending.config} columns={pending.columns} submitLabel="Confirm and run" disabled={busy}
                onSubmit={(config) => resume({ action: "confirm", config })} />
            ) : (
              e.config && <ConfigChips config={e.config} />
            )}
          </>
        );
      case "confirmed":
      case "redo":
        return <>{e.kind === "redo" ? <p>Run it again with:</p> : <p>Confirmed:</p>}{e.config && <ConfigChips config={e.config} />}</>;
      case "run":
        return view.run && view.run.run_id === e.run_id ? (
          <RunCard run={view.run} busy={busy} onCancel={pending?.kind === "await_run" ? () => resume({ action: "cancel" }) : undefined} />
        ) : (
          <p className="muted">Earlier run {e.run_id?.slice(0, 8)}</p>
        );
      case "cancel_requested":
        return <p>Cancel the run.</p>;
      case "run_ended":
        return <Rich text={e.text} />;
      case "report": {
        const reviewing = last && pending?.kind === "review_report";
        return (
          <>
            <Rich text={e.text} />
            {e.report && <ReportCard report={e.report} />}
            {reviewing && !editing && (
              <div className="actions">
                <button className="primary" disabled={busy} onClick={() => resume({ action: "submit" })}>Approve and submit</button>
                <button disabled={busy} onClick={() => setEditing(true)}>Change settings and run again</button>
              </div>
            )}
            {reviewing && editing && pending.kind === "review_report" && (
              <SettingsForm config={pending.config} columns={pending.columns} submitLabel="Run again" disabled={busy}
                onCancel={() => setEditing(false)}
                onSubmit={(config: Config) => { setEditing(false); resume({ action: "redo", config }); }} />
            )}
          </>
        );
      }
      case "approved":
        return <p>Approved. Submit it.</p>;
      case "submitted":
        return (
          <>
            <p>Submitted. These files are in the outbox:</p>
            <ul className="files">{e.files?.map((f) => <li key={f}>{f}</li>)}</ul>
            <p className="muted small">{e.path}</p>
          </>
        );
      default:
        return <p>{e.text ?? e.kind}</p>;
    }
  })();
  // The settings form of a failed or cancelled run lives on the last entry, whatever its kind.
  const reopen = last && pending?.kind === "confirm_config" && e.kind !== "proposal";
  return (
    <div className={`entry ${e.role}`}>
      {body}
      {e.source && <span className="source">{e.source === "claude" ? "written by the model" : "written by rules"}</span>}
      {reopen && pending.kind === "confirm_config" && (
        <SettingsForm config={pending.config} columns={pending.columns} submitLabel="Confirm and run" disabled={busy}
          onSubmit={(config) => resume({ action: "confirm", config })} />
      )}
    </div>
  );
}

function DatasetPicker({ busy, started, onStart }: { busy: boolean; started: boolean; onStart: (id: string) => void }) {
  const [datasets, setDatasets] = useState<Dataset[] | null>(null);
  const [working, setWorking] = useState<string | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const refresh = () => listDatasets().then(setDatasets, (e: Error) => setProblem(e.message));
  useEffect(() => {
    refresh();
  }, []);
  const run = async (label: string, fn: () => Promise<Dataset>) => {
    setWorking(label);
    setProblem(null);
    try {
      await fn();
      await refresh();
    } catch (e) {
      setProblem(e instanceof Error ? e.message : String(e));
    } finally {
      setWorking(null);
    }
  };
  return (
    <div className="picker">
      <h2>{started ? "Analyze another file" : "Choose a file to analyze"}</h2>
      {datasets && datasets.length === 0 && <p className="muted">No files yet. Upload a CSV, or generate a sample to try the flow.</p>}
      <ul>
        {datasets?.map((d) => (
          <li key={d.dataset_id}>
            <span>{d.dataset_id}<small>{bytes(d.bytes)}</small></span>
            <button className="primary" disabled={busy} onClick={() => onStart(d.dataset_id)}>Analyze</button>
          </li>
        ))}
      </ul>
      <div className="actions">
        <label className="button">
          {working === "upload" ? "Uploading" : "Upload CSV"}
          <input type="file" accept=".csv,text/csv" hidden disabled={!!working}
            onChange={(e) => {
              const file = e.target.files?.[0];
              e.target.value = "";
              if (file) void run("upload", () => uploadDataset(file));
            }} />
        </label>
        <button disabled={!!working} onClick={() => void run("sample", () => makeSample(200000))}>
          {working === "sample" ? "Generating" : "Generate a 200,000-row sample"}
        </button>
      </div>
      {problem && <p className="error" role="alert">{problem}</p>}
    </div>
  );
}

function Composer({ pending, busy, resume }: { pending: string; busy: boolean; resume: (a: Answer) => void }) {
  const [text, setText] = useState("");
  const hint =
    pending === "confirm_config" ? "Change a setting in words, for example: threshold 0.7, slice by region"
    : pending === "await_run" ? "The analysis is running. You can say cancel."
    : "Ask for another run, for example: try threshold 0.6, or say submit";
  return (
    <form className="composer" onSubmit={(e) => {
      e.preventDefault();
      const t = text.trim();
      if (!t) return;
      setText("");
      resume({ action: "message", text: t });
    }}>
      <input value={text} onChange={(e) => setText(e.target.value)} placeholder={hint} aria-label="Message to the agent" />
      <button className="primary" type="submit" disabled={busy || !text.trim()}>Send</button>
    </form>
  );
}

function RunLog({ runId }: { runId: string | null }) {
  const lines = useRunLog(runId);
  if (!runId) return <p className="muted small">No run in this thread yet.</p>;
  return (
    <ol className="log" aria-label="Run events">
      {lines.map((l) => (
        <li key={l.seq}><span>#{l.seq}</span> <b>{l.type}</b> {l.data === "{}" ? "" : l.data}</li>
      ))}
    </ol>
  );
}
