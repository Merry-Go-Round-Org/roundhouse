import { isRevisionRequested, revisionRequestedMessage } from "./runReviewStatus";
import { useEffect, useRef, useState } from "react";
import { createModelClient, type RunRead } from "./api";
import { useAuth } from "./AuthContext";
import { canStartOperation } from "./auth";
const api = createModelClient();

/** Observe the saved operation, independently of the chat turn that requested it. */
export function OperationStatusCard({ runId, modelId, onRequest }: {
  runId: string; modelId: string; onRequest?: (text: string) => void;
}) {
  const { user } = useAuth();
  const [run, setRun] = useState<RunRead | null>(null);
  const [error, setError] = useState("");
  const [revision, setRevision] = useState(0);
  const element = useRef<HTMLElement>(null);
  useEffect(() => {
    const c = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    setRun(null); setError("");
    async function read(initial = false) {
      if (!initial && (document.visibilityState !== "visible" || element.current?.closest("[hidden]"))) {
        timer = setTimeout(() => void read(), 3000); return;
      }
      try {
        const saved = await api.getRun(runId, c.signal);
        if (saved.id !== runId || saved.model_id !== modelId) throw new Error("Operation belongs to another model or run.");
        if (c.signal.aborted) return;
        setRun(saved);
        if (["queued", "running"].includes(saved.status)) timer = setTimeout(() => void read(), 3000);
      } catch (e) {
        if (!c.signal.aborted) { setRun(null); setError((e as Error).message); }
      }
    }
    void read(true);
    return () => { c.abort(); clearTimeout(timer); };
  }, [runId, modelId, revision]);
  const revisionRequested = isRevisionRequested(run);
  const tab = run?.operation === "validate" ? "Validation" : run?.operation === "document" ? "Documentation" : run?.operation === "rebuild" ? "Rebuild & parity" : "History";
  return <section className="operation-status-card" aria-label={`Operation status ${runId}`} ref={element}>
    <h4>Operation progress</h4>
    <button type="button" onClick={() => { setRun(null); setError(""); setRevision(v => v + 1); }}>Refresh operation status</button>
    {error ? <p role="alert">Could not load operation status: {error} No new operation was submitted.</p> : !run ? <p role="status">Loading saved operation…</p> : <>
      <p role="status">{run.operation} · {revisionRequested ? "Changes requested" : run.status}{!revisionRequested && run.stage ? ` · ${run.stage}` : ""}</p>
      <p>Model version {run.model_version}</p>
      {run.error && <p role="alert">{run.error}</p>}
      {revisionRequested && <p>{revisionRequestedMessage}</p>}
      {run.status === "failed" && !revisionRequested && <p>The operation failed. Inspect the saved error before requesting another attempt.</p>}
      {run.status === "awaiting_review" && <p>Waiting for human review. Use the review cards to inspect and decide.</p>}
      {run.status === "completed" && <p>Processing completed. Check the saved evidence for its result; completion does not grant approval.</p>}
      {onRequest && run.status === "failed" && !revisionRequested && run.operation !== "monitor" && canStartOperation(user.role, run.operation) && <button type="button" onClick={() => onRequest(`Inspect failed ${run.operation} run ${run.id} for model ${run.model_id}, version ${run.model_version}. Explain the failure and propose a new attempt if eligible. Ask for my explicit confirmation before starting it.`)}>Prepare retry request</button>}
      <a href={`/?model=${encodeURIComponent(modelId)}&tab=${encodeURIComponent(tab)}`}>Open model results</a>
      <details><summary>Operation reference</summary><p>{run.id}</p></details>
    </>}
  </section>;
}
