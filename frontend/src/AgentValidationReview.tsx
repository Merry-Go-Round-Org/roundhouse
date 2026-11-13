import { AnalysisSourceDetails } from "./AnalysisSourceDetails";
import { ChatMarkdown } from "./ChatMarkdown";
import { record, numericEvidence } from "./validationSections";

/** Public, persisted analysis only. Missing evidence never implies a successful check. */
export function AgentValidationReview({ review, challenger }: { review?: unknown; challenger?: unknown }) {
  const analysis = record(review);
  const unavailable = analysis.status === "unavailable";
  const errorCode = typeof analysis.error_code === "string" && /^[A-Za-z0-9._-]{1,80}$/.test(analysis.error_code)
    ? analysis.error_code : null;
  const comparison = record(challenger ?? analysis.challenger);
  const reports = Object.entries(record(analysis.exports)).filter(
    (entry): entry is [string, string] => entry[0].endsWith(".md") && typeof entry[1] === "string",
  );
  const partitions = ["validation", "test"].filter((key) => Object.keys(record(comparison[key])).length > 0);
  return (
    <section aria-label="Agent interpretation and challenger evidence" className="lifecycle-card">
      <h3>Agent interpretation</h3>
      <p>Read the agent’s assessment alongside the measured checks. Your review is still required.</p>
      {unavailable && <div role="status" className="notice">
        <strong>Agent analysis unavailable</strong>
        <p>The independent agent assessment did not complete. Saved validation evidence remains available. Inspect Gate 3 requirements; the review service determines which decisions are allowed.</p>
        {errorCode && <p>Failure code: <code>{errorCode}</code></p>}
        <p>Inspect the saved validation run in History and resolve the reported cause before requesting another validation.</p>
      </div>}
      {reports.length ? reports.map(([name, text]) => (
        <section key={name} className="durable-chat">
          <h4>{name.replace(/\.md$/, "").replaceAll("_", " ")}</h4>
          <ChatMarkdown text={text} />
        </section>
      )) : !unavailable && <p>No saved agent narrative in this report.</p>}
      <h3>Challenger evaluation</h3>
      <p>An independently scored alternative model for comparison.</p>
      {typeof comparison.status === "string" && <p>Status: <strong>{comparison.status}</strong></p>}
      {typeof comparison.reason === "string" && <p>{comparison.reason}</p>}
      {partitions.length ? (
        <div className="inventory-scroll">
          <table aria-label="Challenger evaluation metrics">
            <thead><tr><th>Partition</th><th>ROC AUC</th><th>Rows</th><th>Review status</th></tr></thead>
            <tbody>{partitions.map((key) => {
              const row = record(comparison[key]);
              return <tr key={key}><th scope="row">{key}</th><td>{numericEvidence(row.auc)}</td><td>{numericEvidence(row.n_rows)}</td><td>{typeof row.status === "string" ? row.status : "Not supplied"}</td></tr>;
            })}</tbody>
          </table>
        </div>
      ) : !comparison.status && <p>No challenger evaluation supplied.</p>}
      {(review != null || challenger != null) && <AnalysisSourceDetails
        result={review} title="Agent analysis source references"
      />}
    </section>
  );
}
