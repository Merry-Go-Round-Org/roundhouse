import { useEffect, useRef, useState } from "react";
import { useAuth } from "./AuthContext";
import { createModelClient, ApiError } from "./api";
import { accountRoles, type Account } from "./accounts";
const api=createModelClient();
export function AccountAdmin(){
  const {user}=useAuth();
  return user.role==="admin"?<Accounts key={user.id} actorId={user.id}/>:<p role="alert">Administrator access required.</p>;
}
function Accounts({actorId}:{actorId:string}){
  const [rows,setRows]=useState<Account[]>([]),[loading,setLoading]=useState(true),[busy,setBusy]=useState(false);
  const [error,setError]=useState(""),[message,setMessage]=useState(""),[revision,setRevision]=useState(0);
  const [username,setUsername]=useState(""),[password,setPassword]=useState(""),[role,setRole]=useState<Account["role"]>("developer");
  const [createConfirm,setCreateConfirm]=useState(false),[target,setTarget]=useState<Account|null>(null),[disableConfirm,setDisableConfirm]=useState(false);
  const [locked,setLocked]=useState(false);
  const [directoryOpen,setDirectoryOpen]=useState(false),[page,setPage]=useState(0),[query,setQuery]=useState("");
  const filtered=rows.filter(row=>`${row.username} ${row.role}`.toLowerCase().includes(query.toLowerCase()));
  const pageCount=Math.max(1,Math.ceil(filtered.length/10));
  const safePage=Math.min(page,pageCount-1);
  const request=useRef<AbortController|null>(null);
  useEffect(()=>()=>request.current?.abort(),[]);
  useEffect(()=>{
    const c=new AbortController();setRows([]);setLoading(true);setError("");setTarget(null);setDisableConfirm(false);setCreateConfirm(false);setPassword("");
    api.listAccounts(c.signal).then(value=>{if(!c.signal.aborted){setRows(value);setLocked(false);}})
      .catch(()=>{if(!c.signal.aborted){setError("Cannot read accounts. Check your session/access and refresh.");setLocked(true);}})
      .finally(()=>{if(!c.signal.aborted)setLoading(false);});
    return()=>c.abort();
  },[revision]);
  async function mutate(kind:"create"|"disable"){
    if(request.current||busy||loading||locked||(kind==="create"?!createConfirm:!disableConfirm||!target))return;
    const secret=password;setPassword("");setCreateConfirm(false);setDisableConfirm(false);setLocked(true);setBusy(true);setError("");setMessage("");
    const c=new AbortController();request.current=c;
    try{
      const saved=kind==="create"?await api.createAccount(username,secret,role,c.signal):await api.disableAccount(target!,actorId,c.signal);
      if(!c.signal.aborted){
        setRows(current=>current.some(row=>row.id===saved.id)?current.map(row=>row.id===saved.id?saved:row):[...current,saved]);
        setMessage(`Account ${saved.username}: ${kind==="create"?"created":"disabled; existing sessions revoked"}. Updating account directory…`);
        setTarget(null);
        if(kind==="create")setUsername("");
        try{
          const latest=await api.listAccounts(c.signal);
          if(c.signal.aborted)return;
          const confirmed=latest.find(row=>row.id===saved.id);
          if(!confirmed||confirmed.username!==saved.username||confirmed.role!==saved.role||confirmed.active!==saved.active)
            throw new Error("Account changed during refresh");
          setRows(latest);setLocked(false);
          setMessage(`Account ${saved.username}: ${kind==="create"?"created":"disabled; existing sessions revoked"}. Account directory updated.`);
        }catch{
          if(!c.signal.aborted){
            setMessage(`Account ${saved.username}: ${kind==="create"?"created":"disabled; existing sessions revoked"}.`);
            setError("The change was saved, but the account directory could not be refreshed or has changed again. Refresh accounts before another change. Do not repeat the saved action.");
          }
        }
      }
    }catch(e){if(!c.signal.aborted){
      // Do not display raw account-creation validation payloads: they may echo credentials.
      const reason=e instanceof ApiError&&e.status===409?"The request conflicts with current account state.":
        e instanceof ApiError&&e.status===403?"Administrator access was denied.":"The account change was not confirmed.";
      setError(reason+" Refresh accounts and reconcile the result before another attempt. No automatic retry. Password entry has been cleared.");
    }}finally{request.current=null;if(!c.signal.aborted)setBusy(false);}
  }
  return <section className="accounts-page" aria-label="Account administration">
    <p className="page-description">Manage accounts and workspace access.</p>
    <button type="button" disabled={busy||loading} onClick={() => {setLoading(true);setRevision(v=>v+1);}}>Refresh accounts</button>
    {loading&&<p role="status">Loading accounts…</p>}{error&&<p role="alert">{error}</p>}{message&&<p role="status">{message}</p>}
    <details className="account-directory" open={directoryOpen} onToggle={e=>setDirectoryOpen(e.currentTarget.open)}>
      <summary>Account directory ({rows.length})</summary>
      {directoryOpen && <><label className="inventory-search">Search accounts<input value={query} onChange={e=>{setQuery(e.target.value);setPage(0);}} placeholder="Name or role"/></label>
      {!loading && <ul className="account-list">{filtered.slice(safePage*10,safePage*10+10).map(row=><li key={row.id}>
        <div><strong>{row.username}{row.id===actorId?" (your account)":""}</strong><p>{row.role} · {row.active?"Active":"Disabled"}</p><details><summary>Account ID</summary><code>{row.id}</code></details></div>
        {row.active?<button type="button" disabled={busy||locked||row.id===actorId} onClick={()=>{setTarget(row);setDisableConfirm(false);}}>Review disabling {row.username}</button>
          :<span className="page-description">Re-enable unavailable: the service does not yet support reactivation.</span>}
      </li>)}</ul>}
      {!loading && !filtered.length && <p>No matching accounts.</p>}
      <nav className="pagination" aria-label="Account pages">
        <button type="button" disabled={loading||safePage===0} onClick={()=>setPage(safePage-1)}>Previous accounts</button>
        <span>Page {safePage+1} of {pageCount}</span>
        <button type="button" disabled={loading||safePage+1>=pageCount} onClick={()=>setPage(safePage+1)}>Next accounts</button>
      </nav></>}
    </details>
    {!loading&&target&&<section aria-label="Disable account confirmation" className="lifecycle-card">
      <h3>Disable {target.username}</h3><p>Account {target.id} · role {target.role}. Existing sessions will be revoked. The service does not yet support reactivation.</p>
      <label><input type="checkbox" checked={disableConfirm} disabled={busy||locked} onChange={e=>setDisableConfirm(e.target.checked)}/>I confirm disabling this exact account.</label>
      <button type="button" disabled={busy||locked||!disableConfirm} onClick={()=>void mutate("disable")}>Disable account</button>
      <button type="button" disabled={busy} onClick={()=>{setTarget(null);setDisableConfirm(false);}}>Cancel disabling</button>
    </section>}
    <form className="lifecycle-card" onSubmit={e=>{e.preventDefault();void mutate("create");}}>
      <h3>Create account</h3>
      <fieldset disabled={busy||loading||locked}>
        <label>New username<input autoComplete="off" maxLength={100} required value={username} onChange={e=>{setUsername(e.target.value);setCreateConfirm(false);}}/></label>
        <label>New account password<input type="password" autoComplete="new-password" minLength={12} maxLength={256} required value={password} onChange={e=>{setPassword(e.target.value);setCreateConfirm(false);}}/></label>
        <label>New account role<select aria-label="New account role" value={role} onChange={e=>{setRole(e.target.value as Account["role"]);setCreateConfirm(false);}}>{accountRoles.map(r=><option key={r}>{r}</option>)}</select></label>
      </fieldset>
      <p>Username: {username.trim()||"Not entered"} · role: {role}. Use a password of 12–256 characters. Password entry clears after submission.</p>
      <label><input type="checkbox" checked={createConfirm} disabled={busy||loading||locked} onChange={e=>setCreateConfirm(e.target.checked)}/>I confirm creating this account with the selected role.</label>
      <button type="submit" disabled={busy||loading||locked||!createConfirm||!username.trim()||password.length<12}>Create account</button>
    </form>
    {locked&&<p>Refresh saved accounts before making another change. An uncertain response must not be treated as a failed creation.</p>}
  </section>;
}
