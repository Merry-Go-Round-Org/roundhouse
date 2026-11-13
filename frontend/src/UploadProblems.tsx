import type { UploadState } from "./uploads";

/** Display supplied check evidence; never infer missing paths from a partial file listing. */
export function UploadProblems({ upload }: { upload: UploadState }) {
  const failed = upload.checks.filter(check => check.status === "failed");
  const reasons = [...new Set([...failed.map(check => check.reason), ...(upload.error ? [upload.error] : [])])];
  if (!reasons.length && upload.status !== "failed") return null;
  return <section className="notice" aria-label="Upload problems">
    <h4>{upload.status === "failed" ? "Upload needs correction" : "Checks needing attention"}</h4>
    {reasons.length ? <ul>{reasons.map(reason => <li key={reason}>{reason}</li>)}</ul> : <p>No detailed failure reason was supplied.</p>}
    {failed.some(check => check.blocking) && <p>Blocking checks must be resolved before this bundle can be accepted.</p>}
    <details><summary>Prepared bundle checklist</summary><p>Check manifest.json, schema.json and manifest-declared files. These are general requirements, not a list of detected missing files.</p></details>
  </section>;
}
