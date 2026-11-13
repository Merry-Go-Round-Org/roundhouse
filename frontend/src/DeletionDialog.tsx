import { useEffect, useRef, useState } from "react";
import { ApiError, createModelClient } from "./api";
import { readPendingDeletion, type DeletionPreview, type DeletionReceipt, type DeletionRequest, type DeletionSelection } from "./deletion";

const api = createModelClient();
export function DeletionDialog({ actorId, allowed, selection, close, refresh, completed }: {
  actorId: string; allowed: boolean; selection: DeletionSelection | null; close: () => void;
  refresh: () => void; completed: (request: DeletionRequest) => void;
}) {
  const storageKey = `pending-deletion:${actorId}`;
  const [pending, setPending] = useState<DeletionRequest | null>(() => {
    try { return readPendingDeletion(JSON.parse(sessionStorage.getItem(storageKey) ?? "null")); } catch { return null; }
  });
  const [preview, setPreview] = useState<DeletionPreview | null>(null);
  const [receipt, setReceipt] = useState<DeletionReceipt | null>(null);
  const [confirmed, setConfirmed] = useState(false), [busy, setBusy] = useState(false), [error, setError] = useState("");
  const [blockers, setBlockers] = useState<string[]>([]), [recovery, setRecovery] = useState(false);
  const [retryAllowed, setRetryAllowed] = useState(false);
  const dialog = useRef<HTMLDialogElement>(null), active = useRef<AbortController | null>(null);
  const open = !!selection || recovery;
  useEffect(() => () => active.current?.abort(), []);
  useEffect(() => {
    if (open) dialog.current?.showModal(); else dialog.current?.close();
  }, [open]);
  useEffect(() => {
    if (!selection || pending || !allowed) return;
    setReceipt(null); setPreview(null); setError(""); setBlockers([]); setConfirmed(false);
    // Preview is a read-only POST; deletion always requires a separate confirmation.
    const c = new AbortController(); active.current = c; setBusy(true);
    api.deletion.preview(selection.kind, selection.items.map(i => i.id), c.signal)
      .then(value => { if (!c.signal.aborted) setPreview(value); })
      .catch(e => { if (!c.signal.aborted) {
        setError(e.message);
        if (e instanceof ApiError && [404, 409].includes(e.status)) refresh();
      } })
      .finally(() => { if (!c.signal.aborted) { active.current = null; setBusy(false); } });
    return () => { c.abort(); active.current = null; };
  }, [selection]); // A pending request is reconciled instead of replaced by a new selection.

  function dismiss() {
    if (busy) return;
    close(); setRecovery(false); setConfirmed(false);
  }
  function saved(value: DeletionReceipt, request: DeletionRequest) {
    setReceipt(value); setPending(null); setPreview(null); setError(""); setRetryAllowed(false);
    try { sessionStorage.removeItem(storageKey); } catch { /* Receipt remains visible; recovery is idempotent. */ }
    completed(request);
  }
  async function submit(retry = false) {
    if (active.current || busy || !allowed || !confirmed) return;
    const request: DeletionRequest | null = retry ? (retryAllowed ? pending : null) :
      selection && preview?.canDelete && !pending ? { kind: selection.kind, ids: preview.ids, token: preview.token, requestId: crypto.randomUUID() } : null;
    if (!request) return;
    try { sessionStorage.setItem(storageKey, JSON.stringify(request)); }
    catch { setError("Cannot save recovery information in this browser. Deletion was not sent."); return; }
    setPending(request); setConfirmed(false); setError(""); setRetryAllowed(false); setBusy(true);
    const c = new AbortController(); active.current = c;
    try { const value = await api.deletion.remove(request, c.signal); if (!c.signal.aborted) saved(value, request); }
    catch (e) {
      if (c.signal.aborted) return;
      if (e instanceof ApiError && [403, 404, 409, 422].includes(e.status)) {
        sessionStorage.removeItem(storageKey); setPending(null); setPreview(null);
        setError(`${e.message} No deletion confirmed. Refresh the list and review a new selection.`);
        const detail = (e as ApiError & { deletionDetail?: { models?: { id: string; blockers: string[] }[]; datasets?: { version_id: string; blockers: Record<string, string[]> }[] } }).deletionDetail;
        const reasons: string[] = [];
        if (Array.isArray(detail?.models)) for (const row of detail.models) if (Array.isArray(row.blockers)) for (const reason of row.blockers) if (typeof reason === "string") reasons.push(`${row.id}: ${reason}`);
        if (Array.isArray(detail?.datasets)) for (const row of detail.datasets) if (row.blockers && typeof row.blockers === "object") for (const [key, refs] of Object.entries(row.blockers)) if (Array.isArray(refs)) reasons.push(`${row.version_id}: ${key} — ${refs.filter(r => typeof r === "string").join(", ")}`);
        setBlockers(reasons); refresh();
      } else setError("Deletion outcome is unknown. Check the saved receipt before retrying; do not start a new deletion for these records.");
    } finally { if (!c.signal.aborted) { active.current = null; setBusy(false); } }
  }
  async function checkReceipt() {
    if (!pending || active.current || busy || !allowed) return;
    const request = pending, c = new AbortController(); active.current = c;
    setBusy(true); setError(""); setRetryAllowed(false); setConfirmed(false);
    try { const value = await api.deletion.receipt(request, c.signal); if (!c.signal.aborted) saved(value, request); }
    catch (e) {
      if (c.signal.aborted) return;
      if (e instanceof ApiError && e.status === 404) {
        setError("No receipt is available yet. This does not prove the first request failed. Check again, or explicitly retry the identical request with its original request ID."); setRetryAllowed(true);
      } else setError(e instanceof Error ? e.message : "Cannot check the deletion receipt.");
    } finally { if (!c.signal.aborted) { active.current = null; setBusy(false); } }
  }
  return <>
    {pending && !open && <section className="lifecycle-card" role="status">
      <p>A {pending.kind} deletion needs reconciliation. Request: {pending.requestId}</p>
      <button onClick={() => { setRecovery(true); setReceipt(null); setError(""); }}>Recover deletion</button>
    </section>}
    <dialog ref={dialog} className="deletion-dialog" aria-labelledby="deletion-heading" onCancel={e => { e.preventDefault(); dismiss(); }}>
      <header className="form-actions"><h2 id="deletion-heading">{receipt ? "Deletion result" : "Review deletion"}</h2><button disabled={busy} onClick={dismiss}>Close</button></header>
      {busy && <p role="status">{pending ? "Checking deletion…" : "Checking selected records…"}</p>}
      {error && <p role="alert">{error}</p>}
      {blockers.length > 0 && <ul>{blockers.map((b, i) => <li key={i}>{b}</li>)}</ul>}
      {!allowed && <p role="alert">Deletion requires a developer or administrator account.</p>}
      {pending && !receipt && <section>
        <h3>Pending deletion ({pending.ids.length} {pending.kind})</h3>
        <p>Request ID: <code>{pending.requestId}</code></p>
        <ul>{pending.ids.map(id => <li key={id}><code>{id}</code></li>)}</ul>
        <button disabled={busy || !allowed} onClick={() => void checkReceipt()}>Check deletion receipt</button>
        {retryAllowed && <><label className="filter-checkbox"><input type="checkbox" checked={confirmed} onChange={e => setConfirmed(e.target.checked)} />I confirm retrying this same deletion request.</label>
          <button disabled={busy || !allowed || !confirmed} onClick={() => void submit(true)}>Retry same deletion request</button></>}
      </section>}
      {preview && !pending && !receipt && <>
        <p>{preview.effect}</p><p>All {preview.items.length} selected {selection?.kind} must be eligible. A blocked selection deletes nothing.</p>
        <ul>{preview.items.map(item => <li key={item.id} className="lifecycle-card">
          <strong>{item.name} · version {item.version}</strong><p>ID: <code>{item.id}</code></p>
          {Object.keys(item.counts).length > 0 && <details><summary>Retained records</summary><dl>{Object.entries(item.counts).map(([key, count]) => <div key={key}><dt>{key.replaceAll("_", " ")}</dt><dd>{count}</dd></div>)}</dl></details>}
          {item.blockers.length > 0 ? <><strong>Blocked</strong><ul>{item.blockers.map((b, i) => <li key={i}>{b}</li>)}</ul></> : <p>No blockers reported.</p>}
        </li>)}</ul>
        {!preview.canDelete && <p role="alert">Deletion blocked. Resolve the blockers listed above, then review again.</p>}
        <label className="filter-checkbox"><input type="checkbox" checked={confirmed} disabled={busy || !preview.canDelete || !allowed} onChange={e => setConfirmed(e.target.checked)} />I understand the listed records will be removed from the inventory. Evidence and audit history are kept.</label>
        <button disabled={busy || !allowed || !confirmed || !preview.canDelete} onClick={() => void submit()}>Delete {preview.items.length} {selection?.kind}</button>
      </>}
      {receipt && <section role="status"><h3>{receipt.deleted.length} records deleted</h3>
        <ul>{receipt.deleted.map(id => <li key={id}>{id}</li>)}</ul>
        <p>Retained: {receipt.retained.map(v => v.replaceAll("_", " ")).join(", ")}</p>
        {!!receipt.cleanup_pending?.length && <><p role="alert">Records were deleted, but managed file cleanup is pending. Ask the backend team to complete cleanup; do not repeat deletion.</p><ul>{receipt.cleanup_pending.map(p => <li key={p}>{p}</li>)}</ul></>}
        <p>The list is refreshing. If it cannot reload, use Refresh; do not repeat deletion.</p>
      </section>}
    </dialog>
  </>;
}
