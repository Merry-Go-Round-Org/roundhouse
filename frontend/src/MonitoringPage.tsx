import { useSavedUpdates } from "./useSavedUpdates";
import { useEffect, useRef, useState } from "react";
import { createModelClient } from "./api";
import type { PortfolioMonitoringRow } from "./monitoring";
import { bandLabel } from "./monitoring";
import { MonitoringEvidence } from "./MonitoringEvidence";

const api = createModelClient();
const pageSize = 20;

export function MonitoringPage({ refresh, openModel }: {
  refresh: number;
  openModel: (id: string) => void;
}) {
  const [offset, setOffset] = useState(0);
  const [rows, setRows] = useState<PortfolioMonitoringRow[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [busy, setBusy] = useState(true);
  const [error, setError] = useState("");
  useEffect(() => {
    const c = new AbortController();
    setRows([]); setError(""); setBusy(true); setSelected(null);
    api.listPortfolioMonitoring(offset, pageSize, c.signal).then(value => {
      if (!c.signal.aborted) setRows(value);
    }).catch((e: Error) => {
      if (!c.signal.aborted) setError(e.message);
    }).finally(() => {
      if (!c.signal.aborted) setBusy(false);
    });
    return () => c.abort();
  }, [offset, refresh]);
  const element = useRef<HTMLElement>(null);
  const updates = useSavedUpdates({
    scope: `${offset}:${refresh}`, enabled: !busy && !error, element,
    read: signal => api.listPortfolioMonitoring(offset, pageSize, signal), apply: setRows,
  });
  const current = rows.find(row => row.model_id === selected);
  const withEvidence = rows.filter(row => row.latest !== null).length;
  const redOnPage = rows.filter(row => row.latest?.revalidation_required).length;
  return (
    <section ref={element} aria-label="Live monitoring" className="p2-monitoring">
      <header className="p2-section-head">
        <div><p className="eyebrow">LIVE · PSI</p><h2>Monitoring portfolio</h2></div>
      </header>
      <p className="page-description">Latest drift results. Counts cover this page only.</p>
      <details className="help-details"><summary>About monitoring</summary><p>PSI compares each model with its fixed development baseline. Simulations are labelled separately from uploaded observations. Each page contains up to {pageSize} models; use Dashboard for portfolio totals.</p></details>
      {updates && <p role="status">{updates}</p>}
      {busy && <p role="status">Loading monitoring portfolio…</p>}
      {error && <p role="alert">{error}</p>}
      {!busy && !error && (
        <>
          <div className="p2-kpis">
            <article className="p2-kpi"><span>Models on page</span><strong>{rows.length}</strong></article>
            <article className="p2-kpi"><span>With PSI evidence</span><strong>{withEvidence}</strong></article>
            <article className="p2-kpi"><span>Revalidation flagged on page</span><strong>{redOnPage}</strong></article>
          </div>
          {!rows.length ? <p>No models returned for this portfolio page.</p> : (
            <div className="p2-table-scroll">
              <table aria-label="Monitoring portfolio records">
                <thead><tr><th>Model</th><th>Lifecycle</th><th>Latest window</th><th>PSI band</th><th>Last evaluation</th><th>Inspect</th></tr></thead>
                <tbody>{rows.map(row => (
                  <tr key={row.model_id}>
                    <td>{row.model_name}<small className="p2-secondary">{row.model_id}</small></td>
                    <td>{row.lifecycle_status}</td>
                    <td>{row.latest ? (
                      <>{row.latest.window_label}
                        <small className="p2-secondary">{row.latest.simulated ? "Declared simulation" : "Uploaded batch"}</small>
                      </>
                    ) : "No persisted PSI evidence"}</td>
                    <td>{row.latest ? <span className={"p2-band p2-band-" + row.latest.band}>{bandLabel(row.latest.band)}</span> : "Not evaluated"}</td>
                    <td>{row.last_run ?? "None recorded"}</td>
                    <td>
                      <button className="button secondary" onClick={() => openModel(row.model_id)}>
                        Open model monitoring
                      </button>
                      {row.latest && (
                        <button className="button secondary" onClick={() => setSelected(row.model_id === selected ? null : row.model_id)}>
                          {row.model_id === selected ? "Hide PSI" : "Inspect latest PSI"}
                        </button>
                      )}
                    </td>
                  </tr>
                ))}</tbody>
              </table>
            </div>
          )}
          {current?.latest && <MonitoringEvidence window={current.latest} />}
        </>
      )}
      <div className="pagination">
        <button type="button" disabled={busy || offset === 0}
          onClick={() => {setBusy(true);setOffset(old => Math.max(0, old - pageSize));}} aria-label="Previous monitoring page">Previous</button>
        <span>Page {Math.floor(offset / pageSize) + 1}</span>
        <button type="button" disabled={busy || !!error || rows.length < pageSize}
          onClick={() => {setBusy(true);setOffset(old => old + pageSize);}} aria-label="Next monitoring page">Next</button>
      </div>

    </section>
  );
}
