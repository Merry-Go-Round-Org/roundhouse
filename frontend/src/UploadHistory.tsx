import { UploadProblems } from "./UploadProblems";
import { useEffect, useRef, useState } from "react";
import { createModelClient } from "./api";
import type { UploadState } from "./uploads";

const api = createModelClient();
const pageSize = 20;

/** Recovery is GET-only. Backend scopes the list to the authenticated actor. */
export function UploadHistory({ openModel }: { openModel: (id: string) => void }) {
  const [offset, setOffset] = useState(0);
  const [revision, setRevision] = useState(0);
  const [rows, setRows] = useState<UploadState[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [selected, setSelected] = useState<UploadState | null>(null);
  const [detailError, setDetailError] = useState("");
  const [reading, setReading] = useState(false);
  const detailRequest = useRef<AbortController | null>(null);

  useEffect(() => {
    const c = new AbortController();
    detailRequest.current?.abort();
    setRows([]); setSelected(null); setSelectedId(null); setDetailError("");
    setReading(false); setLoading(true); setError("");
    api.listUploads(offset, pageSize, c.signal).then(value => {
      if (!c.signal.aborted) setRows(value);
    }).catch((e: Error) => {
      if (!c.signal.aborted) setError(e.message);
    }).finally(() => {
      if (!c.signal.aborted) setLoading(false);
    });
    return () => { c.abort(); detailRequest.current?.abort(); };
  }, [offset, revision]);

  async function inspect(id: string) {
    detailRequest.current?.abort();
    const c = new AbortController();
    detailRequest.current = c;
    setSelectedId(id); setSelected(null); setDetailError(""); setReading(true);
    try {
      const value = await api.getUpload(id, c.signal);
      if (!c.signal.aborted) setSelected(value);
    } catch (e) {
      if (!c.signal.aborted) setDetailError((e as Error).message);
    } finally {
      if (!c.signal.aborted) setReading(false);
    }
  }

  return <section className="lifecycle-card" aria-label="Saved model uploads">
    <h2>Saved model uploads</h2>
    <p>Uploads submitted by your signed-in account, newest first. Recover status after
      a reload or uncertain transfer without sending the ZIP again. Dataset uploads
      are checked in Datasets. No file bytes or credentials are stored here.</p>
    <button type="button" className="button secondary" disabled={loading}
      onClick={() => {setLoading(true);setRevision(v => v + 1);}}>Refresh saved uploads</button>
    {loading && <p role="status">Loading saved uploads…</p>}
    {error && <p role="alert">{error}</p>}
    {!loading && !error && <>
      {rows.length === 0 ? <p>No uploads returned on this page. This does not prove an uncertain transfer was rejected. Check other pages or ask the backend operator before retrying.</p> :
        <ul>{rows.map(row => <li key={row.id}>
          <p>Upload {row.id} · {row.status}</p>
          <button type="button" className="button secondary" onClick={() => void inspect(row.id)}>
            Inspect upload {row.id}
          </button>
        </li>)}</ul>}
      <p>Page {offset / pageSize + 1} · up to 20 records; no total count is supplied.</p>
    </>}
    <div className="form-actions">
      <button type="button" disabled={loading || offset === 0}
        onClick={() => {setLoading(true);setOffset(v => Math.max(0, v - pageSize));}}>Previous uploads</button>
      <button type="button" disabled={loading || !!error || rows.length < pageSize}
        onClick={() => {setLoading(true);setOffset(v => v + pageSize);}}>Next uploads</button>
    </div>
    {!loading && selectedId && <section aria-label="Selected upload">
      <h3>Upload {selectedId}</h3>
      <button type="button" className="button secondary" disabled={reading}
        onClick={() => void inspect(selectedId)}>Refresh upload status</button>
      {reading && <p role="status">Reading persisted upload…</p>}
      {detailError && <p role="alert">{detailError}</p>}
      {selected && <>
        <p>Status: {selected.status}</p>
        <p>Refresh explicitly for queued/running work. Closing this view does not cancel processing.</p>
        <UploadProblems upload={selected} />
        {selected.error && <p role="alert">{selected.error}</p>}
        {selected.checks.length ? <ul>{selected.checks.map((check, i) => <li key={i}>
          {check.name}: {check.status} · {check.blocking ? "Blocking" : "Advisory"} · {check.reason}
        </li>)}</ul> : <p>No checks reported yet.</p>}
        <details><summary>Bundle files reported by backend</summary>
          {selected.files.length ? <ul>{selected.files.map((file, i) => <li key={i}>{file}</li>)}</ul> : <p>No files reported.</p>}
        </details>
        {selected.status === "completed" && selected.model_id && <>
          <p>Upload checks completed. This is not documentation completion, validation or human approval.</p>
          <button type="button" className="button primary" onClick={() => openModel(selected.model_id!)}>Open recovered model</button>
        </>}
      </>}
    </section>}
  </section>;
}
