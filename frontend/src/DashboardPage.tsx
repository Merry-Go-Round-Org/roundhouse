import { useEffect, useState } from "react";
import { Activity, AlertTriangle, BarChart3, Coins, Database, ShieldCheck } from "lucide-react";
import { createModelClient } from "./api";
import type { DashboardSummary } from "./monitoring";

const api = createModelClient();
const seconds = (n: number | null) =>
  n === null ? "Not recorded" : n.toLocaleString(undefined, { maximumFractionDigits: 1 }) + " s";

function Counts({ title, values }: { title: string; values: Record<string, number> }) {
  const entries = Object.entries(values).sort((a, b) => b[1] - a[1]);
  const total = entries.reduce((sum, [, count]) => sum + count, 0);
  return (
    <section className="lifecycle-card" aria-label={title}>
      <h3>{title}</h3>
      {entries.length === 0 ? <p>No recorded states.</p> : (
        <div className="p2-counts">
          {entries.map(([name, count]) => (
            <div key={name} className="p2-count-row">
              <div className="p2-count-label"><span>{name.replaceAll("_", " ")}</span><strong>{count.toLocaleString()}</strong></div>
              <div className="p2-track" aria-hidden="true">
                <div style={{ width: (total ? 100 * count / total : 0) + "%" }} />
              </div>
            </div>
          ))}
        </div>
      )}
    </section>
  );
}

export function DashboardPage({ refresh, openMonitoring }: {
  refresh: number;
  openMonitoring: () => void;
}) {
  const [data, setData] = useState<DashboardSummary | null>(null);
  const [busy, setBusy] = useState(true);
  const [error, setError] = useState("");
  useEffect(() => {
    const controller = new AbortController();
    setBusy(true); setError(""); setData(null);
    api.getDashboard(controller.signal).then(value => {
      if (!controller.signal.aborted) setData(value);
    }).catch((e: Error) => {
      if (!controller.signal.aborted) setError(e.message);
    }).finally(() => {
      if (!controller.signal.aborted) setBusy(false);
    });
    return () => controller.abort();
  }, [refresh]);
  return (
    <section aria-label="Live dashboard" className="p2-dashboard">
      <header className="p2-section-head">
        <div><p className="eyebrow">LIVE · PERSISTED RECORDS</p><h2>Portfolio dashboard</h2></div>
        <button className="button secondary" onClick={openMonitoring}>Inspect PSI monitoring</button>
      </header>
      <p className="page-description">Portfolio activity, review progress and recorded costs.</p>
      {busy && <p role="status">Loading persisted dashboard…</p>}
      {error && <p role="alert">{error}</p>}
      {data && !busy && (
        <>
          <div className="p2-kpis">
            {([
              ["Models", data.models, Database],
              ["Reports", data.reports, BarChart3],
              ["PSI events", data.monitoring_events, Activity],
              ["Revalidation due", data.red_alerts, AlertTriangle],
              ["Packages", data.packages, ShieldCheck],
            ] as const).map(([name, count, Icon]) => (
              <article className="p2-kpi" key={name}>
                <Icon size={19} aria-hidden="true" />
                <span>{name}</span><strong>{count.toLocaleString()}</strong>
              </article>
            ))}
          </div>

          <div className="p2-grid">
            <Counts title="Model lifecycle" values={data.lifecycle} />
            <Counts title="Run states" values={data.runs} />
            <Counts title="Review decisions" values={data.reviews} />
            <Counts title="Finding states" values={data.findings} />
            <Counts title="Scoring states" values={data.scoring_runs} />
            <section className="lifecycle-card" aria-label="Recorded elapsed time and cost">
              <h3>Recorded elapsed time and cost</h3>
              <div className="p2-measures">
                <div><span>Measured review opens</span><strong>{data.review_elapsed_seconds.count}</strong></div>
                <div><span>Mean elapsed since explicit review open</span><strong>{seconds(data.review_elapsed_seconds.mean)}</strong></div>
                <div><span>Total review-open elapsed time</span><strong>{seconds(data.review_elapsed_seconds.total)}</strong></div>
                <div><span>Recorded LLM cost (USD)</span><strong><Coins size={15} aria-hidden="true" /> {"$" + data.llm_cost_usd.toFixed(4)}</strong></div>
              </div>
              <p className="page-description">Elapsed time is not time actively spent reviewing. Costs may be estimates.</p>
            </section>
          </div>
          <section className="lifecycle-card" aria-label="Stage elapsed durations">
            <h3>Run stage elapsed durations</h3>
            {Object.keys(data.stage_elapsed_seconds).length === 0
              ? <p>No completed stage durations recorded.</p>
              : <div className="p2-table-scroll"><table>
                  <thead><tr><th>Operation</th><th>Runs timed</th><th>Mean elapsed</th><th>Total elapsed</th></tr></thead>
                  <tbody>{Object.entries(data.stage_elapsed_seconds).map(([op, v]) => (
                    <tr key={op}><td>{op}</td><td>{v.count}</td>
                      <td>{seconds(v.mean)}</td><td>{seconds(v.total)}</td></tr>
                  ))}</tbody>
                </table></div>}
          </section>
          <details className="help-details" aria-label="Dashboard measurement limitations">
            <summary>How to read these numbers</summary><p>Revalidation due counts models, not drift events. These figures describe activity, not model approval or measured savings.</p>
            <ul>{data.measurement_limitations.map(line => <li key={line}>{line}</li>)}</ul>
          </details>
        </>
      )}
    </section>
  );
}
