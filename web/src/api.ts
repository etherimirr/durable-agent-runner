import type { Answer, Dataset, Info, View } from "./types";

async function json<T>(res: Response): Promise<T> {
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
    } catch {
      /* keep the status text */
    }
    throw new Error(detail);
  }
  return res.json() as Promise<T>;
}

const post = (url: string, body?: unknown) =>
  fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: body ? JSON.stringify(body) : undefined });

export const getInfo = () => fetch("/api/info").then((r) => json<Info>(r));
export const listDatasets = () => fetch("/api/datasets").then((r) => json<{ datasets: Dataset[] }>(r)).then((d) => d.datasets);
export const makeSample = (rows: number) => post(`/api/datasets/sample?rows=${rows}`).then((r) => json<Dataset>(r));
export const getThread = (id: string) => fetch(`/api/threads/${id}`).then((r) => (r.status === 404 ? null : json<View>(r)));
export const startThread = (id: string, dataset_id: string) => post(`/api/threads/${id}/start`, { dataset_id }).then((r) => json<View>(r));
export const resumeThread = (id: string, answer: Answer) => post(`/api/threads/${id}/resume`, answer).then((r) => json<View>(r));

export function uploadDataset(file: File) {
  const form = new FormData();
  form.append("file", file);
  return fetch("/api/datasets", { method: "POST", body: form }).then((r) => json<Dataset>(r));
}
