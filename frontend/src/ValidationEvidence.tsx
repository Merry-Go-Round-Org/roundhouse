import { useId } from "react";
import { AgentValidationReview } from "./AgentValidationReview";
import { Gate3Requirements } from "./Gate3Requirements";
import type {
  ValidationEvidence as Evidence,
  ValidationCheck,
  MetricStatus,
} from "./validation";
import { statusDisplay } from "./validation";
import {
  groupChecks,
  record,
  numericEvidence,
  intervalEvidence,
} from "./validationSections";
export function EvidenceStatus({ status }: { status: MetricStatus }) {
  const display = statusDisplay(status);
  return (
    <span className={"evidence-status " + display.tone}>{display.label}</span>
  );
}
function DetailFields({
  value,
  depth = 0,
}: {
  value: unknown;
  depth?: number;
}) {
  if (value === null || value === undefined) return <span>Not supplied</span>;
  if (typeof value !== "object") return <span>{String(value)}</span>;
  if (depth >= 4) return <pre>{JSON.stringify(value, null, 2)}</pre>;
  const entries = Object.entries(value);
  if (!entries.length) return <span>No details supplied</span>;
  return (
    <dl className="record-fields evidence-fields">
      {entries.map(([key, child]) => (
        <div key={key}>
          <dt>{Array.isArray(value) ? "Item " + (Number(key) + 1) : key}</dt>
          <dd>
            {child && typeof child === "object" ? (
              <details>
                <summary>Inspect {key}</summary>
                <DetailFields value={child} depth={depth + 1} />
              </details>
            ) : (
              <DetailFields value={child} depth={depth + 1} />
            )}
          </dd>
        </div>
      ))}
    </dl>
  );
}
function CheckCard({ check }: { check: ValidationCheck }) {
  return (
    <article className="lifecycle-card">
      <h4>{check.check_id}</h4>
      <EvidenceStatus status={check.status} />
      <p>
        {check.required ? "Required check" : "Supporting check"} · Evidence
        kind: {check.evidence_kind || "Not supplied"}
      </p>
      <p>{check.summary}</p>
      {check.evidence_kind === "fresh_execution" && (
        <p>
          Fresh-execution category; the reported status determines whether
          execution evidence is available. NOT_ASSESSABLE does not mean it ran.
        </p>
      )}
      <DetailFields value={check.details} />
      <details>
        <summary>Original check JSON</summary>
        <pre>{JSON.stringify(check, null, 2)}</pre>
      </details>
    </article>
  );
}
function SavedPerformance({ evidence }: { evidence: Evidence }) {
  if (evidence.task !== "binary_classification")
    return (
      <div>
        <p>
          Binary performance metrics are not applicable to this synthetic
          survival report.
        </p>
        <p>
          Synthetic C-index (reported):{" "}
          {numericEvidence(evidence.saved_metrics.c_index)}
        </p>
      </div>
    );
  const validation = record(evidence.saved_metrics.validation),
    test = record(evidence.saved_metrics.test);
  const rows = [
    ["Rows", "rows"],
    ["Positives", "positives"],
    ["ROC AUC", "roc_auc"],
    ["Gini", "gini"],
    ["KS", "ks"],
    ["PR AUC / average precision", "pr_auc"],
    ["Precision", "precision"],
    ["Recall", "recall"],
    ["F1", "f1"],
    ["Threshold", "threshold"],
  ];
  return (
    <div>
      <p>
        Historical saved evaluation — not a newly executed holdout. These
        numbers do not establish performance acceptance. No metric or threshold
        is calculated in the browser.
      </p>
      <div className="inventory-scroll">
        <table aria-label="Saved performance metrics">
          <thead>
            <tr>
              <th>Metric</th>
              <th>Saved validation partition</th>
              <th>Saved test partition</th>
            </tr>
          </thead>
          <tbody>
            {rows.map(([label, key]) => (
              <tr key={key}>
                <th scope="row">{label}</th>
                <td>{numericEvidence(validation[key])}</td>
                <td>{numericEvidence(test[key])}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {["validation", "test"].map((part) => (
        <details key={part}>
          <summary>Saved {part} confusion matrix and alert budget</summary>
          <DetailFields
            value={{
              confusion_matrix:
                record(evidence.saved_metrics[part]).confusion_matrix ?? null,
              alert_budget:
                record(evidence.saved_metrics[part]).alert_budget ?? null,
            }}
          />
        </details>
      ))}
    </div>
  );
}
function Calibration({ evidence }: { evidence: Evidence }) {
  if (evidence.task !== "binary_classification")
    return (
      <p>
        Binary calibration and AUC confidence intervals are not applicable to
        this synthetic survival report.
      </p>
    );
  return (
    <div>
      <p>
        Saved calibration and uncertainty evidence requires human review.
        Missing intervals or bins remain unavailable; no calibration clearance
        is inferred.
      </p>
      <div className="inventory-scroll">
        <table aria-label="Saved calibration and uncertainty">
          <thead>
            <tr>
              <th>Saved partition</th>
              <th>Brier score</th>
              <th>ROC AUC 95% interval</th>
              <th>PR AUC 95% interval</th>
              <th>Bootstrap resamples</th>
              <th>Calibration bins available</th>
            </tr>
          </thead>
          <tbody>
            {["validation", "test"].map((part) => {
              const metrics = record(evidence.saved_metrics[part]),
                bootstrap = record(metrics.bootstrap);
              return (
                <tr key={part}>
                  <th scope="row">{part}</th>
                  <td>{numericEvidence(metrics.brier)}</td>
                  <td>{intervalEvidence(bootstrap.roc_auc_ci95)}</td>
                  <td>{intervalEvidence(bootstrap.pr_auc_ci95)}</td>
                  <td>{numericEvidence(bootstrap.resamples)}</td>
                  <td>
                    {typeof metrics.calibration_bins_available === "boolean"
                      ? metrics.calibration_bins_available
                        ? "Yes (reported)"
                        : "No (reported)"
                      : "Not supplied"}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      <p>
        Full interval methods, assumptions and available group details are
        retained in the check details and original saved metrics below.
      </p>
    </div>
  );
}
export function ValidationEvidence({
  evidence,
  backendModelId,
}: {
  evidence: Evidence;
  backendModelId: string;
}) {
  const prefix = useId(),
    groups = groupChecks(evidence.checks);
  const findings = evidence.checks.filter(
    (c) =>
      evidence.summary.blocking_check_ids.includes(c.check_id) ||
      evidence.summary.review_check_ids.includes(c.check_id),
  );
  const jump = (id: string) => {
    const target = document.getElementById(prefix + id);
    target?.scrollIntoView({ block: "start" });
    target?.focus({ preventScroll: true });
  };
  return (
    <section
      className="validation-evidence"
      aria-label="Canonical validation evidence"
    >
      <h3>Validation evidence overview</h3>
      <p>
        <strong>{evidence.summary.overall_status}</strong>
      </p>
      <p>
        Human approval: not granted by this report. Authenticated review is
        separate.
      </p>
      <p>
        Required checks passed:{" "}
        {evidence.summary.required_checks_passed
          ? "Yes (reported by producer)"
          : "No"}
      </p>
      <p>
        Blockers:{" "}
        {evidence.summary.blocking_check_ids.join(", ") || "None reported"}
      </p>
      <p>
        Review checks:{" "}
        {evidence.summary.review_check_ids.join(", ") || "None reported"}
      </p>
      {evidence.model_kind === "synthetic_demo" && (
        <p className="notice">
          Synthetic demo — not a validated public-data model.
        </p>
      )}
      <Gate3Requirements checks={evidence.checks} />
      <AgentValidationReview review={evidence.agent_review} challenger={evidence.challenger} />
      <nav className="validation-navigation" aria-label="Validation sections">
        {groups.map((g) => (
          <button
            className="button secondary"
            key={g.id}
            onClick={() => jump(g.id)}
          >
            {g.title}
          </button>
        ))}
        <button className="button secondary" onClick={() => jump("findings")}>
          Findings requiring attention
        </button>
        <button className="button secondary" onClick={() => jump("provenance")}>
          Report provenance
        </button>
      </nav>
      {groups.map((group) => (
        <section key={group.id} aria-label={group.title}>
          <h3 id={prefix + group.id} tabIndex={-1}>
            {group.title}
          </h3>
          {group.id === "performance" && (
            <SavedPerformance evidence={evidence} />
          )}
          {group.id === "calibration" && <Calibration evidence={evidence} />}
          {group.id === "stability" && (
            <p>
              Dedicated score PSI and per-feature PSI fields are not mapped yet.
              Check Other reported checks for newer evidence. Out-of-time
              applicability is a separate check, not a PSI result. The browser
              does not compute PSI or infer a green result.
            </p>
          )}
          {group.id === "fairness" && (
            <p>
              {evidence.domain === "ulb"
                ? "ULB has no published protected-group attributes; demographic fairness cannot be established from its fields."
                : evidence.domain === "uci"
                  ? "UCI group metrics are descriptive. Review group uncertainty, sample sizes and limitations; a technical result is not fairness clearance."
                  : "Synthetic demonstration results do not establish real-population fairness."}
            </p>
          )}
          {group.id === "migration" && (
            <p>
              For a rebuild candidate, this cites the recorded official parity
              run on the exact candidate bundle, on development fixture rows
              only. Legacy R models are not assessable here and independently
              fitted Python models report N/A; missing parity is never
              converted to a pass.
            </p>
          )}
          {group.id === "execution" && (
            <p>
              Execution requested: {evidence.execution_requested ? "Yes" : "No"}
              . Inspect each reported status and evidence kind. A request or a
              fresh_execution label alone does not prove a successful scorer
              run.
            </p>
          )}
          {group.id === "other" && (
            <p>
              Unrecognized check IDs are preserved here so newer backend
              evidence is not silently dropped.
            </p>
          )}
          {group.checks.length ? (
            group.checks.map((c) => <CheckCard key={c.check_id} check={c} />)
          ) : (
            <p>
              No checks supplied for this section. No pass or applicability
              decision is inferred.
            </p>
          )}
        </section>
      ))}
      <section aria-label="Findings requiring attention">
        <h3 id={prefix + "findings"} tabIndex={-1}>
          Findings requiring attention
        </h3>
        <p>
          These are checks listed by the producer as blocking or requiring
          review, not persisted findings records. Severity, owner, due date and
          resolution state are not supplied. No review decision is submitted
          here.
        </p>
        {findings.length ? (
          <div className="inventory-scroll">
            <table>
              <thead>
                <tr>
                  <th>Check</th>
                  <th>Producer flags</th>
                  <th>Status</th>
                  <th>Reason</th>
                </tr>
              </thead>
              <tbody>
                {findings.map((c) => (
                  <tr key={c.check_id}>
                    <th scope="row">{c.check_id}</th>
                    <td>
                      {[
                        evidence.summary.blocking_check_ids.includes(c.check_id)
                          ? "Blocking"
                          : "",
                        evidence.summary.review_check_ids.includes(c.check_id)
                          ? "Review"
                          : "",
                      ]
                        .filter(Boolean)
                        .join(" / ")}
                    </td>
                    <td>
                      <EvidenceStatus status={c.status} />
                    </td>
                    <td>{c.summary}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <p>
            No blocking/review checks listed by the producer. This is not
            approval.
          </p>
        )}
      </section>
      <section aria-label="Report provenance">
        <h3 id={prefix + "provenance"} tabIndex={-1}>
          Report provenance
        </h3>
        <dl className="record-fields">
          {Object.entries({
            "Backend model": backendModelId,
            "Legacy model declared in artifact": evidence.model_id,
            "Model kind": evidence.model_kind,
            Task: evidence.task,
            Domain: evidence.domain,
            "Model artifact SHA-256":
              evidence.artifact_sha256 ?? "Not supplied",
            Generated: evidence.generated_at_utc,
            "Policy version": evidence.policy_version,
            "Model version / sandbox image / started by":
              "Not supplied by this artifact",
          }).map(([k, v]) => (
            <div key={k}>
              <dt>{k}</dt>
              <dd>{v}</dd>
            </div>
          ))}
        </dl>
        <p>
          The backend must verify UUID-to-legacy-model/version mapping and
          hashes during ingestion. The browser does not establish that mapping
          from names.
        </p>
        <details>
          <summary>Original saved metrics — not newly executed holdout</summary>
          <pre>{JSON.stringify(evidence.saved_metrics, null, 2)}</pre>
        </details>
        <h3>Limitations</h3>
        {evidence.limitations.length ? (
          <ul>
            {evidence.limitations.map((s, i) => (
              <li key={i}>{s}</li>
            ))}
          </ul>
        ) : (
          <p>No limitations supplied by the report.</p>
        )}
      </section>
    </section>
  );
}
