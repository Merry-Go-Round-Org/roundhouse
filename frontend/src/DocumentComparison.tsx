import { useEffect, useRef, useState } from "react";
import { createModelClient, type ReportRead } from "./api";
import type { ModelFile } from "./modelFiles";
const api = createModelClient();
type Choice = {key: string; label: string; file?: ModelFile; report?: ReportRead};
type Preview = {label: string; provenance: string; text: string};
export function DocumentComparison({modelId}: {modelId: string}) {
  const [choices, setChoices] = useState<Choice[] | null>(null);
  const [left, setLeft] = useState(""); const [right, setRight] = useState("");
  const [previews, setPreviews] = useState<Preview[] | null>(null);
  const [error, setError] = useState(""); const [busy, setBusy] = useState(false);
  const request = useRef<AbortController | null>(null);
  useEffect(() => () => request.current?.abort(), []);
  async function act(compare: boolean) {
    if (request.current) return;
    const c = new AbortController(); request.current = c; setBusy(true); setError(""); setPreviews(null);
    try {
      if (!compare) {
        setChoices(null); setLeft(""); setRight("");
        const [files, reports] = await Promise.all([api.listModelFiles(modelId,c.signal),api.listReports(modelId,c.signal)]);
        if (!c.signal.aborted) setChoices([
          ...files.map(file => ({key:"file:"+file.path,label:"Uploaded source: "+file.path,file})),
          ...reports.filter(report => report.kind === "documentation").map((report,index) => ({key:"report:"+report.id,
            label:`Agent-generated document ${index+1} · model version ${report.model_version ?? "unspecified"}`, report})),
        ]);
      } else {
        const picks = [left,right].filter(Boolean).map(key => choices?.find(choice => choice.key === key));
        if (!picks.length || picks.some(p => !p)) throw new Error("Select an available source or report.");
        const reports = await api.listReports(modelId,c.signal);
        const output: Preview[] = [];
        for (const pick of picks) {
          if (pick!.file) {
            output.push({label:pick!.label,provenance:`Backend SHA-256: ${pick!.file.sha256}. Registered package snapshot; report-version association is not supplied.`,text:await api.getModelFile(modelId,pick!.file,c.signal)});
          } else {
            const original = pick!.report!;
            const current = reports.find(row => row.id === original.id);
            if (!current || current.kind !== "documentation" || current.artifact_id !== original.artifact_id || current.artifact_version !== original.artifact_version || current.artifact_sha256 !== original.artifact_sha256 || current.model_version !== original.model_version)
              throw new Error("Report identity/version changed. Reload comparison sources.");
            output.push({label:pick!.label,provenance:`Artifact ${current.artifact_id} · SHA-256 ${current.artifact_sha256 ?? "Not supplied"} · Review ${current.review_status ?? "Not supplied"}.`,text:await api.getDocumentText(current.artifact_id,c.signal)});
          }
        }
        if (!c.signal.aborted) setPreviews(output);
      }
    } catch (e) { if (!c.signal.aborted) setError((e as Error).message); }
    finally { request.current = null; if (!c.signal.aborted) setBusy(false); }
  }
  return <section aria-label="Source documents and comparison" className="lifecycle-card">
    <h3>Uploaded sources and document comparison</h3>
    <p>Browse original notes and source files uploaded with the model, or compare them with generated documents. Uploaded files are not agent-generated reports; their link to older report versions may be unknown.</p>
    <button type="button" disabled={busy} onClick={() => void act(false)}>Browse uploaded sources and reports</button>
    {error && <p role="alert">{error}</p>}
    {choices && <>
      {!choices.length && <p>No readable sources or documentation reports returned.</p>}
      <label>First document<select aria-label="First document" disabled={busy} value={left} onChange={e => {setLeft(e.target.value);setPreviews(null);}}>
        <option value="">Select document</option>{choices.map(choice => <option key={choice.key} value={choice.key}>{choice.label}</option>)}
      </select></label>
      <label>Second document (optional)<select aria-label="Second document (optional)" disabled={busy} value={right} onChange={e => {setRight(e.target.value);setPreviews(null);}}>
        <option value="">Single document</option>{choices.map(choice => <option key={choice.key} value={choice.key}>{choice.label}</option>)}
      </select></label>
      <button type="button" disabled={busy || !left || left === right} onClick={() => void act(true)}>Read selected documents</button>
    </>}
    {busy && <p role="status">Reading authorized documents…</p>}
    {previews && <div className="document-comparison">{previews.map((preview,i) => <section key={i} aria-label={`Comparison document ${i+1}`}>
      <h4>{preview.label}</h4><details className="help-details"><summary>Source identifiers</summary><p>{preview.provenance}</p></details><pre>{preview.text}</pre>
    </section>)}</div>}
  </section>;
}
