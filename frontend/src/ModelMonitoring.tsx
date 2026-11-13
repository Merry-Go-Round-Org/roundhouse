import { useSavedUpdates } from "./useSavedUpdates";
import { AnalysisPanel } from "./LiveProposals";
import { useEffect, useRef, useState, type FormEvent } from "react";
import { useAuth } from "./AuthContext";
import { canStartOperation } from "./auth";
import { createModelClient, ApiError, type ModelRecord, type RunRead } from "./api";
import type { MonitoringEvent } from "./monitoring";
import { MonitoringEvidence } from "./MonitoringEvidence";

const api = createModelClient();
const pageSize = 20;
type JobType = "simulate" | "upload";
export function ModelMonitoring({ model, refresh }: { model: ModelRecord; refresh: number }) {
  const { user } = useAuth();
  const [offset, setOffset] = useState(0);
  const [events, setEvents] = useState<MonitoringEvent[]>([]);
  const [loadError, setLoadError] = useState("");
  const [loading, setLoading] = useState(true);
  const [reload, setReload] = useState(0);
  const [windowLabel, setWindowLabel] = useState("");
  const [feature, setFeature] = useState("");
  const [factor, setFactor] = useState("1.5");
  const [batch, setBatch] = useState<File | null>(null);
  const [mode, setMode] = useState<JobType>("simulate");
  const [confirm, setConfirm] = useState(false);
  const [submitError, setSubmitError] = useState("");
  const [feedback, setFeedback] = useState("");
  const [busy, setBusy] = useState(false);
  const [progress, setProgress] = useState<number | null>(null);
  const [run, setRun] = useState<RunRead | null>(null);
  const [runId, setRunId] = useState<string | null>(null);
  const [uncertain, setUncertain] = useState(false);
  const [runLoading, setRunLoading] = useState(false);
  const actionLock = useRef(false);
  const active = useRef<AbortController | null>(null);
  const life = useRef<AbortController | null>(null);

  const manifest = model.inventory?.manifest;
  const binary = manifest?.task === "binary_classification";
  const fields = Array.isArray(manifest?.feature_order)
    ? manifest.feature_order.filter((s): s is string => typeof s === "string" && !!s.trim())
    : [];
  const gateThreeSignedOff = ["accepted", "accepted_with_conditions"].includes(
    String(model.inventory?.approval?.decision),
  );
  const capable = binary && !!model.inventory?.dataset_version_id &&
    !!model.inventory?.bundle_sha256 && gateThreeSignedOff &&
    model.inventory?.applicability?.monitor !== "NOT_APPLICABLE";
  const allowed = canStartOperation(user.role, "monitor");
  const canSubmit = capable && allowed;
  const factorNum = Number(factor);

  useEffect(() => () => {
    active.current?.abort();
    life.current?.abort();
  }, []);
  useEffect(() => {
    const c = new AbortController();
    setEvents([]); setLoading(true); setLoadError("");
    api.listModelMonitoring(model.id, offset, pageSize, c.signal).then(value => {
      if (!c.signal.aborted) setEvents(value);
    }).catch((e: Error) => {
      if (!c.signal.aborted) setLoadError(e.message);
    }).finally(() => {
      if (!c.signal.aborted) setLoading(false);
    });
    return () => c.abort();
  }, [model.id, offset, refresh, reload]);

  const element = useRef<HTMLElement>(null);
  const updates = useSavedUpdates({
    scope: `${model.id}:${offset}:${refresh}:${reload}`, enabled: !loading && !loadError, element,
    read: signal => api.listModelMonitoring(model.id, offset, pageSize, signal), apply: setEvents,
  });
  const runUpdates = useSavedUpdates({
    scope: `${model.id}:${runId}`, enabled: !busy && !runLoading && !!runId && !!run && ["queued", "running"].includes(run.status), element,
    interval: 3000,
    read: async signal => {
      const saved = await api.getRun(runId!, signal);
      if (saved.id !== runId || saved.model_id !== model.id || saved.model_version !== model.version || saved.operation !== "monitor")
        throw new Error("Backend returned a run for another model/version.");
      return saved;
    },
    apply: saved => {
      setRun(saved);
      setFeedback("Persisted monitoring run " + saved.id + " is " + saved.status + ".");
      if (saved.status === "completed") { setUncertain(false); setReload(v => v + 1); }
    },
  });
  async function submit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    if (actionLock.current || uncertain || !canSubmit || !confirm ||
      !windowLabel.trim() || windowLabel.length > 120 ||
      (mode === "simulate" && (!fields.includes(feature) || !Number.isFinite(factorNum) ||
        factorNum <= 0 || factorNum > 100)) ||
      (mode === "upload" && (!batch || !batch.name.toLowerCase().endsWith(".csv") ||
        batch.size === 0 || batch.size > 20_000_000))) return;
    actionLock.current = true;
    setBusy(true); setSubmitError(""); setFeedback(""); setProgress(null);
    setRun(null); setRunId(null);
    const c = new AbortController();
    active.current = c;
    let dispatched = false;
    try {
      dispatched = true;
      const result = mode === "simulate"
        ? await api.simulateMonitoring(model.id, feature, factorNum, windowLabel, c.signal)
        : await api.submitMonitoringBatch(model.id, batch!, windowLabel, setProgress, c.signal);
      if (c.signal.aborted) return;
      setRun(result); setRunId(result.id);
      setFeedback("Backend accepted monitoring run " + result.id +
        ". Status: " + result.status + ". Saved status updates automatically; execution is asynchronous.");
      setConfirm(false);
      setUncertain(false);
    } catch (error) {
      if (c.signal.aborted) return;
      if (error instanceof ApiError && error.runId) setRunId(error.runId);
      const message = (error as Error).message;
      setSubmitError(message + (dispatched
        ? " Check persisted runs and monitoring before any new request. No automatic retry."
        : " No monitoring run was submitted."));
      setUncertain(dispatched && !(error instanceof ApiError &&
        [400, 403, 404, 409, 410, 413, 422].includes(error.status)));
    } finally {
      actionLock.current = false;
      if (!c.signal.aborted) setBusy(false);
    }
  }
  async function inspectRun() {
    if (!runId || runLoading) return;
    const c = new AbortController();
    life.current = c;
    setRunLoading(true); setSubmitError("");
    try {
      const record = await api.getRun(runId, c.signal);
      if (c.signal.aborted) return;
      if (record.model_id !== model.id || record.operation !== "monitor" ||
        record.model_version !== model.version)
        throw new Error("Backend returned a run for another model/version.");
      setRun(record);
      setFeedback("Persisted monitoring run " + record.id + " is " + record.status +
        (record.stage ? " · " + record.stage : "") + ". Refresh PSI history after completion.");
      if (record.status === "completed") {
        setUncertain(false);
        setReload(v => v + 1);
      }
    } catch (e) {
      if (!c.signal.aborted)
        setSubmitError((e as Error).message + " Do not resubmit until Backend status is verified.");
    } finally {
      if (!c.signal.aborted) setRunLoading(false);
    }
  }

  function changeMode(next: JobType) {
    setMode(next); setConfirm(false);
    setSubmitError(""); setProgress(null);
  }
  return (
    <section ref={element} className="p2-monitoring" aria-label="Model PSI monitoring">
      {updates && <p role="status">{updates}</p>}
      {runUpdates && <p role="status">{runUpdates}</p>}
      <header className="p2-section-head">
        <div><p className="eyebrow">LIVE · PERSISTED PSI</p><h2>Model monitoring</h2></div>
      </header>
      <p>Model {model.name} · version {model.version}. Monitoring uses frozen
        development baseline bins and a separate current window; no recalculation
        of the reference using current data.</p>
      {!allowed && <p role="status">Read-only role: only admins, developers and validators can request monitoring.</p>}
      {!capable && <p role="status">
        Monitoring submission is unavailable: the Backend requires an uploaded
        binary model, registered package, verified dataset version and accepted
        Gate 3 validation sign-off.
        Synthetic survival models are NOT_APPLICABLE; no passed PSI is implied.
        An absent or pending Gate 3 decision cannot be assumed approved.
      </p>}
      {canSubmit && (
        <form className="lifecycle-card p2-monitor-form" onSubmit={submit}>
          <h3>Submit a monitoring window</h3>
          <p>Submitting queues a real Backend job. It can consume worker capacity and
            scoring resources. Monitoring may trigger a revalidation run on red PSI;
            simulation results are never production evidence.</p>
          <label>Input mode
            <select aria-label="Monitoring input mode" value={mode}
              onChange={e => changeMode(e.target.value as JobType)} disabled={busy || uncertain}>
              <option value="simulate">Declared drift simulation</option>
              <option value="upload">Uploaded CSV batch</option>
            </select>
          </label>
          <label>Window label
            <input aria-label="Monitoring window label" maxLength={120} required
              value={windowLabel} onChange={e => { setWindowLabel(e.target.value); setConfirm(false); }}
              disabled={busy || uncertain} placeholder="e.g. 2026-11 independent test window"/>
          </label>
          {mode === "simulate" ? (
            <div className="p2-form-columns">
              <label>Declared input feature
                <select aria-label="Simulation feature" value={feature}
                  onChange={e => { setFeature(e.target.value); setConfirm(false); }} required
                  disabled={busy || uncertain}>
                  <option value="">Select a declared model feature</option>
                  {fields.map(name => <option key={name} value={name}>{name}</option>)}
                </select>
              </label>
              <label>Shift factor (greater than 0, up to 100)
                <input aria-label="Simulation factor" type="number" step="any"
                  min="0.000001" max="100" value={factor} required
                  onChange={e => { setFactor(e.target.value); setConfirm(false); }}
                  disabled={busy || uncertain}/>
              </label>
            </div>
          ) : (
            <>
              <label>Batch CSV (max 20 MB / 20,000 rows)
                <input aria-label="Monitoring CSV batch" type="file" accept=".csv,text/csv"
                  disabled={busy || uncertain}
                  onChange={e => { setBatch(e.target.files?.[0] ?? null); setConfirm(false); }}/>
              </label>
              <p>Include <code>row_id</code> and exactly the model's declared input columns;
                do not include target labels or audit-only attributes. Backend verifies
                feature order, rows and cohort separation.</p>
              {progress !== null && <p role="status">Upload transfer: {progress}%</p>}
            </>
          )}
          <label className="p2-confirm">
            <input type="checkbox" checked={confirm} disabled={busy || uncertain}
              onChange={e => setConfirm(e.target.checked)}/>
            I confirm this is a {mode === "simulate" ? "declared simulation, not observed production drift" :
              "real CSV batch for the selected model and dataset"}, and authorize a monitoring job.
          </label>
          <button className="button primary" type="submit"
            disabled={busy || uncertain || !confirm || !windowLabel.trim() ||
              (mode === "simulate" && (!fields.includes(feature) || !Number.isFinite(factorNum) ||
                factorNum <= 0 || factorNum > 100)) ||
              (mode === "upload" && (!batch || batch.size === 0 || batch.size > 20_000_000))}>
            {busy ? "Submitting monitoring…" : "Submit monitoring job"}
          </button>
          {uncertain && <p className="p2-alert" role="status">
            Request outcome is uncertain. Check run history and monitor records;
            no automatic retry will occur. Reload the model page to begin a fresh request.
          </p>}
        </form>
      )}
      {submitError && <p role="alert">{submitError}</p>}
      {feedback && <p role="status">{feedback}</p>}
      {runId && (
        <section className="lifecycle-card" aria-label="Monitoring execution">
          <h3>Monitoring run</h3>
          <p>Run ID: {runId}</p>
          <p>Status: {run?.status ?? "Submission persisted or uncertain — query run"}</p>
          <p>Stage: {run?.stage ?? "Not recorded"} · Error: {run?.error ?? "None supplied"}</p>
          <button className="button secondary" type="button" disabled={runLoading}
            onClick={() => void inspectRun()}>{runLoading ? "Refreshing…" : "Refresh run status (GET)"}</button>
        </section>
      )}
      <section aria-label="Monitoring history">
        <h3>Persisted PSI windows</h3>
        <p>Page {offset / pageSize + 1} · most recent first · each row represents a completed evaluation, not a queued job.</p>
        <button className="button secondary" type="button" onClick={() => {setLoading(true);setReload(v => v + 1);}}
          disabled={loading}>Refresh PSI history</button>
        {loading && <p role="status">Loading persisted PSI history…</p>}
        {loadError && <p role="alert">{loadError}</p>}
        {!loading && !loadError && (events.length === 0
          ? <p>No completed PSI windows recorded for this model.</p>
          : events.map(event => <div key={event.id}><MonitoringEvidence event={event} window={event.payload} />{event.payload.analysis_run_id && <AnalysisPanel key={event.payload.analysis_run_id} runId={event.payload.analysis_run_id} modelId={model.id}/>}</div>))}
        <div className="form-actions">
          <button type="button" disabled={loading || offset === 0}
            onClick={() => {setLoading(true);setOffset(v => Math.max(0, v - pageSize));}}>Previous PSI page</button>
          <button type="button" disabled={loading || !!loadError || events.length < pageSize}
            onClick={() => {setLoading(true);setOffset(v => v + pageSize);}}>Next PSI page</button>
        </div>
      </section>
    </section>
  );
}
