import { record } from "./validationSections";

/** Show public provenance references without repeating report bodies or unknown fields. */
export function AnalysisSourceDetails({ result, runId, title = "Analysis evidence and export details" }: {
  result: unknown; runId?: string; title?: string;
}) {
  const data = record(result);
  const strings = (value: unknown) => Array.isArray(value)
    ? [...new Set(value.filter((item): item is string => typeof item === "string" && !!item.trim()))] : [];
  const groups = [
    ["Export files", Object.keys(record(data.exports))],
    ["Artifact references", strings(data.artifact_ids)],
    ["Proposal references", strings(data.proposal_ids)],
  ] as const;
  return <details className="help-details" style={{ overflowWrap: "anywhere" }}>
    <summary>{title}</summary>
    <p>References for tracing this report. Export contents are shown in the report above when supported.</p>
    <dl>
      {runId && <><dt>Operation run</dt><dd>{runId}</dd></>}
      <dt>Harness run</dt><dd>{typeof data.ai_native_run_id === "string" && data.ai_native_run_id.trim() ? data.ai_native_run_id : "Not supplied"}</dd>
    </dl>
    {groups.map(([label, values]) => <section key={label} aria-label={label}>
      <h4>{label}</h4>
      {values.length ? <ul>{values.map(value => <li key={value} style={{ overflowWrap: "anywhere" }}>{value}</li>)}</ul> : <p>None supplied.</p>}
    </section>)}
  </details>;
}
