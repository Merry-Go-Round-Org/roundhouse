import { useEffect, useRef, useState } from "react";
import { createModelClient } from "./api";
import { useAuth } from "./AuthContext";
import { canReview } from "./auth";
import { findingPatch, type Finding, type FindingOwner, type FindingStatus } from "./findings";
const api = createModelClient();
export function ModelFindings({modelId}: {modelId: string}) {
  const [open,setOpen] = useState(false);
  return <section><button type="button" aria-expanded={open} onClick={()=>setOpen(v=>!v)}>
    {open ? "Hide persisted findings" : "View persisted findings"}
  </button>{open && <FindingsContent modelId={modelId} />}</section>;
}
function FindingsContent({modelId}: {modelId: string}) {
  const {user} = useAuth();
  const editable = canReview(user.role);
  const [offset,setOffset] = useState(0), [revision,setRevision] = useState(0);
  const [rows,setRows] = useState<Finding[]>([]), [owners,setOwners] = useState<FindingOwner[]>([]);
  const [error,setError] = useState(""), [ownerError,setOwnerError] = useState("");
  const [busy,setBusy] = useState(true);
  useEffect(() => {
    const c = new AbortController(); setBusy(true); setRows([]); setOwners([]); setError("");setOwnerError("");
    api.listFindings(modelId,offset,c.signal).then(v => {if (!c.signal.aborted) setRows(v);})
      .catch(e => {if (!c.signal.aborted) setError(e.message);})
      .finally(() => {if (!c.signal.aborted) setBusy(false);});
    if (editable) api.findingOwners(c.signal).then(v => {if (!c.signal.aborted) setOwners(v);})
      .catch(e => {if (!c.signal.aborted) setOwnerError(e.message);});
    return () => c.abort();
  },[modelId,offset,revision,editable,user.id]);
  // Enter loading in the same update as refresh/page changes: never remount editable stale rows.
  return <section aria-label="Persisted findings" className="lifecycle-card">
    <h3>Persisted findings</h3>
    <p>Backend records for this model, including report/check references. Resolving a finding does not approve a model or rerun validation.</p>
    {!editable && <p>Read-only: validators and approvers can update findings.</p>}
    <button type="button" disabled={busy} onClick={() => {setBusy(true);setRevision(v=>v+1);}}>Refresh findings</button>
    {busy && <p role="status">Loading findings…</p>}{error && <p role="alert">{error}</p>}
    {ownerError && <p role="alert">Owner choices unavailable: {ownerError}. Refresh findings to retry.</p>}
    {!busy && !error && <>
      {!rows.length && <p>No findings returned on this page. This is not an approval or proof of no issues.</p>}
      {rows.map(f => <FindingCard key={`${f.id}:${revision}:${user.id}`} finding={f} owners={owners} editable={editable} offset={offset} />)}
    </>}
    <p>Page {offset/20+1}. Counts refer to this fetched page.</p>
    <button type="button" disabled={busy || offset===0} onClick={()=>{setBusy(true);setOffset(v=>Math.max(0,v-20));}}>Previous findings</button>
    <button type="button" disabled={busy || !!error || rows.length<20} onClick={()=>{setBusy(true);setOffset(v=>v+20);}}>Next findings</button>
  </section>;
}
function FindingCard({finding,owners,editable,offset}: {finding:Finding;owners:FindingOwner[];editable:boolean;offset:number}) {
  const [status,setStatus] = useState(finding.status);
  const [owner,setOwner] = useState(finding.owner_id ?? "");
  const [due,setDue] = useState(finding.due_date ? new Date(finding.due_date).toISOString().slice(0,16) : "");
  const [comment,setComment] = useState(""), [confirmed,setConfirmed] = useState(false);
  const [busy,setBusy] = useState(false), [locked,setLocked] = useState(false);
  const [error,setError] = useState(""), [saved,setSaved] = useState<Finding|null>(null);
  const request = useRef<AbortController|null>(null);
  useEffect(()=>()=>request.current?.abort(),[]);
  async function save() {
    if (request.current || locked || !confirmed || !editable) return;
    let patch;
    try { patch = findingPatch(finding,status,owner,due,comment); }
    catch (e) {setError((e as Error).message);return;}
    if (patch.owner_id && !owners.some(o=>o.id===patch.owner_id)) {setError("Select an active owner from the backend list.");return;}
    const c = new AbortController();request.current=c;setBusy(true);setLocked(true);setConfirmed(false);setError("");
    try { const result = await api.updateFinding(finding,patch,offset,c.signal);if(!c.signal.aborted)setSaved(result); }
    catch(e){if(!c.signal.aborted)setError((e as Error).message+" Refresh findings to reconcile saved state before editing again. No automatic retry.");}
    finally{request.current=null;if(!c.signal.aborted)setBusy(false);}
  }
  const record = saved ?? finding;
  return <article className="lifecycle-card" aria-label={`Finding ${finding.id}`}>
    <h4>{record.summary}</h4><p>Finding {record.id} · Severity: {record.severity} · Status: {record.status}</p>
    <p>Report: {record.report_id} · Check: {record.check_id}</p>
    <p>Owner: {record.owner_id ?? "Unassigned"} · Due (UTC): {record.due_date ?? "Not set"}</p>
    <details><summary>Recorded finding comments</summary>{record.comments.length ? <ul>{record.comments.map((c,i)=><li key={i}>{c.at} · {c.actor_id}: {c.comment}</li>)}</ul>:<p>No comments recorded.</p>}</details>
    {editable && !saved && <form onSubmit={e=>{e.preventDefault();void save();}} onChange={()=>setConfirmed(false)}>
      <fieldset disabled={busy||locked}>
        <legend>Update this finding</legend>
        <label>Status<select aria-label="Status" value={status} onChange={e=>setStatus(e.target.value as FindingStatus)}>
          <option value="open">Open</option><option value="in_progress">In progress</option><option value="resolved">Resolved</option>
        </select></label>
        <label>Owner<select aria-label="Owner" value={owner} onChange={e=>setOwner(e.target.value)}>
          <option value="">Unassigned</option>
          {finding.owner_id && !owners.some(o=>o.id===finding.owner_id) && <option value={finding.owner_id}>Current owner: {finding.owner_id} (not in active choices)</option>}
          {owners.map(o=><option value={o.id} key={o.id}>{o.username} · {o.role}</option>)}
        </select></label>
        <label>Due date (UTC)<input type="datetime-local" value={due} onChange={e=>setDue(e.target.value)} /></label>
        <label>Update comment<textarea required maxLength={4000} value={comment} onChange={e=>setComment(e.target.value)} /></label>
      </fieldset>
      <p>Changes: status {status}; owner {owner||"unassigned"}; due {due||"not set"} UTC. An empty owner/due field clears that value only if changed.</p>
      <label><input type="checkbox" checked={confirmed} disabled={busy||locked}
        onChange={e=>{e.stopPropagation();setConfirmed(e.target.checked);}} />I confirm this finding update and comment.</label>
      <button type="submit" disabled={busy||locked||!confirmed||!comment.trim()}>Save finding update</button>
    </form>}
    {saved && <p role="status">Finding update saved. Refresh findings for the latest shared state.</p>}
    {error && <p role="alert">{error}</p>}
  </article>;
}
