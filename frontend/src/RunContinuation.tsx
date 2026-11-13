import { useEffect, useRef, useState } from "react";
import { useAuth } from "./AuthContext";
import { createModelClient, type RunRead } from "./api";
import { loadContinuation, submitContinuation, type Continuation } from "./continuation";
const api = createModelClient();

export function RunContinuation({ run }: { run: RunRead }) {
  const { user } = useAuth();
  const [shown, setShown] = useState<Continuation | null>(null);
  const [confirmed, setConfirmed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [result, setResult] = useState<RunRead | null>(null);
  const lock = useRef(false);
  const request = useRef<AbortController | null>(null);
  useEffect(() => () => request.current?.abort(), []);
  async function act(submit: boolean) {
    if (lock.current || (submit && (!shown || !confirmed))) return;
    lock.current = true; setBusy(true); setError(""); setConfirmed(false); setResult(null);
    const snapshot = shown;
    setShown(null);
    const c = new AbortController(); request.current = c;
    try {
      if (submit) {
        const resumed = await submitContinuation(api, snapshot!, user, c.signal);
        if (!c.signal.aborted) setResult(resumed);
      } else {
        const state = await loadContinuation(api, run.id, run.model_id, user, c.signal);
        if (!c.signal.aborted) setShown(state);
      }
    } catch (e) {
      if (!c.signal.aborted) setError((e as Error).message + (submit
        ? " No automatic retry. Refresh run details and read saved continuation state before another request; dispatch errors can mean continuation was already saved."
        : " No continuation was submitted."));
    } finally {
      lock.current = false;
      if (!c.signal.aborted) setBusy(false);
    }
  }
  return <section aria-label="Run continuation" className="lifecycle-card">
    <h4>Continue after a recorded review</h4>
    <p>Only the original operator can resume the current agent checkpoint.
      This does not grant approval or submit a new review decision.</p>
    <button type="button" className="button secondary" disabled={busy}
      onClick={() => void act(false)}>Read saved continuation state</button>
    {busy && <p role="status">Checking continuation…</p>}
    {error && <p role="alert">{error}</p>}
    {shown && <>
      <p>Run: {shown.run.id} · Model version: {shown.run.model_version} · Task: {shown.taskId}</p>
      <p>Review: {shown.review.id} · Gate {shown.review.gate} · Artifact: {shown.review.artifact_id} · Version: {shown.review.artifact_version}</p>
      <p>Recorded decision: {shown.review.resolution!.decision} · Reviewer: {shown.review.resolution!.reviewer_id}</p>
      <p>Rationale: {shown.review.resolution!.rationale}</p>
      <p>Conditions: {shown.review.resolution!.conditions.join("; ") || "None recorded"}</p>
      <p>Changes requested returns the recorded rejection to the agent. Acceptance does not invent validation or parity evidence.</p>
      <label><input type="checkbox" checked={confirmed} disabled={busy}
        onChange={e => setConfirmed(e.target.checked)} />
        I confirm resuming this exact run with the recorded review decision.
      </label>
      <button type="button" className="button primary" disabled={busy || !confirmed}
        onClick={() => void act(true)}>Resume run</button>
    </>}
    {result && <p role="status">Continuation acknowledged for {result.id}: {result.status} · {result.stage ?? "No stage supplied"}.
      Refresh run details and History to read later progress. This is not proof of successful agent execution.</p>}
  </section>;
}
