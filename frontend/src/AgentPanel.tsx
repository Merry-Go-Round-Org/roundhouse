export { DurableAgentPanel as AgentPanel } from "./DurableAgentPanel";
import { useAuth } from "./AuthContext";
import { canStartOperation } from "./auth";
import { useEffect, useRef, useState } from "react";
import { createModelClient, type ModelRecord, type RunRead, type Operation } from "./api";
const api = createModelClient();
export function RunPanel({
  model,
  onRunChange,
  operations,
}: {
  model: ModelRecord;
  onRunChange?: () => void;
  operations?: readonly Operation[];
}) {
  const { user } = useAuth();
  const [run, setRun] = useState<RunRead | null>(null),
    [busy, setBusy] = useState(false),
    [error, setError] = useState("");
  const lock = useRef(false);
  const lifetime = useRef<AbortController | null>(null);
  useEffect(() => {
    const c = new AbortController();
    lifetime.current = c;
    return () => c.abort();
  }, []);
  useEffect(() => {
    if (!run || !["queued", "running"].includes(run.status)) return;
    const c = new AbortController();
    const timer = setTimeout(() => {
      api
        .getRun(run.id, c.signal)
        .then((v) => {
          if (!c.signal.aborted) {
            setRun(v);
            setError("");
            onRunChange?.();
          }
        })
        .catch((e) => {
          if (!c.signal.aborted) setError(e.message);
        });
    }, 2000);
    return () => {
      clearTimeout(timer);
      c.abort();
    };
  }, [run, onRunChange]);
  const nonBinary = model.dataset_id === null || model.inventory?.synthetic_demo === true;
  const operationApplicable = (operation: Operation) =>
    model.inventory?.applicability?.[operation] !== "NOT_APPLICABLE" &&
    (operation === "document" || !nonBinary);
  async function start(operation: Operation) {
    if (!operationApplicable(operation)) return; // Never dispatch NOT_APPLICABLE synthetic operations.
    if (lock.current) return;
    lock.current = true;
    setBusy(true);
    setError("");
    const signal = lifetime.current!.signal;
    try {
      const result = await api.startRun(
        model.id,
        {
          operation,
          model_version: model.version,
          dataset_id: model.dataset_id,
        },
        signal,
      );
      if (signal.aborted) return;
      setRun(result);
      onRunChange?.();
    } catch (e) {
      if (signal.aborted) return;
      setError(
        (e as Error).message +
          " No automatic retry: confirm the submission outcome before retrying.",
      );
    } finally {
      lock.current = false;
      if (!signal.aborted) setBusy(false);
    }
  }
  return (
    <section aria-label="Model lifecycle">
      <h2>Model lifecycle</h2>
      <p>
        Start a task for model version {model.version}. Generated output remains
        subject to review.
      </p>
      <div className="lifecycle-actions">
        {(
          [
            [
              "document",
              "Generate new documentation",
              "Create a new document from the model and its uploaded source files.",
            ],
            [
              "rebuild",
              "Rebuild in Python",
              "Ask the agent to rebuild this model in Python and compare its predictions with the original.",
            ],
            [
              "validate",
              "Run validation",
              "Check performance, calibration, stability and fairness. Review the results before approval.",
            ],
            [
              "monitor",
              "Request monitoring",
              "Request monitoring through the existing run API. Results require the backend implementation.",
            ],
          ] as const
        )
          .filter(
            ([operation]) => !operations || operations.includes(operation),
          )
          .map(([operation, label, description]) => (
            <article className="lifecycle-card" key={operation}>
              <h3>{label}</h3>
              <p>{description}</p>
              {!operationApplicable(operation) && (
                <p role="status">
                  NOT_APPLICABLE: this synthetic survival model has no eligible
                  binary dataset for {operation}. No job or passed check is implied.
                </p>
              )}
              <button
                className="button primary"
                disabled={
                  busy ||
                  !canStartOperation(user.role, operation) ||
                  // Monitoring needs explicit batch/simulation inputs, not a generic run.
                  operation === "monitor" ||
                  !operationApplicable(operation) ||
                  (operation === "rebuild" && model.language !== "R") ||
                  (!!run &&
                    ["queued", "running", "awaiting_review"].includes(
                      run.status,
                    ))
                }
                onClick={() => void start(operation)}
              >
                {busy ? "Submitting…" : label}
              </button>
            </article>
          ))}
      </div>

      <details className="parity-contract">
        <summary>How iterative rebuild and parity work</summary>
        <ol>
          <li>
            The legacy reference predictions, development inputs and the
            tolerance (1e-8 absolute) are fixed by the platform.
          </li>
          <li>
            The agent rebuilds the model, exports a candidate and runs official
            parity: the same platform check a person runs with Run parity.
          </li>
          <li>
            Outside tolerance: the agent fixes the candidate using the worst
            rows, exports again and reruns parity, within fixed iteration, time
            and cost limits.
          </li>
          <li>
            Within tolerance, or limit reached: the agent requests Gate 2
            review, saying which. The tolerance is never relaxed.
          </li>
          <li>
            Gate 2 can be accepted only with a passing parity run on the exact
            candidate under review. The reviewer can rerun it.
          </li>
        </ol>
        <p>
          Every parity run is recorded on the Rebuild &amp; parity tab, labelled
          by who started it. No parity pass is inferred from a completed run.
        </p>
      </details>
      {error && <p role="alert">{error}</p>}
      {run && (
        <div role="status">
          <p>
            {run.id}: {run.status} · {run.stage ?? "No stage supplied"}
          </p>
          {run.error && <p>{run.error}</p>}
          {error && (
            <button onClick={() => setRun({ ...run })}>
              Retry status check
            </button>
          )}
        </div>
      )}
      <p>Finished tasks still require human review.</p>
    </section>
  );
}
