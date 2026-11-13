import { isRevisionRequested } from "./runReviewStatus";
import { useSavedUpdates } from "./useSavedUpdates";
import { AnalysisPanel } from "./LiveProposals";
import { ModelAuditHistory } from "./ModelAuditHistory";
import { RunContinuation } from "./RunContinuation";
import { useEffect, useRef, useState } from "react";
import { createModelClient, type RunRead } from "./api";

const api = createModelClient();
const pageSize = 20;

export function RunHistory({ modelId }: { modelId: string }) {
  const [offset, setOffset] = useState(0);
  const [revision, setRevision] = useState(0);
  const [rows, setRows] = useState<RunRead[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [selected, setSelected] = useState<string | null>(null);
  useEffect(() => {
    const c = new AbortController();
    setLoading(true);
    setRows([]);
    setError("");
    setSelected(null);
    api
      .listRuns(modelId, offset, pageSize, c.signal)
      .then((value) => {
        if (!c.signal.aborted) setRows(value);
      })
      .catch((e) => {
        if (!c.signal.aborted) setError(e.message);
      })
      .finally(() => {
        if (!c.signal.aborted) setLoading(false);
      });
    return () => c.abort();
  }, [modelId, offset, revision]);
  const element = useRef<HTMLElement>(null);
  const updates = useSavedUpdates({
    scope: `${modelId}:${offset}:${revision}`, enabled: !loading && !error, element,
    read: signal => api.listRuns(modelId, offset, pageSize, signal), apply: setRows,
  });
  return (
    <section ref={element} aria-label="Run history">
      <h2>Run history</h2>
      {updates && <p role="status">{updates}</p>}
      <p>
        Saved document, rebuild, validation and monitoring runs. Completion does
        not mean validation passed or human approval was granted.
      </p>
      <div className="evidence-actions">
        <button
          className="button secondary"
          disabled={loading}
          onClick={() => {setLoading(true);setRevision((v) => v + 1);}}
        >
          Refresh history
        </button>
        <button
          className="button secondary"
          disabled={loading || offset === 0}
          onClick={() => {setLoading(true);setOffset((v) => Math.max(0, v - pageSize));}}
        >
          Previous runs
        </button>
        <button
          className="button secondary"
          disabled={loading || !!error || rows.length < pageSize}
          onClick={() => {setLoading(true);setOffset((v) => v + pageSize);}}
        >
          Next runs
        </button>
      </div>
      <p>
        Page {offset / pageSize + 1}. Up to {pageSize} runs per page; the API
        supplies no total. A full page may be the last page.
      </p>
      {loading ? (
        <p role="status">Loading run history…</p>
      ) : error ? (
        <p role="alert">{error}</p>
      ) : (
        <>
          {!rows.length && (
            <p>
              {offset === 0
                ? "No runs returned for this model."
                : "No runs returned on this page. Use Previous runs to return."}
            </p>
          )}
          {!!rows.length && (
            <div className="inventory-scroll">
              <table aria-label="Saved runs">
                <thead>
                  <tr>
                    <th>Run</th>
                    <th>Operation</th>
                    <th>Status</th>
                    <th>Model version</th>
                    <th>Created</th>
                    <th>Stage</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((run) => (
                    <tr key={run.id}>
                      <td>
                        <button onClick={() => setSelected(run.id)}>
                          Inspect {run.id}
                        </button>
                      </td>
                      <td>{run.operation}</td>
                      <td>{isRevisionRequested(run) ? "Changes requested" : run.status}</td>
                      <td>{run.model_version}</td>
                      <td>{run.created_at}</td>
                      <td>{isRevisionRequested(run) ? "Review revision" : run.stage ?? "Not supplied"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          {selected && (
            <RunDetail key={selected} id={selected} modelId={modelId} />
          )}
        </>
      )}
      <p>
        Refresh reads saved state only; it never submits or retries a run. Full
        event coverage is described in Model event history below. Explicit
        continuation is a separate confirmed write for a recorded agent checkpoint.
      </p>
      <ModelAuditHistory key={modelId} modelId={modelId} />
    </section>
  );
}

function RunDetail({ id, modelId }: { id: string; modelId: string }) {
  const [run, setRun] = useState<RunRead | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [revision, setRevision] = useState(0);
  useEffect(() => {
    const c = new AbortController();
    setLoading(true);
    setRun(null);
    setError("");
    api
      .getRun(id, c.signal)
      .then((value) => {
        if (c.signal.aborted) return;
        if (value.model_id !== modelId)
          throw new Error("Backend returned a run for another model.");
        setRun(value);
      })
      .catch((e) => {
        if (!c.signal.aborted) setError(e.message);
      })
      .finally(() => {
        if (!c.signal.aborted) setLoading(false);
      });
    return () => c.abort();
  }, [id, modelId, revision]);
  const element = useRef<HTMLElement>(null);
  const updates = useSavedUpdates({
    scope: `${id}:${modelId}:${revision}`, enabled: !loading && !error && !!run && ["queued", "running"].includes(run.status), element,
    interval: 3000,
    read: async signal => {
      const saved = await api.getRun(id, signal);
      if (saved.id !== id || saved.model_id !== modelId) throw new Error("Backend returned a run for another model.");
      return saved;
    }, apply: setRun,
  });
  return (
    <section ref={element}
      aria-label="Selected run"
      className="lifecycle-card run-history-detail"
    >
      <h3>Run {id}</h3>
      {updates && <p role="status">{updates}</p>}
      <button
        className="button secondary"
        disabled={loading}
        onClick={() => {setLoading(true);setRevision((v) => v + 1);}}
      >
        Refresh run details
      </button>
      {loading ? (
        <p role="status">Loading run details…</p>
      ) : error ? (
        <p role="alert">{error}</p>
      ) : (
        run && (
          <>
            <dl>
              {Object.entries({
                "Model ID": run.model_id,
                "Model version": run.model_version,
                Dataset: run.dataset_id,
                Operation: run.operation,
                Status: isRevisionRequested(run) ? "Changes requested" : run.status,
                Stage: isRevisionRequested(run) ? "Review revision" : run.stage,
                Created: run.created_at,
                Started: run.started_at,
                Finished: run.finished_at,
              }).map(([label, value]) => (
                <div key={label}>
                  <dt>{label}</dt>
                  <dd>{value ?? "Not supplied"}</dd>
                </div>
              ))}
            </dl>
            {isRevisionRequested(run) && <p>This reviewed run is closed for revision. Inspect subsequent runs in History for follow-up work and its current status.</p>}
            {run.error && <p role="alert">Recorded run error: {run.error}</p>}
            {["validate", "monitor"].includes(run.operation) && <AnalysisPanel key={run.id} runId={run.id} modelId={modelId}/>}
            {run.status === "awaiting_review" && (
              <p>
                This run is paused for review. Record decisions in Review queue.
                Eligible agent runs can then be continued by the original operator.
              </p>
            )}
            {run.status === "awaiting_review" && ["document", "rebuild"].includes(run.operation) && (
              <RunContinuation key={`${run.id}:${revision}`} run={run} />
            )}
            {(run.status === "queued" || run.status === "running") && (
              <p>
                This run is still active. Its saved status updates automatically while visible.
              </p>
            )}
            <p>
              The table and selected run update independently from saved records.
            </p>
          </>
        )
      )}
    </section>
  );
}
