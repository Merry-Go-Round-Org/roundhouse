import type { MonitoringEvent, PsiBand, PsiWindow } from "./monitoring";
import { bandLabel } from "./monitoring";

const psi = (n: number) => n.toFixed(4);
function Band({ band }: { band: PsiBand }) {
  return <span className={"p2-band p2-band-" + band}>{bandLabel(band)}</span>;
}
export function MonitoringEvidence({ window, event }: {
  window: PsiWindow;
  event?: MonitoringEvent;
}) {
  const features = Object.entries(window.features).sort((a, b) =>
    b[1].psi - a[1].psi || a[0].localeCompare(b[0]));
  return (
    <article className="lifecycle-card p2-window" aria-label={"PSI window " + window.window_label}>
      <header className="p2-window-header">
        <div>
          <h3>{window.window_label}</h3>
          <p>{event ? event.created_at + " · Run " + event.run_id : "Latest persisted monitoring window"}</p>
        </div>
        <Band band={window.band} />
      </header>
      <p>
        <strong>{window.simulated ? "DECLARED SIMULATION" : "UPLOADED CSV BATCH"}</strong>
        {" · "}{window.status} · Frozen development baseline / {window.input_scope.replaceAll("_", " ")}
      </p>
      <div className="p2-window-stats">
        <div><span>Prediction-score PSI</span><strong>{psi(window.score.psi)}</strong><Band band={window.score_band} /></div>
        <div><span>Feature summary</span><strong>{features.length} features</strong><Band band={window.feature_band} /></div>
        <div><span>Reference / current rows</span><strong>{window.reference_rows} / {window.current_rows}</strong></div>
        <div><span>Revalidation required</span><strong>{window.revalidation_required ? "Yes — Backend-triggered" : "No flag recorded"}</strong></div>
      </div>
      {window.revalidation_required && (
        <p role="status" className="p2-alert">
          Red PSI breach requires revalidation. {window.revalidation_run_id
            ? "Queued validation run: " + window.revalidation_run_id
            : "Revalidation run ID not supplied; inspect run history."}
        </p>
      )}
      {window.status === "NOT_ASSESSABLE" && (
        <p role="status">Insufficient or unassessable evidence. This is neither a pass nor a confirmed breach.</p>
      )}
      {window.simulated && window.shift && (
        <p>Declared shift: <strong>{window.shift.feature}</strong> × {window.shift.factor}.
          This is an artificial shift of the frozen {window.input_scope === "validation_partition_simulation" ? "validation" : "test"} partition, not observed production drift.</p>
      )}
      {window.red_features.length > 0 && (
        <p>Red feature PSI: <strong>{window.red_features.join(", ")}</strong></p>
      )}
      <details>
        <summary>Feature-level PSI ({features.length})</summary>
        <div className="p2-table-scroll">
          <table aria-label="Feature PSI">
            <thead><tr><th>Feature / score</th><th>PSI</th><th>Band</th><th>Assessability</th></tr></thead>
            <tbody>
              <tr><td>Predicted score</td><td>{psi(window.score.psi)}</td>
                <td><Band band={window.score.band} /></td><td>{window.score.status}</td></tr>
              {features.map(([name, metric]) => (
                <tr key={name}><td>{name}</td><td>{psi(metric.psi)}</td>
                  <td><Band band={metric.band} /></td><td>{metric.status}</td></tr>
              ))}
            </tbody>
          </table>
        </div>
      </details>
      <details>
        <summary>PSI provenance and limitations</summary>
        <dl className="record-fields">
          {([
            ["Package name", window.model_id],
            ["Model artifact SHA-256", window.artifact_sha256],
            ["Frozen baseline SHA-256", window.baseline_sha256],
            ["Package SHA-256", window.package_sha256],
            ["Dataset version ID", window.dataset_version_id],
            ["Policy version", window.policy_version],
            ["Runtime image", window.runtime_image],
            ["Reference partition", window.baseline_partition],
            ["Baseline reference scope", window.reference_scope],
          ] as const).map(([key, value]) => (
            <div key={key}><dt>{key}</dt><dd>{value}</dd></div>
          ))}
        </dl>
        <ul>{window.limitations.map(note => <li key={note}>{note}</li>)}</ul>
      </details>
      <p className="p2-provenance">
        PSI describes marginal distribution drift; it does not measure accuracy,
        calibration, causality, concept drift or approval. Thresholds are project heuristics.
      </p>
    </article>
  );
}
