import { isRevisionRequested, revisionRequestedMessage } from "./runReviewStatus";
import { AnalysisSourceDetails } from "./AnalysisSourceDetails";
import { OperationStatusCard } from "./OperationStatusCard";
import { useEffect, useRef, useState } from "react";
import { ApiError, createModelClient, type RunRead } from "./api";
import { useAuth } from "./AuthContext";
import { ProposalCard } from "./ProposalCard";
import {
  proposalSnapshot,
  type Proposal,
  type ActionCatalog,
  type Analysis,
} from "./nativeApi";
import type { FindingOwner } from "./findings";
import { ChatMarkdown } from "./ChatMarkdown";
import "./proposalPreview.css";
import "./nativeWorkflows.css";
const api = createModelClient();
const titles: Record<string, string> = {
  open_finding: "Open a validation finding",
  mark_drift_benign: "Mark drift as benign",
  start_revalidation: "Start revalidation",
  start_recalibration: "Start recalibration",
  restrict_use: "Restrict model use",
  report_data_incident: "Report a data incident",
  start_operation: "Start a model task",
};
export const pretty = (v: unknown): string =>
  v === null || v === undefined
    ? "Not supplied"
    : typeof v === "string"
      ? v
      : Array.isArray(v)
        ? v.map(pretty).join(", ")
        : typeof v === "object"
          ? JSON.stringify(v, null, 2)
          : String(v);
const label = (s: string) => s.replaceAll("_", " ");

export function LiveProposals({
  modelId,
  sourceRunId,
  onRequest,
}: {
  modelId?: string;
  sourceRunId?: string;
  onRequest?: (text: string) => void;
}) {
  const listElement = useRef<HTMLElement>(null);
  const [backgroundError, setBackgroundError] = useState("");
  const [offset, setOffset] = useState(0),
    [status, setStatus] = useState("");
  const [rows, setRows] = useState<Proposal[]>([]),
    [error, setError] = useState(""),
    [loading, setLoading] = useState(true),
    [revision, setRevision] = useState(0),
    [selected, setSelected] = useState("");
  useEffect(() => {
    const c = new AbortController();
    setLoading(true);
    setError("");
    setBackgroundError("");
    setRows([]);
    api.native
      .proposals(
        { model_id: modelId, source_run_id: sourceRunId, status },
        offset,
        c.signal,
      )
      .then((v) => {
        if (!c.signal.aborted) setRows(v);
      })
      .catch((e) => {
        if (!c.signal.aborted) setError(e.message);
      })
      .finally(() => {
        if (!c.signal.aborted) setLoading(false);
      });
    return () => c.abort();
  }, [modelId, sourceRunId, status, offset, revision]);
  useEffect(() => {
    if (loading || error || backgroundError) return;
    const c = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    const refresh = async () => {
      if (document.visibilityState !== "visible" || listElement.current?.closest("[hidden]")) {
        timer = setTimeout(refresh, 10000);
        return;
      }
      try {
        const fresh = await api.native.proposals({ model_id: modelId, source_run_id: sourceRunId, status }, offset, c.signal);
        if (c.signal.aborted) return;
        // Only refresh list summaries. Never remount a selected decision form.
        setRows(fresh);
        timer = setTimeout(refresh, 10000);
      } catch (problem) {
        if (!c.signal.aborted) setBackgroundError((problem as Error).message);
      }
    };
    timer = setTimeout(refresh, 10000);
    return () => { c.abort(); clearTimeout(timer); };
  }, [modelId, sourceRunId, status, offset, revision, loading, error, backgroundError]);
  return (
    <section ref={listElement} className="native-workflows" aria-label="Agent proposals">
      <h2>Agent proposals</h2>
      <p>
        Review suggested actions. Approval and execution are separate steps.
      </p>
      <div className="native-toolbar">
        <label>
          Proposal status
          <select
            value={status}
            onChange={(e) => {
              setLoading(true);
              setStatus(e.target.value);
              setOffset(0);
              setSelected("");
            }}
          >
            <option value="">All statuses</option>
            {[
              "pending",
              "approved",
              "rejected",
              "superseded",
              "expired",
              "executed",
            ].map((v) => (
              <option key={v}>{v}</option>
            ))}
          </select>
        </label>
        <button
          onClick={() => {
            setLoading(true);
            setSelected("");
            setRevision((v) => v + 1);
          }}
        >
          Refresh proposals
        </button>
      </div>
      {backgroundError && <p role="status">Automatic updates paused: {backgroundError} Use Refresh proposals to try again.</p>}
      {loading ? (
        <p role="status">Loading proposals…</p>
      ) : error ? (
        <p role="alert">{error}</p>
      ) : (
        <>
          <ul className="native-record-list">
            {rows.map((p) => (
              <li key={p.id}>
                <button onClick={() => setSelected(p.id)}>
                  {titles[p.type] ?? label(p.type)} · {p.status} ·{" "}
                  {new Date(p.created_at).toLocaleString()}
                </button>
              </li>
            ))}
          </ul>
          {!rows.length && <p>No proposals on this page.</p>}
        </>
      )}
      <div className="native-toolbar">
        <button
          disabled={loading || offset === 0}
          onClick={() => {
            setLoading(true);
            setSelected("");
            setOffset((v) => Math.max(0, v - 20));
          }}
        >
          Previous proposals
        </button>
        <span>Page {offset / 20 + 1}</span>
        <button
          disabled={loading || !!error || rows.length < 20}
          onClick={() => {
            setLoading(true);
            setSelected("");
            setOffset((v) => v + 20);
          }}
        >
          Next proposals
        </button>
      </div>
      {selected && (
        <LiveProposal key={selected} id={selected} modelId={modelId} sourceRunId={sourceRunId} onRequest={onRequest} />
      )}
    </section>
  );
}
export function ProposalSection({ modelId }: { modelId: string }) {
  const [open, setOpen] = useState(false);
  return (
    <details
      className="native-workflows"
      onToggle={(e) => setOpen(e.currentTarget.open)}
    >
      <summary>Review agent proposals for this model</summary>
      {open && <LiveProposals key={modelId} modelId={modelId} />}
    </details>
  );
}
export function LiveProposal({
  id,
  modelId,
  conversationId,
  sourceRunId,
  onRequest,
}: {
  id: string;
  modelId?: string;
  conversationId?: string;
  sourceRunId?: string;
  onRequest?: (text: string) => void;
}) {
  const [p, setP] = useState<Proposal | null>(null),
    [catalog, setCatalog] = useState<ActionCatalog>({}),
    [error, setError] = useState(""),
    [busy, setBusy] = useState(true),
    [uncertain, setUncertain] = useState(false),
    [revision, setRevision] = useState(0);
  const detailElement = useRef<HTMLElement>(null);
  const [changed, setChanged] = useState(false);
  const [updateError, setUpdateError] = useState("");
  const life = useRef<AbortController | null>(null),
    lock = useRef(false);
  function bind(v: Proposal) {
    if (
      (modelId && v.model_id !== modelId) ||
      (conversationId && v.conversation_id !== conversationId) ||
      (sourceRunId && v.source_run_id !== sourceRunId)
    )
      throw new Error("Proposal belongs to another context.");
    return v;
  }
  useEffect(() => {
    const c = new AbortController();
    life.current = c;
    setChanged(false);
    setUpdateError("");
    setP(null);
    setBusy(true);
    setError("");
    Promise.all([
      api.native.proposal(id, c.signal),
      api.native.actions(c.signal),
    ])
      .then(([v, a]) => {
        if (!c.signal.aborted) {
          setP(bind(v));
          setCatalog(a);
          setUncertain(false);
        }
      })
      .catch((e) => {
        if (!c.signal.aborted) setError(e.message);
      })
      .finally(() => {
        if (!c.signal.aborted) setBusy(false);
      });
    return () => c.abort();
  }, [id, modelId, conversationId, sourceRunId, revision]);
  useEffect(() => {
    if (!p || busy || uncertain || changed || updateError) return;
    const c = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    const inspect = async () => {
      if (document.visibilityState !== "visible" || detailElement.current?.closest("[hidden]")) {
        timer = setTimeout(inspect, 10000);
        return;
      }
      try {
        const saved = bind(await api.native.proposal(id, c.signal));
        if (c.signal.aborted) return;
        if (JSON.stringify(saved) !== JSON.stringify(p)) {
          // Preserve notes on the old evidence, but block decisions until explicit reinspection.
          setChanged(true);
          return;
        }
        timer = setTimeout(inspect, 10000);
      } catch (problem) {
        if (!c.signal.aborted) setUpdateError((problem as Error).message);
      }
    };
    timer = setTimeout(inspect, 10000);
    return () => { c.abort(); clearTimeout(timer); };
  }, [p, busy, uncertain, changed, updateError, id, modelId, conversationId, sourceRunId]);
  async function write(work: (signal: AbortSignal) => Promise<Proposal>) {
    if (lock.current || busy || uncertain || changed || updateError) return;
    lock.current = true;
    setBusy(true);
    setError("");
    try {
      const v = await work(life.current!.signal);
      if (!life.current!.signal.aborted) setP(bind(v));
    } catch (e) {
      if (!life.current!.signal.aborted) {
        setError(
          (e as Error).message +
            " Reload saved proposal before another action; no write was retried.",
        );
        setUncertain(true);
      }
    } finally {
      lock.current = false;
      if (!life.current!.signal.aborted) setBusy(false);
    }
  }
  return (
    <section ref={detailElement} className="native-workflows" aria-label="Saved proposal">
      <button
        disabled={busy}
        onClick={() => {
          setP(null);
          setBusy(true);
          setRevision((v) => v + 1);
        }}
      >
        Reload saved proposal
      </button>
      {changed && <p role="status">This proposal changed. Your notes are preserved below. Copy any notes you need, then reload the saved proposal and inspect it again before deciding.</p>}
      {updateError && <p role="status">Could not check for proposal changes: {updateError} Reload the saved proposal before deciding.</p>}
      {busy && <p role="status">Loading saved state…</p>}
      {error && <p role="alert">{error}</p>}
      {p && (
        <ProposalCard
          proposal={{
            id: p.id,
            revision: proposalSnapshot(p),
            title: titles[p.type] ?? label(p.type),
            origin: p.conversation_id ? "Chat proposal" : "Agent proposal",
            summary: p.rationale,
            model: p.model_id,
            modelVersion: pretty(p.preconditions.model_version),
            sourceRun:
              p.source_run_id ??
              "Chat task: " + (p.source_task_id ?? "not supplied"),
            requiredRole: p.required_role,
            status: p.status,
            fields: Object.entries(p.args).map(([k, v]) => ({
              label: label(k),
              value: pretty(v),
            })),
            evidence: p.evidence.map((e) => ({
              kind: label(e.kind),
              reference: e.ref,
            })),
            technical: {
              preconditions: p.preconditions,
              args: p.args,
              decision: p.decision,
              result: p.result,
            },
          }}
          actions={
            <>
              <ProposalDecision
                key={proposalSnapshot(p) + ":" + uncertain}
                proposal={p}
                disabled={busy || uncertain || changed || !!updateError}
                available={!!catalog[p.type]?.execution_available}
                write={write}
              />
              {p.result && (
                <section aria-label="Proposal outcome">
                  <h3>Recorded outcome</h3>
                  <dl>
                    {Object.entries(p.result).map(([k, v]) => (
                      <div key={k}>
                        <dt>{label(k)}</dt>
                        <dd>{pretty(v)}</dd>
                      </div>
                    ))}
                  </dl>
                </section>
              )}
              {p.status === "executed" && typeof p.result?.run_id === "string" && p.result.run_id.trim() && <OperationStatusCard key={p.result.run_id} runId={p.result.run_id} modelId={p.model_id} onRequest={onRequest} />}
              {p.source_run_id && (
                <AnalysisPanel
                  runId={p.source_run_id}
                  modelId={p.model_id}
                  includeProposals={false}
                />
              )}
              <a href={"/?model=" + encodeURIComponent(p.model_id)}>
                Open model evidence
              </a>
            </>
          }
        />
      )}
    </section>
  );
}
function ProposalDecision({
  proposal: p,
  disabled,
  available,
  write,
}: {
  proposal: Proposal;
  disabled: boolean;
  available: boolean;
  write: (work: (signal: AbortSignal) => Promise<Proposal>) => Promise<void>;
}) {
  const { user } = useAuth();
  const [choice, setChoice] = useState<"approved" | "rejected" | "">(""),
    [reason, setReason] = useState(""),
    [confirmed, setConfirmed] = useState(false),
    [owner, setOwner] = useState(""),
    [due, setDue] = useState("");
  const [owners, setOwners] = useState<FindingOwner[]>([]),
    [ownerError, setOwnerError] = useState("");
  const allowed = user.role === p.required_role || user.role === "admin";
  useEffect(() => {
    const c = new AbortController();
    if (p.type === "open_finding" && p.status === "pending" && allowed)
      api
        .findingOwners(c.signal)
        .then((v) => {
          if (!c.signal.aborted) setOwners(v);
        })
        .catch((e) => {
          if (!c.signal.aborted) setOwnerError(e.message);
        });
    return () => c.abort();
  }, [p.id, p.type, p.status, allowed]);
  if (!allowed)
    return <p>Read-only: this action requires the {p.required_role} role or an administrator.</p>;
  const finding = choice === "approved" && p.type === "open_finding";
  const dueValid =
    /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$/.test(due) &&
    Number.isFinite(Date.parse(due + ":00Z"));
  if (p.status === "approved")
    return (
      <fieldset disabled={disabled || !available}>
        <legend>Execute approved action</legend>
        <p>
          This starts the approved operation. It does not grant model approval.
        </p>
        <label>
          <input
            type="checkbox"
            checked={confirmed}
            onChange={(e) => setConfirmed(e.target.checked)}
          />
          I confirm execution of this exact approved action
        </label>
        <button
          disabled={!confirmed}
          onClick={() => {
            setConfirmed(false);
            void write((signal) => api.native.execute(p, signal));
          }}
        >
          Execute approved action
        </button>
        {!available && (
          <p>Execution unavailable in the current action catalogue.</p>
        )}
      </fieldset>
    );
  if (p.status !== "pending")
    return (
      <p>This proposal is {p.status}. No decision or execution is available.</p>
    );
  return (
    <fieldset disabled={disabled}>
      <legend>Review proposal</legend>
      <p>
        To change a proposed action, reject it with your corrections and ask the
        agent for a revised proposal.
      </p>
      <label>
        Proposal decision
        <select
          value={choice}
          onChange={(e) => {
            setChoice(e.target.value as typeof choice);
            setConfirmed(false);
          }}
        >
          <option value="">Choose a decision</option>
          <option value="approved">Approve</option>
          <option value="rejected">Reject</option>
        </select>
      </label>
      <label>
        Decision rationale
        <textarea
          value={reason}
          maxLength={2000}
          onChange={(e) => {
            setReason(e.target.value);
            setConfirmed(false);
          }}
        />
      </label>
      {finding && (
        <>
          <label>
            Finding owner
            <select
              value={owner}
              onChange={(e) => {
                setOwner(e.target.value);
                setConfirmed(false);
              }}
            >
              <option value="">Select active owner</option>
              {owners.map((o) => (
                <option value={o.id} key={o.id}>
                  {o.username}
                </option>
              ))}
            </select>
          </label>
          <label>
            Due date (UTC)
            <input
              type="datetime-local"
              value={due}
              onChange={(e) => {
                setDue(e.target.value);
                setConfirmed(false);
              }}
            />
          </label>
          {ownerError && <p role="alert">{ownerError}</p>}
          <p>Execution requires an accepted validation report.</p>
        </>
      )}
      <label>
        <input
          type="checkbox"
          checked={confirmed}
          onChange={(e) => setConfirmed(e.target.checked)}
        />
        I inspected the evidence and confirm this exact proposal and decision
      </label>
      <button
        disabled={
          !confirmed ||
          !choice ||
          !reason.trim() ||
          (finding && (!owners.some((o) => o.id === owner) || !dueValid))
        }
        onClick={() => {
          setConfirmed(false);
          void write((signal) =>
            api.native.decide(
              p,
              {
                decision: choice as "approved" | "rejected",
                rationale: reason.trim(),
                ...(finding
                  ? {
                      owner_id: owner,
                      due_date: new Date(due + ":00Z").toISOString(),
                    }
                  : {}),
              },
              signal,
            ),
          );
        }}
      >
        Submit proposal decision
      </button>
    </fieldset>
  );
}
type AnalysisPanelProps = { runId: string; modelId: string; includeProposals?: boolean };
export function AnalysisPanel(props: AnalysisPanelProps) {
  return <SavedAnalysisPanel key={`${props.modelId}:${props.runId}`} {...props} />;
}
function SavedAnalysisPanel({ runId, modelId, includeProposals = true }: AnalysisPanelProps) {
  const [data, setData] = useState<Analysis | null>(null);
  const [run, setRun] = useState<RunRead | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(true);
  const [paused, setPaused] = useState(false);
  const [revision, setRevision] = useState(0);
  const element = useRef<HTMLElement>(null);
  useEffect(() => {
    const c = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    let reads = 0;
    setError(""); setPaused(false); setBusy(true);
    async function load() {
      if (c.signal.aborted) return;
      if (document.visibilityState !== "visible" || element.current?.closest("[hidden]")) {
        timer = setTimeout(() => void load(), 3000); return;
      }
      try {
        const saved = await api.getRun(runId, c.signal);
        if (saved.id !== runId || saved.model_id !== modelId)
          throw new Error("Analysis run belongs to another model or run.");
        let analysis: Analysis | null = null;
        try { analysis = await api.native.analysis(runId, c.signal); }
        catch (e) { if (!(e instanceof ApiError && e.status === 404)) throw e; }
        if (c.signal.aborted) return;
        setRun(saved); setData(analysis); setBusy(false);
        reads += 1;
        if (["queued", "running"].includes(saved.status)) {
          // A bounded observation window; refreshing only restarts reads, never the job.
          if (reads >= 100) setPaused(true);
          else timer = setTimeout(() => void load(), 3000);
        }
      } catch (e) {
        if (!c.signal.aborted) {
          setRun(null); setData(null); setBusy(false); setError((e as Error).message);
        }
      }
    }
    void load();
    return () => { c.abort(); clearTimeout(timer); };
  }, [runId, modelId, revision]);
  const revisionRequested = isRevisionRequested(run);
  const active = run && ["queued", "running"].includes(run.status);
  const tab = run?.operation === "validate" ? "Validation" : "Monitoring";
  return (
    <section ref={element} className="native-analysis" aria-label="Agent analysis">
      <h3>Agent analysis</h3>
      <button disabled={busy} onClick={() => { setBusy(true); setRevision(v => v + 1); }}>
        {busy ? "Loading analysis…" : "Refresh agent analysis"}
      </button>
      {error && <p role="alert">Could not refresh analysis: {error} Automatic updates stopped. Refresh agent analysis to try again. No new job was submitted.</p>}
      {paused && <p role="status">Automatic updates paused after five minutes of checks. Refresh agent analysis to continue checking the same operation.</p>}
      {run && <>
        <p role="status">Operation: {revisionRequested ? "Changes requested" : label(run.status)}{!revisionRequested && run.stage ? ` · ${label(run.stage)}` : ""}</p>
        {revisionRequested && <p>{revisionRequestedMessage}</p>}
        {run.status === "failed" && !revisionRequested && <div role="alert">
          <p>The operation failed. {run.error || "No failure reason was supplied."}</p>
          <p>Refresh checks the saved result only. Inspect the error before requesting a new attempt from the model page or agent chat; it requires a separate authorized action.</p>
          <a href={`/?model=${encodeURIComponent(modelId)}&tab=${encodeURIComponent(tab)}`}>Open model {tab.toLowerCase()}</a>
        </div>}
        {active && <p>Analysis is being prepared. Saved results update automatically while this panel is visible.</p>}
        {run.status === "awaiting_review" && <p>The operation is waiting for human review. Read the evidence before deciding.</p>}
        {data?.result ? <>
          <h4>{label(data.kind)} report</h4>
          <p>Saved agent interpretation. Measured checks and human approval are separate.</p>
          {Object.entries(data.result.exports).filter(([name]) => name.endsWith(".md")).map(([name, text]) => (
            <section className="durable-chat" key={name}>
              <h4>{name.replaceAll("_", " ")}</h4><ChatMarkdown text={text} />
            </section>
          ))}
          <AnalysisSourceDetails result={data.result} runId={data.run_id} />
          {includeProposals && data.result.proposal_ids.map(id => (
            <LiveProposal key={id} id={id} modelId={modelId} sourceRunId={runId} />
          ))}
        </> : <p>{active ? "No saved analysis report yet." : "No saved analysis report is available. This is not a passed analysis."}</p>}
      </>}
    </section>
  );
}
