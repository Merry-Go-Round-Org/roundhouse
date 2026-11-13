import { useState, type ReactNode } from "react";

/** Presentation model only. This is not an agreed HTTP response schema. */
export type ProposalView = {
  id: string;
  revision: string;
  title: string;
  origin: string;
  summary: string;
  model: string;
  modelVersion: string;
  sourceRun: string;
  requiredRole: string;
  status:
    "pending" | "approved" | "rejected" | "superseded" | "expired" | "executed";
  fields: readonly { label: string; value: string }[];
  evidence: readonly { kind: string; reference: string }[];
  technical: Readonly<Record<string, unknown>>;
};

export function ProposalCard({
  proposal,
  previewDecisions = false,
  unavailableReason,
  actions,
}: {
  proposal: ProposalView;
  previewDecisions?: boolean;
  unavailableReason?: string;
  actions?: ReactNode;
}) {
  const statusLabels = {
    pending: "Needs review",
    approved: "Approved · execution not confirmed",
    rejected: "Rejected",
    superseded: "Outdated · review the latest version",
    expired: "Expired",
    executed: "Execution recorded",
  };
  return (
    <article className="proposal-card" aria-label={proposal.title}>
      <header className="proposal-card-heading">
        <div>
          <p className="proposal-origin">{proposal.origin}</p>
          <h2>{proposal.title}</h2>
        </div>
        <span className={`proposal-status proposal-status-${proposal.status}`}>
          {statusLabels[proposal.status]}
        </span>
      </header>
      <p className="proposal-summary">{proposal.summary}</p>
      <dl className="proposal-context">
        <div>
          <dt>Model</dt>
          <dd>{proposal.model}</dd>
        </div>
        <div>
          <dt>Version</dt>
          <dd>{proposal.modelVersion}</dd>
        </div>
        <div>
          <dt>Reviewer role</dt>
          <dd>{proposal.requiredRole}</dd>
        </div>
      </dl>
      <section aria-label="Proposed action">
        <h3>What is proposed</h3>
        <dl className="proposal-fields">
          {proposal.fields.map((field) => (
            <div key={field.label}>
              <dt>{field.label}</dt>
              <dd>{field.value}</dd>
            </div>
          ))}
        </dl>
      </section>
      <section aria-label="Proposal evidence">
        <h3>Supporting evidence</h3>
        {proposal.evidence.length ? (
          <ul>
            {proposal.evidence.map((item, index) => (
              <li key={index}>
                <span>{item.kind}</span> {item.reference}
              </li>
            ))}
          </ul>
        ) : (
          <p>No evidence supplied.</p>
        )}
      </section>
      <details className="proposal-technical">
        <summary>Identifiers and exact proposal details</summary>
        <dl>
          <dt>Proposal</dt>
          <dd>{proposal.id}</dd>
          <dt>Revision</dt>
          <dd>{proposal.revision}</dd>
          <dt>Originating run</dt>
          <dd>{proposal.sourceRun}</dd>
        </dl>
        <pre>{JSON.stringify(proposal.technical, null, 2)}</pre>
      </details>
      {actions !== undefined ? (
        actions
      ) : previewDecisions ? (
        <PreviewDecision
          key={JSON.stringify([
            proposal.id,
            proposal.revision,
            proposal.status,
            unavailableReason,
          ])}
          disabledReason={
            unavailableReason ||
            (proposal.status !== "pending"
              ? "Only a current pending proposal can be reviewed."
              : proposal.evidence.length === 0
                ? "Evidence is needed before reviewing this example."
                : undefined)
          }
        />
      ) : (
        <p className="proposal-unavailable">
          {unavailableReason ||
            "Decisions are unavailable until the proposal API is connected."}
        </p>
      )}
    </article>
  );
}

/** Local interaction preview: deliberately no callback, API import or execution path. */
function PreviewDecision({ disabledReason }: { disabledReason?: string }) {
  const [choice, setChoice] = useState<"approve" | "reject" | "">("");
  const [reason, setReason] = useState("");
  const [confirmed, setConfirmed] = useState(false);
  const [result, setResult] = useState("");
  return (
    <section className="proposal-decision" aria-label="Preview decision">
      <h3>Try the review interaction</h3>
      <p>Local preview only. No decision is saved and no action runs.</p>
      {disabledReason && <p role="status">{disabledReason}</p>}
      <fieldset disabled={!!disabledReason || !!result}>
        <legend>Preview choice</legend>
        <div className="proposal-choice-row">
          {(["approve", "reject"] as const).map((value) => (
            <button
              type="button"
              key={value}
              aria-pressed={choice === value}
              onClick={() => {
                setChoice(value);
                setConfirmed(false);
              }}
            >
              {value === "approve" ? "Approve" : "Reject"}
            </button>
          ))}
        </div>
        <label>
          Reason for this choice
          <textarea
            value={reason}
            maxLength={2000}
            onChange={(event) => {
              setReason(event.target.value);
              setConfirmed(false);
            }}
          />
        </label>
        <label className="proposal-attestation">
          <input
            type="checkbox"
            checked={confirmed}
            disabled={!choice || !reason.trim()}
            onChange={(event) => setConfirmed(event.target.checked)}
          />
          I reviewed the example and confirm this preview choice
        </label>
        <button
          type="button"
          className="button primary"
          disabled={!choice || !reason.trim() || !confirmed}
          onClick={() => {
            setConfirmed(false);
            setResult(
              `Preview complete: ${choice === "approve" ? "approve" : "reject"} selected. Nothing was saved or executed.`,
            );
          }}
        >
          Preview decision
        </button>
      </fieldset>
      {result && <p role="status">{result}</p>}
    </section>
  );
}
