import { useSavedUpdates } from "./useSavedUpdates";
import { isRevisionRequested, revisionRequestedMessage } from "./runReviewStatus";
import { useAuth } from "./AuthContext";
import { canReview } from "./auth";
import { useEffect, useRef, useState } from "react";
import { createModelClient, type RunRead } from "./api";
import {
  gateLabels,
  prepareReviewDraft,
  waitingTime,
  canDecideReview,
  canChooseReviewDecision,
  sameReviewCheckpoint,
  sameReviewPermissions,
  limitationConditions,
  type ReviewItem,
  type ReviewStatus,
  type ReviewDraft,
} from "./reviews";
import { DocumentReader } from "./DocumentReader";
import { ValidationEvidence } from "./ValidationEvidence";
import { LiveProposals } from "./LiveProposals";
import { ParityPanel } from "./ParityPanel";
import { InputContractChecks } from "./InputContractChecks";
import type { ValidationEvidence as Evidence } from "./validation";
const api = createModelClient();
export function ReviewQueue({
  openModel,
  modelId,
  openIntake,
}: {
  openModel?: (id: string) => void;
  modelId?: string;
  openIntake?: () => void;
}) {
  const { user } = useAuth();
  const reviewerEligible = canReview(user.role);
  const focusAfterSaved = useRef<string | null>(null);
  const [notice, setNotice] = useState("");
  const [status, setStatus] = useState<ReviewStatus>("pending");
  const [rows, setRows] = useState<ReviewItem[]>([]),
    [loading, setLoading] = useState(true);
  const [error, setError] = useState(""),
    [revision, setRevision] = useState(0);
  const [selected, setSelected] = useState<ReviewItem | null>(null);
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 60000);
    return () => clearInterval(timer);
  }, []);
  useEffect(() => {
    const c = new AbortController();
    setLoading(true);
    setError("");
    setRows([]);
    setSelected(null);
    api
      .listReviews(status, c.signal)
      .then((value) => {
        if (!c.signal.aborted) {
          const scoped = modelId ? value.filter(review => review.model_id === modelId) : value;
          setRows(scoped);
          if (focusAfterSaved.current) {
            setSelected(scoped.find((r) => r.id === focusAfterSaved.current) ?? null);
            focusAfterSaved.current = null;
          }
        }
      })
      .catch((e) => {
        if (!c.signal.aborted) setError(e.message);
      })
      .finally(() => {
        if (!c.signal.aborted) setLoading(false);
      });
    return () => c.abort();
  }, [status, revision, modelId]);
  const element = useRef<HTMLElement>(null);
  const updates = useSavedUpdates({
    scope: `${status}:${revision}:${modelId}`, enabled: !loading && !error, element,
    read: signal => api.listReviews(status, signal),
    apply: value => setRows(modelId ? value.filter(r => r.model_id === modelId) : value),
  });
  return (
    <section ref={element} aria-label="Review queue">
      <h2>Human review queue</h2>
      {updates && <p role="status">{updates}</p>}
      <p className="page-description">Inspect evidence and record a decision.</p>
      {openIntake && <p>Looking for an uploaded archive? Gate 0 is reviewed in Archive intake before models enter this queue. <button onClick={openIntake}>Review archive intake plans</button></p>}
      {!reviewerEligible && <p className="notice">Read-only: your role cannot submit human decisions.</p>}
      <details className="help-details"><summary>Review guidance</summary><p>Drafts are not saved. Open the review, inspect its exact version and confirm your decision. If submission is unconfirmed, refresh its saved status before trying again. Waiting time is elapsed queue time, not review effort.</p></details>
      {notice && <p role="status">{notice}</p>}
      <div className="evidence-actions">
        <div className="status-switch" role="group" aria-label="Review status">
          {(["pending", "decided"] as const).map(value => <button key={value} type="button"
            aria-pressed={status === value} disabled={loading}
            onClick={() => { if (status !== value) {setLoading(true);setStatus(value);} }}>
            {value === "pending" ? "Pending" : "Decided"}
          </button>)}
        </div>
        <button
          className="button secondary"
          disabled={loading}
          onClick={() => {setLoading(true);setRevision((v) => v + 1);}}
        >
          Reload reviews
        </button>
      </div>
      {loading ? (
        <p role="status">Loading reviews…</p>
      ) : error ? (
        <p role="alert">{error}</p>
      ) : (
        <>
          {!rows.length && <p>No {status} reviews.</p>}
          {rows.length > 0 && (
            <div className="inventory-scroll">
              <table aria-label="Review items">
                <thead>
                  <tr>
                    <th>Review</th>
                    <th>Model / version</th>
                    <th>Gate</th>
                    <th>Created</th>
                    <th>{status === "pending" ? "Waiting" : "Decision"}</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((r) => (
                    <tr key={r.id}>
                      <td>
                        <button onClick={() => setSelected(r)}>
                          Inspect {r.id}
                        </button>
                      </td>
                      <td>
                        {r.model_name} · {r.model_version}
                      </td>
                      <td>
                        Gate {r.gate}: {gateLabels[r.gate]}
                      </td>
                      <td>{r.created_at}</td>
                      <td>
                        {r.status === "pending"
                          ? waitingTime(r.created_at, now)
                          : r.resolution?.decision}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          {selected && (
            <ReviewDetail
              key={selected.id + selected.artifact_version}
              item={selected}
              openModel={openModel}
              onSaved={(review, run) => {
                focusAfterSaved.current = review.id;
                setNotice(
                  "Backend recorded " + (review.resolution?.decision ?? "decision") +
                  " for artifact " + review.artifact_id + " version " +
                  review.artifact_version + ". " +
                  (run ? isRevisionRequested(run) ? revisionRequestedMessage : "Run " + run.id + " status: " + run.status + "." :
                    "Run status not yet refreshed; inspect History."),
                );
                setLoading(true);
                setStatus("decided");
                setRevision((v) => v + 1);
              }}
            />
          )}
        </>
      )}
    </section>
  );
}
function ReviewDetail({
  item,
  openModel,
  onSaved,
}: {
  item: ReviewItem;
  openModel?: (id: string) => void;
  onSaved: (item: ReviewItem, run: RunRead | null) => void;
}) {
  const { user } = useAuth();
  const [current, setCurrent] = useState(item);
  const [showProposals, setShowProposals] = useState(false);
  const reviewerEligible = canDecideReview(current, user);
  const [decision, setDecision] = useState(""),
    [rationale, setRationale] = useState(""),
    [conditions, setConditions] = useState("");
  const [acknowledgements, setAcknowledgements] = useState<Record<string, string>>({});
  const [formError, setFormError] = useState("");
  let draft: ReviewDraft | null = null;
  let decisionHint = "";
  try {
    const writtenConditions = current.gate === 3 && current.limitations !== undefined && decision === "accepted_with_conditions"
      ? limitationConditions(current.limitations, acknowledgements) : conditions;
    draft = prepareReviewDraft(current, decision, rationale, writtenConditions);
  } catch (error) {
    decisionHint = (error as Error).message;
  }
  const [openedAt, setOpenedAt] = useState<string | null>(null);
  const [reviewBusy, setReviewBusy] = useState(false);
  const [confirmed, setConfirmed] = useState(false);
  const [uncertain, setUncertain] = useState(false);
  const [reviewFeedback, setReviewFeedback] = useState("");
  const [document, setDocument] = useState<string | null>(null),
    [evidence, setEvidence] = useState<Evidence | null>(null);
  const [busy, setBusy] = useState(false),
    [error, setError] = useState("");
  const controller = useRef<AbortController | null>(null),
    lock = useRef(false);
  const reviewController = useRef<AbortController | null>(null);
  const reviewLock = useRef(false);
  useEffect(() => () => {
    controller.current?.abort();
    reviewController.current?.abort();
  }, []);
  const element = useRef<HTMLElement>(null);
  const updates = useSavedUpdates({
    scope: `${current.id}:${current.artifact_version}`, enabled: current.status === "pending" && !reviewBusy && !busy && !uncertain, element,
    read: signal => api.getReview(current.id, signal),
    apply: fresh => {
      if (!sameReviewCheckpoint(current, fresh) || fresh.status !== current.status || !sameReviewPermissions(current, fresh)) {
        invalidateInspection(); setUncertain(true);
        setFormError("This review changed. Your notes are preserved. Refresh saved review status and inspect the current evidence before deciding.");
      }
    },
    failed: () => {
      invalidateInspection(); setUncertain(true);
      setFormError("Could not verify the current review. Your notes are preserved; refresh saved review status before deciding.");
    },
  });
  function invalidateInspection() {
    controller.current?.abort();
    controller.current = null;
    lock.current = false;
    setBusy(false);
    setDocument(null);
    setEvidence(null);
    setError("");
    setShowProposals(false);
    setOpenedAt(null);

    setConfirmed(false);
  }
  async function openForAudit() {
    if (!reviewerEligible || reviewLock.current || current.status !== "pending") return;
    reviewLock.current = true;
    setReviewBusy(true);
    setFormError("");
    const c = new AbortController();
    reviewController.current = c;
    try {
      const fresh = await api.getReview(current.id, c.signal);
      if (!sameReviewCheckpoint(current, fresh) || fresh.status !== "pending")
        throw new Error("Checkpoint changed or was decided. Reload the review before acting.");
      const opened = await api.openReview(fresh.id, c.signal);
      if (!c.signal.aborted) {
        setCurrent(fresh);
        setOpenedAt(opened.opened_at);
        setReviewFeedback("Backend recorded review open at " + opened.opened_at + ". " + opened.measurement);
      }
    } catch (error) {
      if (!c.signal.aborted) {
        invalidateInspection();
        setFormError((error as Error).message + " No decision was submitted.");
        setUncertain(true);
      }
    } finally {
      reviewLock.current = false;
      if (!c.signal.aborted) setReviewBusy(false);
    }
  }
  /** Re-read Gate 2 blockers after a parity run; never changes a decision. */
  async function refreshParityStatus() {
    try {
      const fresh = await api.getReview(current.id);
      if (sameReviewCheckpoint(current, fresh) && fresh.status === current.status)
        setCurrent(fresh);
    } catch {
      /* The parity panel already shows the run; the review keeps its last read. */
    }
  }
  async function refreshReview() {
    if (reviewLock.current) return;
    invalidateInspection();
    setUncertain(true);
    reviewLock.current = true;
    setReviewBusy(true);
    setFormError("");
    const c = new AbortController();
    reviewController.current = c;
    try {
      const fresh = await api.getReview(current.id, c.signal);
      if (c.signal.aborted) return;
      setUncertain(false);
      if (fresh.status === "decided") {
        setCurrent(fresh);
        setReviewFeedback("Backend reports this review as decided. No new decision POST was sent.");
      } else {
        if (!sameReviewCheckpoint(current, fresh)) {
          setOpenedAt(null);

          setConfirmed(false);
          setFormError("The artifact or checkpoint changed. Inspect the updated review and explicitly open it again.");
        } else {
          setUncertain(false);
          setOpenedAt(null);

          setConfirmed(false);
          setReviewFeedback("Backend confirms review is pending. Explicitly reopen it and reconfirm before a new submission.");
        }
        setCurrent(fresh);
      }
    } catch (error) {
      if (!c.signal.aborted) setFormError((error as Error).message + " Outcome remains uncertain; do not resubmit.");
    } finally {
      reviewLock.current = false;
      if (!c.signal.aborted) setReviewBusy(false);
    }
  }
  async function submitConfirmed() {
    if (!draft || !confirmed || !openedAt || !reviewerEligible ||
      !canChooseReviewDecision(current, user, draft.decision) ||
      uncertain || reviewLock.current) return;
    reviewLock.current = true;
    setReviewBusy(true);
    setFormError("");
    setReviewFeedback("");
    const c = new AbortController();
    reviewController.current = c;
    let posted = false;
    try {
      // Read the latest Backend checkpoint before committing this exact version.
      const fresh = await api.getReview(current.id, c.signal);
      if (c.signal.aborted) return;
      if (fresh.status !== "pending" || !sameReviewCheckpoint(current, fresh) ||
        !sameReviewPermissions(current, fresh) ||
        !canChooseReviewDecision(fresh, user, draft.decision))
        throw new Error("Review is no longer pending or the version/gate changed. Reload it before submitting.");
      posted = true;
      const decided = await api.decideReview(fresh, draft, c.signal);
      if (c.signal.aborted) return;
      setCurrent(decided);

      setConfirmed(false);
      setUncertain(false);
      // The POST returns a persisted decision. Read the run; it may remain
      // awaiting_review until the agent is explicitly resumed.
      let run: RunRead | null = null;
      try {
        const latest = await api.getRun(decided.run_id, c.signal);
        if (latest.model_id === decided.model_id && latest.model_version === decided.model_version)
          run = latest;
      } catch {
        // Never turn a successful recorded decision into a retryable POST.
      }
      if (!c.signal.aborted) onSaved(decided, run);
    } catch (error) {
      if (c.signal.aborted) return;
      const message = (error as Error).message;
      setFormError(
        message + (posted
          ? " Submission outcome may have changed; check the saved review before retrying. No automatic retry."
          : " No decision was submitted; inspect the current checkpoint."),
      );
      // Even a preflight mismatch needs read-only reconciliation before another decision.
      invalidateInspection();
      setUncertain(true);
    } finally {
      reviewLock.current = false;
      if (!c.signal.aborted) setReviewBusy(false);
    }
  }
  async function artifact(download: boolean) {
    if (lock.current || reviewBusy || uncertain) return;
    lock.current = true;
    setBusy(true);
    setError("");
    const c = new AbortController();
    controller.current = c;
    try {
      if (download) {
        const blob = await api.downloadArtifact(current.artifact_id, c.signal);
        if (c.signal.aborted) return;
        const url = URL.createObjectURL(blob),
          link = window.document.createElement("a");
        link.href = url;
        link.download = "review-" + current.id.replace(/[^a-zA-Z0-9_-]/g, "_");
        window.document.body.append(link);
        link.click();
        link.remove();
        setTimeout(() => URL.revokeObjectURL(url), 1000);
      } else if (current.gate === 1) {
        setDocument(null);
        const text = await api.getDocumentText(current.artifact_id, c.signal);
        if (!c.signal.aborted) setDocument(text);
      } else if (current.gate === 3) {
        setEvidence(null);
        const value = await api.getValidationEvidence(
          current.artifact_id,
          c.signal,
        );
        if (!c.signal.aborted) setEvidence(value);
      }
    } catch (e) {
      if (!c.signal.aborted) setError((e as Error).message);
    } finally {
      if (controller.current === c) {
        lock.current = false;
        if (!c.signal.aborted) setBusy(false);
      }
    }
  }
  const submitHint = reviewBusy
    ? "Checking the saved review. Please wait."
    : uncertain
      ? "Refresh saved review status, inspect the current evidence and reopen the review."
      : !reviewerEligible
        ? current.requested_by === user.id
          ? "You cannot review work you started. Ask an independent reviewer."
          : current.gate === 3 && user.role === "validator"
            ? "Gate 3 requires an independent approver."
            : "Your account cannot submit this review."
        : !draft
          ? decisionHint
          : !canChooseReviewDecision(current, user, draft.decision)
            ? "This decision is unavailable. Check the review requirements above."
            : !openedAt
              ? "Select Open review for audit above before submitting."
              : !confirmed
                ? "Tick the confirmation after inspecting the evidence and decision."
                : "Ready to submit your decision.";
  return (
    <section ref={element} aria-label="Selected review" className="lifecycle-card">
      {updates && <p role="status">{updates}</p>}
      <h3>
        Gate {current.gate}: {gateLabels[current.gate]}
      </h3>
      <p>
        {current.model_name} · model version {current.model_version} · run{" "}
        {current.run_id}
      </p>
      <p>
        Artifact: {current.artifact_id} · artifact version {current.artifact_version}
      </p>
      {current.hard_blockers !== undefined && current.limitations !== undefined ? (
        <section aria-label="Review requirements" className="notice">
          <h4>Hard blockers</h4>
          {current.hard_blockers.length ? <ul>{current.hard_blockers.map(id => <li key={id}>{id}</li>)}</ul> : <p>No hard blockers reported.</p>}
          <h4>Limitations requiring acknowledgement</h4>
          {current.limitations.length ? <><ul>{current.limitations.map(id => <li key={id}>{id}</li>)}</ul><p>Plain acceptance is unavailable. Each limitation needs a written condition if conditional acceptance is allowed.</p></> : <p>No limitations reported.</p>}
        </section>
      ) : current.blockers && current.blockers.length > 0 && (
        <div className="notice" role="status">
          Backend blocking checks: {current.blockers.join(", ")}. Approval is not available.
        </div>
      )}
      {current.blockers?.includes("official_parity_not_passed") && (
        <p className="notice">Gate 2 acceptance needs a passing official parity run on this exact candidate. Run parity below, or request changes. A rebuild run or local draft is not approval.</p>
      )}
      {openModel ? <button
        className="button secondary"
        onClick={() => openModel(current.model_id)}
      >Open model inventory</button> : <a className="button secondary"
        href={`/?model=${encodeURIComponent(current.model_id)}`}>Open model inventory</a>}
      {current.status === "pending" && reviewerEligible && (
        <div className="evidence-actions">
          <button type="button" className="button secondary"
            disabled={reviewBusy || uncertain || !!openedAt}
            onClick={() => void openForAudit()}>
            {openedAt ? "Review opened in Backend" : "Open review for audit"}
          </button>
          <span>{openedAt ? "Opened at " + openedAt : "Open the review before submission."}</span>
        </div>
      )}
      {reviewFeedback && <p role="status">{reviewFeedback}</p>}
      {current.status === "pending" && uncertain && (
        <button type="button" className="button secondary"
          disabled={reviewBusy} onClick={() => void refreshReview()}>
          Refresh saved review status (read only)
        </button>
      )}
      <div className="review-detail-layout">
        <section aria-label="Review artifact" className="review-artifact-pane" tabIndex={0}>
          <h4>Evidence to inspect</h4>
          <div className="evidence-actions">
            {current.gate !== 2 && (
              <button
                className="button secondary"
                disabled={busy || reviewBusy || uncertain}
                onClick={() => void artifact(false)}
              >
                Preview review artifact
              </button>
            )}
            <button
              className="button secondary"
              disabled={busy || reviewBusy || uncertain}
              onClick={() => void artifact(true)}
            >
              Download review artifact
            </button>
          </div>
          {current.gate === 2 && (
            <p>
              Download the candidate artifact to inspect its code and parameter
              files; no code is executed in the browser.
            </p>
          )}
          {current.gate === 2 && current.candidate_model_id && (
            <ParityPanel
              modelId={current.candidate_model_id}
              candidate={{ id: current.candidate_model_id }}
              onRan={() => void refreshParityStatus()}
            />
          )}
          {/* Saved candidate-bound input-check API contract is still pending. */}
          {current.gate === 2 && <InputContractChecks evidence={current.input_contract}
            candidateId={current.candidate_model_id} rebuildRunId={current.run_id} />}
          {error && <p role="alert">{error}</p>}
          {document !== null && (
            <DocumentReader source={document} reportId={current.id} />
          )}
          {current.gate === 3 && !uncertain && !reviewBusy && (
            <details open={showProposals} key={current.run_id + ":" + current.artifact_version}
              onToggle={(event) => setShowProposals(event.currentTarget.open)}>
              <summary>Agent proposals for this validation run</summary>
              <p>Inspect the suggested findings before sign-off. Proposal decisions are separate from Gate 3 approval.</p>
              {showProposals && <LiveProposals key={current.model_id + ":" + current.run_id}
                modelId={current.model_id} sourceRunId={current.run_id} />}
            </details>
          )}
          {evidence && (
            <ValidationEvidence
              evidence={evidence}
              backendModelId={current.model_id}
            />
          )}
        </section>
        {current.status === "pending" ? (
          <form
            className="form review-draft"
            onSubmit={(event) => { event.preventDefault(); void submitConfirmed(); }}
           >
            <h4>Review your decision</h4>
            <p>Choose a decision, explain your reason and confirm before submitting.</p>
            <label>
              Decision
              <select
                aria-label="Decision"
                value={decision}
                onChange={(e) => { setDecision(e.target.value); setConfirmed(false); }}
              >
                <option value="">Choose a decision</option>
                <option value="accepted" disabled={!canChooseReviewDecision(current, user, "accepted")}>Accepted</option>
                <option value="changes_requested" disabled={!canChooseReviewDecision(current, user, "changes_requested")}>Changes requested</option>
                <option value="accepted_with_conditions" disabled={!canChooseReviewDecision(current, user, "accepted_with_conditions")}>
                  Accepted with conditions
                </option>
              </select>
            </label>
            <label>
              Rationale
              <textarea
                aria-label="Rationale" value={rationale}
                onChange={(e) => { setRationale(e.target.value); setConfirmed(false); }}
                rows={4}
              />
            </label>
            {decision === "accepted_with_conditions" && (
              current.gate === 3 && current.limitations !== undefined ?
              <fieldset><legend>Acknowledge every limitation</legend>
                <p>Explain the condition or restriction you accept for each check. Its status will not change.</p>
                {current.limitations.map(id => <label key={id}>
                  Acknowledgement for {id}
                  <textarea aria-label={`Acknowledgement for ${id}`} rows={3}
                    value={acknowledgements[id] ?? ""}
                    onChange={event => { setAcknowledgements(previous => ({ ...previous, [id]: event.target.value })); setConfirmed(false); }} />
                </label>)}
              </fieldset> :
              <label>
                Conditions (one per line)
                <textarea
                  aria-label="Conditions (one per line)" value={conditions}
                  onChange={(e) => { setConditions(e.target.value); setConfirmed(false); }}
                  rows={3}
                />
              </label>
            )}
            {draft && (
              <section aria-label="Decision summary">
                <h4>Decision summary — not submitted</h4>
                <dl className="decision-summary"><dt>Decision</dt><dd>{draft.decision.replaceAll("_", " ")}</dd><dt>Reason</dt><dd>{draft.rationale}</dd><dt>Conditions</dt><dd>{draft.conditions.join("; ") || "None"}</dd></dl>
                <details className="help-details"><summary>Decision identifiers</summary><pre>{JSON.stringify(draft, null, 2)}</pre></details>
                <p>{current.model_name} · version {current.model_version} · Gate {current.gate}: {gateLabels[current.gate]}</p>

              </section>
            )}
            <label className="review-confirmation">
              <input type="checkbox" checked={confirmed} disabled={!draft || reviewBusy || uncertain || !reviewerEligible}
                onChange={(e) => setConfirmed(e.target.checked)} />
              I inspected this artifact and confirm the exact version and decision above
            </label>
            <p className="page-description" aria-label="Submission requirements">{submitHint}</p>
            <div className="evidence-actions">
              <button type="submit" className="button primary"
                disabled={reviewBusy || !reviewerEligible || !openedAt || !confirmed ||
                  uncertain || !draft ||
                  !canChooseReviewDecision(current, user, draft.decision)}
                >
                {reviewBusy ? "Checking Backend…" : "Submit decision"}
              </button>
            </div>
            {formError && <p role="alert">{formError}</p>}
            <p>Your decision is recorded only after Submit decision. If its outcome is unclear, refresh the saved review before trying again.</p>
          </form>
        ) : (
          <section aria-label="Recorded decision" className="lifecycle-card">
            <h4>Backend-returned decision</h4>
            <p>
              {current.resolution?.decision} · {current.resolution?.decided_at}
            </p>
            {current.resolution?.review_elapsed_seconds !== undefined &&
              current.resolution.review_elapsed_seconds !== null && (
              <p>
                Elapsed since review opened: {current.resolution.review_elapsed_seconds} seconds.
                {current.resolution.timing_limitation ? " " + current.resolution.timing_limitation : ""}
              </p>
            )}
            <p>Reviewer reference: {current.resolution?.reviewer_id}</p>
            <p>{current.resolution?.rationale}</p>
            {!!current.resolution?.conditions.length && (
              <ul>
                {current.resolution.conditions.map((c, i) => (
                  <li key={i}>{c}</li>
                ))}
              </ul>
            )}
            <p>
              This is a read-only decision returned by the Backend. No new
              decision is submitted here.
            </p>
          </section>
        )}
      </div>
    </section>
  );
}
