import { useSavedUpdates } from "./useSavedUpdates";
import { DocumentComparison } from "./DocumentComparison";
import { DocumentReader } from "./DocumentReader";
import { useEffect, useRef, useState } from "react";
import { createModelClient, type ReportRead } from "./api";
import { ValidationEvidence, EvidenceStatus } from "./ValidationEvidence";
import type { ValidationEvidence as Evidence } from "./validation";
const api = createModelClient();
export function ModelDocuments({
  modelId,
  kind,
}: {
  modelId: string;
  kind?: ReportRead["kind"];
}) {
  const [previewDocument, setDocument] = useState<{
    reportId: string;
    text: string;
  } | null>(null);
  const [reports, setReports] = useState<ReportRead[] | null>(null);
  const [evidence, setEvidence] = useState<{
    reportId: string;
    value: Evidence;
  } | null>(null);
  const [busy, setBusy] = useState(false),
    [error, setError] = useState("");
  const controller = useRef<AbortController | null>(null),
    lock = useRef(false);
  useEffect(() => () => controller.current?.abort(), []);
  const element = useRef<HTMLElement>(null);
  const [listRevision, setListRevision] = useState(0);
  useEffect(() => {
    const c = new AbortController();
    setReports(null); setDocument(null); setEvidence(null); setError(""); setBusy(true);
    lock.current = true; controller.current = c;
    api.listReports(modelId, c.signal).then(rows => {
      if (!c.signal.aborted) setReports(kind ? rows.filter(r => r.kind === kind) : rows);
    }).catch(e => { if (!c.signal.aborted) setError(e.message); }).finally(() => {
      if (controller.current === c) { lock.current = false; if (!c.signal.aborted) setBusy(false); }
    });
    return () => { c.abort(); lock.current = false; };
  }, [modelId, kind]);
  const updates = useSavedUpdates({
    scope: `${modelId}:${kind}:${listRevision}`, enabled: reports !== null && !busy && !error, element,
    read: signal => api.listReports(modelId, signal),
    apply: rows => {
      const next = kind ? rows.filter(r => r.kind === kind) : rows;
      const unchanged = (id: string) => {
        const before = reports?.find(r => r.id === id), after = next.find(r => r.id === id);
        return before && after && before.artifact_id === after.artifact_id &&
          before.artifact_version === after.artifact_version && before.artifact_sha256 === after.artifact_sha256;
      };
      if (previewDocument && !unchanged(previewDocument.reportId)) setDocument(null);
      if (evidence && !unchanged(evidence.reportId)) setEvidence(null);
      setReports(next);
    },
    failed: () => { setDocument(null); setEvidence(null); },
  });
  async function action(work: (signal: AbortSignal) => Promise<void>) {
    if (lock.current) return;
    lock.current = true;
    controller.current = new AbortController();
    const signal = controller.current.signal;
    setBusy(true);
    setError("");
    try {
      await work(signal);
    } catch (e) {
      if (!signal.aborted) setError((e as Error).message);
    } finally {
      lock.current = false;
      if (!signal.aborted) setBusy(false);
    }
  }
  function load() {
    setListRevision(v => v + 1);
    return action(async (signal) => {
      setReports(null);
      setDocument(null);
      setEvidence(null);
      const rows = await api.listReports(modelId, signal);
      if (!signal.aborted)
        setReports(kind ? rows.filter((r) => r.kind === kind) : rows);
    });
  }
  function inspect(report: ReportRead) {
    return action(async (signal) => {
      setEvidence(null);
      const value = await api.getValidationEvidence(report.artifact_id, signal);
      if (!signal.aborted) setEvidence({ reportId: report.id, value });
    });
  }
  function preview(report: ReportRead) {
    return action(async (signal) => {
      setDocument(null);
      const text = await api.getDocumentText(report.artifact_id, signal);
      if (!signal.aborted) setDocument({ reportId: report.id, text });
    });
  }
  function download(report: ReportRead, markdown = false) {
    return action(async (signal) => {
      const blob = markdown
        ? await api.downloadReportMarkdown(report.id, signal)
        : await api.downloadArtifact(report.artifact_id, signal);
      if (signal.aborted) return;
      const url = URL.createObjectURL(blob),
        link = document.createElement("a");
      link.href = url;
      link.download = `report-${report.id.replace(/[^a-zA-Z0-9_-]/g, "_")}${markdown ? ".md" : ""}`;
      document.body.append(link);
      link.click();
      link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    });
  }
  return (
    <section
      ref={element}
      className="model-documents"
      aria-label="Model documentation and reports"
    >
      <h2>
        {kind === "validation"
          ? "Validation reports"
          : "Documentation and reports"}
      </h2>
      <p className="page-description">Read generated reports below, or browse uploaded source files to find original notes.</p>
      <button
        className="button secondary"
        onClick={() => void load()}
        disabled={busy}
      >
        {busy ? "Loading…" : "View available documents and reports"}
      </button>
      {updates && <p role="status">{updates}</p>}
      {error && (
        <p role="alert">
          {error} Document/report access requires backend support.
        </p>
      )}
      {reports && reports.length === 0 && (
        <p>No {kind ?? "documents or"} reports returned for this model.</p>
      )}
      {reports?.map((r, index) => (
        <article className="lifecycle-card" key={r.id}>
          <h3>{r.kind === "documentation" ? "Agent-generated documentation" : r.kind === "validation" ? "Validation report" : "Model report"} {index + 1}</h3>
          <p>Model version: {r.model_version ?? "Not supplied"}</p>
          <p>Human review: {r.review_status === "pending" ? "Awaiting review" : r.review_status === "decided" ? "Decision recorded — inspect Review queue" : r.review_status ?? "No review record supplied"}. A generated report is not approval.</p>
          {r.review_resolution && <section aria-label="Recorded report decision">
            <h4>{r.review_resolution.decision.replaceAll("_", " ")}</h4>
            <p>{r.review_resolution.rationale}</p>
            {r.review_resolution.conditions.length > 0 && <><h4>Recorded conditions</h4><ul>{r.review_resolution.conditions.map((condition, i) => <li key={i}>{condition}</li>)}</ul></>}
            <p>Check statuses remain unchanged.</p>
          </section>}
          <details className="help-details"><summary>Report identifiers and checksums</summary>
            <p>Report: {r.id} · Run: {r.run_id}</p>
            <p>Artifact version: {r.artifact_version ?? "Not supplied"} · SHA-256: {r.artifact_sha256 ?? "Not supplied"}</p>
            <p>Review: {r.review_id ?? "Not supplied"}</p>
          </details>
          {r.metrics.map((m, i) => (
            <div className="metric-evidence" key={i}>
              <p>
                {m.name}: {m.value ?? "Not supplied"} ·{" "}
                <EvidenceStatus status={m.status} />
              </p>
              <p>{m.reason}</p>
            </div>
          ))}
          <div className="evidence-actions">
            {r.kind === "documentation" && (
              <button
                className="button secondary"
                disabled={busy}
                onClick={() => void preview(r)}
              >
                Read documentation
              </button>
            )}
            {r.kind === "validation" && (
              <button
                className="button secondary"
                disabled={busy}
                onClick={() => void inspect(r)}
              >
                View validation evidence
              </button>
            )}
            <button
              className="button secondary"
              disabled={busy}
              onClick={() => void download(r)}
            >
              Download {r.kind} artifact
            </button>
            <button className="button secondary" disabled={busy} onClick={() => void download(r, true)}>
              Download report as Markdown
            </button>
          </div>
          {previewDocument?.reportId === r.id && (
            <DocumentReader key={r.id} source={previewDocument.text} reportId={r.id} modelId={modelId} />
          )}
          {evidence?.reportId === r.id && (
            <ValidationEvidence
              evidence={evidence.value}
              backendModelId={modelId}
            />
          )}
        </article>
      ))}
      {kind !== "validation" && <DocumentComparison key={modelId} modelId={modelId} />}
      {kind !== "validation" && (
        <p>Use Generate new documentation to request another draft.</p>
      )}
      {kind === "validation" && (
        <p>Inspect findings below, then use Review queue for validation sign-off.</p>
      )}
    </section>
  );
}
