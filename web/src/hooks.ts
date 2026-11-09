import { useCallback, useEffect, useRef, useState } from "react";
import { getThread, resumeThread, startThread } from "./api";
import type { Answer, View } from "./types";

export type Link = "live" | "polling" | "connecting";

/**
 * The thread's state, kept current by a snapshot stream. If the stream drops,
 * the browser reconnects on its own; while it is down we poll every 5 seconds
 * so a reloaded or flaky page still catches up.
 */
export function useThread(threadId: string) {
  const [view, setView] = useState<View | null>(null);
  const [link, setLink] = useState<Link>("connecting");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setView(null);
    setLink("connecting");
    let alive = true;
    const source = new EventSource(`/api/threads/${threadId}/events`);
    source.addEventListener("view", (e) => {
      if (!alive) return;
      setLink("live");
      setView(JSON.parse((e as MessageEvent<string>).data) as View | null);
    });
    source.onerror = () => alive && setLink("polling");
    const poll = window.setInterval(() => {
      if (source.readyState !== EventSource.OPEN) {
        getThread(threadId).then((v) => alive && setView(v), () => undefined);
      }
    }, 5000);
    return () => {
      alive = false;
      source.close();
      window.clearInterval(poll);
    };
  }, [threadId]);

  const act = useCallback(async (fn: () => Promise<View>) => {
    setBusy(true);
    setError(null);
    try {
      setView(await fn());
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }, []);

  const start = useCallback((datasetId: string) => act(() => startThread(threadId, datasetId)), [act, threadId]);
  const resume = useCallback((answer: Answer) => act(() => resumeThread(threadId, answer)), [act, threadId]);
  return { view, link, busy, error, start, resume };
}

export type LogLine = { seq: string; type: string; data: string };

/** The run's event log. The browser resends Last-Event-ID when it reconnects, so lines are never repeated or lost. */
export function useRunLog(runId: string | null) {
  const [lines, setLines] = useState<LogLine[]>([]);
  const seen = useRef(new Set<string>());

  useEffect(() => {
    setLines([]);
    seen.current = new Set();
    if (!runId) return;
    const source = new EventSource(`/runs/${runId}/stream`);
    const types = ["run.queued", "run.claimed", "run.recovered", "step.started", "step.finished",
                   "run.cancel_requested", "run.succeeded", "run.failed", "run.cancelled"];
    for (const type of types) {
      source.addEventListener(type, (e) => {
        const ev = e as MessageEvent<string>;
        if (seen.current.has(ev.lastEventId)) return;
        seen.current.add(ev.lastEventId);
        setLines((prev) => [...prev, { seq: ev.lastEventId, type, data: ev.data }]);
        if (type === "run.succeeded" || type === "run.failed" || type === "run.cancelled") source.close();
      });
    }
    return () => source.close();
  }, [runId]);

  return lines;
}
