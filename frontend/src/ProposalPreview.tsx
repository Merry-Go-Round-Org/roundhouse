import { useState } from "react";
import { ProposalCard, type ProposalView } from "./ProposalCard";
import "./proposalPreview.css";

const base = {
  revision: "fixture-1",
  model: "Credit risk model · sample",
  modelVersion: "1.0.0",
  sourceRun: "fixture-run",
  requiredRole: "Validator (proposed)",
  status: "pending" as const,
};
const fixtures: Record<string, ProposalView> = {
  Validation: {
    ...base,
    id: "fixture-finding",
    title: "Open a validation finding",
    origin: "Validation agent · sample",
    summary:
      "The agent suggests recording a missing preprocessing explanation for human review.",
    fields: [
      { label: "Finding", value: "Explain how missing values are handled" },
      { label: "Severity", value: "Medium" },
      {
        label: "Why this severity",
        value:
          "The scoring behavior needs a clear explanation before independent review.",
      },
      {
        label: "Impact",
        value:
          "Reviewers cannot fully trace how missing inputs affect predictions.",
      },
      {
        label: "Suggested remediation",
        value:
          "Document the preprocessing and add a reproducible missing-value example.",
      },
    ],
    evidence: [
      { kind: "Report section", reference: "Preprocessing / missing values" },
      { kind: "Check", reference: "DOC-01 (sample)" },
    ],
    technical: {
      type: "open_finding",
      args: {
        title: "Explain how missing values are handled",
        severity: "medium",
        severity_rationale:
          "The scoring behavior needs a clear explanation before independent review.",
        impact:
          "Reviewers cannot fully trace how missing inputs affect predictions.",
        remediation:
          "Document the preprocessing and add a reproducible missing-value example.",
        check_ids: ["DOC-01"],
        report_section: "Preprocessing / missing values",
      },
    },
  },
  Monitoring: {
    ...base,
    id: "fixture-monitoring",
    title: "Start revalidation",
    origin: "Investigation agent · sample",
    summary:
      "The agent recommends revalidation after investigating a changed input distribution.",
    fields: [
      {
        label: "Action",
        value: "Start a validation run that includes the monitoring batch",
      },
      {
        label: "Reason",
        value:
          "The observed change needs a fresh assessment against the frozen development baseline.",
      },
      { label: "Monitoring window", value: "Sample November batch" },
    ],
    evidence: [
      { kind: "Monitoring run", reference: "fixture-monitoring-run" },
      {
        kind: "Report section",
        reference: "Investigation / recommended actions",
      },
    ],
    technical: {
      type: "start_revalidation",
      args: {
        reason:
          "The observed change needs a fresh assessment against the frozen development baseline.",
      },
      target: {
        model_id: "fixture-model",
        monitoring_run_id: "fixture-monitoring-run",
      },
    },
  },
  Chat: {
    ...base,
    id: "fixture-chat",
    title: "Propose a use restriction",
    origin: "Chat agent · sample",
    requiredRole: "Approver (proposed)",
    summary:
      "The agent suggests a temporary restriction for a person to review. Chat text alone cannot approve it.",
    fields: [
      {
        label: "Restriction",
        value: "Require manual review for cases with missing income values",
      },
      {
        label: "Reason",
        value: "The treatment of missing income is still under investigation.",
      },
    ],
    evidence: [
      {
        kind: "Report section",
        reference: "Validation / outstanding findings",
      },
    ],
    technical: {
      type: "restrict_use",
      args: {
        restriction:
          "Require manual review for cases with missing income values",
        reason: "The treatment of missing income is still under investigation.",
      },
    },
  },
  "Intake plan": {
    ...base,
    id: "fixture-intake",
    title: "Review an intake plan",
    origin: "Intake agent · design example",
    requiredRole: "Gate 0 role awaiting agreement",
    summary:
      "Review the candidate grouping and proposed metadata before creating inventory records. This is a design example, not a supported proposal action.",
    fields: [
      { label: "Candidate files", value: "model.rds, score.R, notes.md" },
      { label: "Unused files", value: "old-chart.png" },
      { label: "Missing information", value: "Business owner and actual use" },
      {
        label: "Algorithm",
        value: "Logistic regression — read from the artifact",
      },
      {
        label: "Intended use",
        value:
          "Credit risk estimation — taken from an unverified document; needs confirmation",
      },
      {
        label: "Dataset mapping",
        value: "uci_credit — agent suggestion; needs confirmation",
      },
    ],
    evidence: [
      { kind: "File", reference: "notes.md" },
      { kind: "Artifact", reference: "model.rds" },
    ],
    technical: {
      note: "Gate 0 action/HTTP schema and editable field rules are pending. No invented action type or payload.",
    },
  },
};
export default function ProposalPreview() {
  const [context, setContext] = useState("Validation");
  const [state, setState] = useState<ProposalView["status"]>("pending");
  const [readOnly, setReadOnly] = useState(false);
  const [revision, setRevision] = useState(1);
  const proposal = {
    ...fixtures[context],
    revision: `fixture-${revision}`,
    status: state,
  };
  return (
    <main className="proposal-preview">
      <header>
        <a href="/demo">Back to sample workspace</a>
        <p className="proposal-preview-label">
          Design preview · sample data only
        </p>
        <h1>Agent proposals</h1>
        <p>
          Review the suggested action, its evidence and the decision separately.
        </p>
      </header>
      <aside
        className="proposal-preview-notice"
        aria-label="Preview limitations"
      >
        These examples are not connected to your models or the backend.
        Approvals, editable proposals, Gate 0 and execution still need agreed
        APIs.
      </aside>
      <nav className="proposal-preview-contexts" aria-label="Proposal examples">
        {Object.keys(fixtures).map((name) => (
          <button
            type="button"
            key={name}
            aria-pressed={context === name}
            onClick={() => {
              setContext(name);
              setState("pending");
              setRevision(1);
            }}
          >
            {name}
          </button>
        ))}
      </nav>
      <details className="proposal-preview-controls">
        <summary>Preview states</summary>
        <label>
          Example status
          <select
            value={state}
            onChange={(event) =>
              setState(event.target.value as ProposalView["status"])
            }
          >
            <option value="pending">Pending</option>
            <option value="approved">Approved</option>
            <option value="rejected">Rejected</option>
            <option value="superseded">Superseded</option>
            <option value="expired">Expired</option>
            <option value="executed">Executed</option>
          </select>
        </label>
        <label>
          <input
            type="checkbox"
            checked={readOnly}
            onChange={(event) => setReadOnly(event.target.checked)}
          />
          Read-only reviewer example
        </label>
        <button type="button" onClick={() => setRevision((value) => value + 1)}>
          Simulate revised proposal
        </button>
      </details>
      <ProposalCard
        proposal={proposal}
        previewDecisions={context !== "Intake plan"}
        unavailableReason={
          context === "Intake plan"
            ? "Gate 0 review and editing are awaiting the backend contract."
            : readOnly
              ? "This reviewer example has read-only access."
              : undefined
        }
      />
    </main>
  );
}
