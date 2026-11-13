import { useEffect, useState } from "react";
import { createModelClient } from "./api";
import type { AuditEvent } from "./auditHistory";
const api = createModelClient();
export function ModelAuditHistory({modelId}: {modelId:string}) {
  const [open,setOpen] = useState(false);
  return <section className="lifecycle-card">
    <h2>Model event history</h2>
    <button type="button" aria-expanded={open} onClick={()=>setOpen(v=>!v)}>{open ? "Hide model events" : "View model events"}</button>
    {open && <Timeline key={modelId} modelId={modelId} />}
  </section>;
}
function Timeline({modelId}: {modelId:string}) {
  const [offset,setOffset] = useState(0),[revision,setRevision] = useState(0);
  const [rows,setRows] = useState<AuditEvent[]>([]),[loading,setLoading] = useState(true);
  const [error,setError] = useState(""),[action,setAction] = useState("");
  useEffect(()=>{
    const c=new AbortController();setRows([]);setLoading(true);setError("");setAction("");
    api.listModelHistory(modelId,offset,c.signal).then(value=>{if(!c.signal.aborted)setRows(value);})
      .catch(e=>{if(!c.signal.aborted)setError(e.message);})
      .finally(()=>{if(!c.signal.aborted)setLoading(false);});
    return ()=>c.abort();
  },[modelId,offset,revision]);
  const visible=rows.filter(row=>!action||row.action===action);
  return <section aria-label="Model audit events">
    <p>Backend public events for this model, its runs and reviews, newest first. This endpoint does not include every finding update or harness task event. Actor identity is not supplied. This is not a complete audit export or evidence of approval by itself.</p>
    <button type="button" disabled={loading} onClick={() => {setLoading(true);setRevision(v=>v+1);}}>Refresh model events</button>
    <label>Filter this event page<select aria-label="Filter this event page" value={action} disabled={loading||!!error}
      onChange={e=>setAction(e.target.value)}><option value="">All event types</option>
      {[...new Set(rows.map(row=>row.action))].map(value=><option key={value}>{value}</option>)}
    </select></label>
    {loading && <p role="status">Loading model events…</p>}{error && <p role="alert">{error}</p>}
    {!loading&&!error&&<>
      <p>Page {offset/20+1} · {visible.length} of {rows.length} fetched events. Filters apply only to this page; no global total is supplied.</p>
      {!rows.length ? <p>No events returned on this page. Missing events are not proof that an action never occurred.</p> : !visible.length ? <p>No events match this page filter.</p> :
        <ol>{visible.map(event=><li key={event.id} className="lifecycle-card audit-event">
          <h3>{event.action}</h3><p>Timestamp supplied by backend: <time dateTime={event.created_at}>{event.created_at}</time></p>
          <p>Event: {event.id} · Resource: {event.resource_id}</p>
          <details><summary>Public event details</summary>
            {Object.keys(event.payload).length ? <dl>{Object.entries(event.payload).map(([key,value])=><div key={key}>
              <dt>{key}</dt><dd>{Array.isArray(value) ? value.join("; ") || "No conditions" : value ?? "Not supplied"}</dd>
            </div>)}</dl> : <p>No public details supplied.</p>}
          </details>
        </li>)}</ol>}
    </>}
    <button type="button" disabled={loading||offset===0} onClick={() => {setLoading(true);setOffset(v=>Math.max(0,v-20));}}>Previous events</button>
    <button type="button" disabled={loading||!!error||rows.length<20} onClick={() => {setLoading(true);setOffset(v=>v+20);}}>Next events</button>
    <p>Refresh and pagination only read saved state. Use the corresponding review/run controls for actions.</p>
  </section>;
}
