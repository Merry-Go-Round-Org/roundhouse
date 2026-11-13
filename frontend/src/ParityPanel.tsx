import { useEffect, useRef, useState } from "react";
import { createModelClient } from "./api";
import { parityOutcome, type ParityRun } from "./parity";

const api = createModelClient();
const pageSize = 20;
type Props = {
  modelId: string;
  candidate?: { id: string; version?: string } | null;
  onRan?: (run: ParityRun) => void;
};
const difference = (value: unknown) =>
  typeof value === "number" ? value.toExponential(3) : "Not available";

export function ParityPanel(props: Props) {
  return (
    <ParityContent
      key={`${props.modelId}:${props.candidate?.id ?? ""}:${props.candidate?.version ?? ""}`}
      {...props}
    />
  );
}
function ParityContent({ modelId, candidate, onRan }: Props) {
  const [runs, setRuns] = useState<ParityRun[]>([]);
  const [offset, setOffset] = useState(0),
    [revision, setRevision] = useState(0);
  const [loading, setLoading] = useState(true),
    [busy, setBusy] = useState(false);
  const [readError, setReadError] = useState(""),
    [error, setError] = useState("");
  const [version, setVersion] = useState(candidate?.version ?? "");
  const [chosen, setChosen] = useState("");
  const [uncertain, setUncertain] = useState(false),
    [reconciled, setReconciled] = useState(false),
    [confirmed, setConfirmed] = useState(false);
  const request = useRef<AbortController | null>(null);
  useEffect(() => () => request.current?.abort(), []);
  useEffect(() => {
    const c = new AbortController();
    api
      .listParityRuns(modelId, c.signal, offset, pageSize)
      .then((value) => {
        if (!c.signal.aborted) {
          setRuns(value);
          setReconciled(true);
        }
      })
      .catch((e) => {
        if (!c.signal.aborted) setReadError(e.message);
      })
      .finally(() => {
        if (!c.signal.aborted) setLoading(false);
      });
    return () => c.abort();
  }, [modelId, offset, revision]);
  useEffect(() => {
    if (!candidate?.id || candidate.version) return;
    const c = new AbortController();
    api
      .get(candidate.id, c.signal)
      .then((value) => {
        if (!c.signal.aborted) setVersion(value.version);
      })
      .catch((e) => {
        if (!c.signal.aborted)
          setError("Cannot read candidate version: " + e.message);
      });
    return () => c.abort();
  }, [candidate?.id, candidate?.version]);
  const options = [
    ...new Map(
      runs.map((run) => [
        run.candidate_model_id + ":" + run.candidate_version,
        { id: run.candidate_model_id, version: run.candidate_version },
      ]),
    ).entries(),
  ];
  const target = candidate?.id
    ? version
      ? { id: candidate.id, version }
      : null
    : (options.find(([key]) => key === chosen)?.[1] ?? options[0]?.[1]);
  function refresh(page = offset) {
    setLoading(true);
    setRuns([]);
    setReadError("");
    setChosen("");
    setConfirmed(false);
    setReconciled(false);
    setOffset(page);
    setRevision((v) => v + 1);
  }
  async function run() {
    if (
      !target ||
      request.current ||
      loading ||
      readError ||
      (uncertain && (!reconciled || !confirmed))
    )
      return;
    const c = new AbortController();
    request.current = c;
    setBusy(true);
    setError("");
    setConfirmed(false);
    try {
      const result = await api.runParity(target.id, target.version, c.signal);
      if (c.signal.aborted) return;
      setUncertain(false);
      onRan?.(result);
      refresh(0);
    } catch (e) {
      if (!c.signal.aborted) {
        setError((e as Error).message);
        setUncertain(true);
        setReconciled(false);
      }
    } finally {
      request.current = null;
      if (!c.signal.aborted) setBusy(false);
    }
  }
  const latest = runs[0];
  return (
    <section
      className="lifecycle-card parity-panel"
      aria-label="Official parity"
    >
      <h2>Official parity</h2>
      <p>
        Agents and people run the same platform check: candidate predictions
        against legacy reference predictions on development fixture rows, with
        absolute tolerance 1e-8. This is not validation or final-test evidence.
        A passing result does not grant human approval.
      </p>
      <button
        type="button"
        disabled={busy || loading}
        onClick={() => refresh()}
      >
        Refresh parity history
      </button>
      {loading && <p role="status">Loading parity history…</p>}
      {readError && (
        <p role="alert">{readError} Refresh history to retry the read.</p>
      )}
      {!candidate?.id && !loading && !readError && options.length > 0 && (
        <label>
          Candidate to compare
          <select
            aria-label="Candidate to compare"
            disabled={busy || uncertain}
            value={chosen || options[0][0]}
            onChange={(e) => setChosen(e.target.value)}
          >
            {options.map(([key, value]) => (
              <option key={key} value={key}>
                {value.id} · {value.version}
              </option>
            ))}
          </select>
        </label>
      )}
      <p>
        {target
          ? `Candidate ${target.id} · version ${target.version}`
          : "No candidate available on this history page. Open the candidate inventory record to run its first comparison."}
      </p>
      {uncertain && (
        <div className="notice">
          <p>
            Execution was not confirmed. It may still finish on the server. No
            automatic retry. Refresh saved history and inspect the candidate
            results before requesting another execution; an absent result does
            not prove the earlier request failed.
          </p>
          {reconciled && !loading && !readError && (
            <label>
              <input
                type="checkbox"
                checked={confirmed}
                disabled={busy}
                onChange={(e) => setConfirmed(e.target.checked)}
              />
              I reviewed saved history and explicitly authorize a new parity
              execution.
            </label>
          )}
        </div>
      )}
      <button
        type="button"
        disabled={
          busy ||
          loading ||
          !!readError ||
          !target ||
          (uncertain && (!reconciled || !confirmed))
        }
        onClick={() => void run()}
      >
        {busy ? "Running parity…" : "Run parity"}
      </button>
      {error && <p role="alert">{error}</p>}
      {!loading && !readError && (
        <>
          <p>
            Page {offset / pageSize + 1} · up to {pageSize} runs; no total count
            supplied. Candidate choices cover this page only.
          </p>
          {latest && (
            <p role="status">
              Newest on this page: <strong>{parityOutcome(latest)}</strong> ·{" "}
              {latest.initiator === "agent" ? "Agent" : "Person"} · max absolute
              difference {difference(latest.result.max_absolute_difference)}
            </p>
          )}
          {!runs.length && (
            <p>
              No parity runs returned on this page. Not run is never a pass.
            </p>
          )}
          {runs.map((run) => (
            <article key={run.id} aria-label={`Parity run ${run.id}`}>
              <h3>
                {parityOutcome(run)} · {run.candidate_version}
              </h3>
              <p>
                {run.initiator === "agent" ? "Agent" : "Person"} ·{" "}
                {run.created_at} · candidate {run.candidate_model_id}
              </p>
              <p>
                Maximum difference:{" "}
                {difference(run.result.max_absolute_difference)} · Rows above
                tolerance:{" "}
                {typeof run.result.n_above_tolerance === "number"
                  ? run.result.n_above_tolerance
                  : "Not available"}{" "}
                of{" "}
                {typeof run.result.n_rows === "number"
                  ? run.result.n_rows
                  : "Not available"}
              </p>
              {run.error && <p>{run.error}</p>}
              <details>
                <summary>Evidence for {run.id}</summary>
                <dl>
                  {Object.entries({
                    "Run ID": run.id,
                    "Candidate bundle SHA-256": run.candidate_bundle_sha256,
                    "Parent model": run.parent_model_id,
                    "Parent version": run.parent_version,
                    "Rebuild run": run.rebuild_run_id,
                    "Initiating actor": run.actor_id,
                    ...Object.fromEntries(
                      [
                        "method",
                        "scope",
                        "tolerance_absolute",
                        "tolerance_relative",
                        "candidate_package_sha256",
                        "parent_package_sha256",
                        "input_sha256",
                        "reference_predictions_sha256",
                        "candidate_predictions_sha256",
                        "runtime_image",
                        "limitation",
                      ].map((key) => [key, run.result[key]]),
                    ),
                  }).map(([key, value]) => (
                    <div key={key}>
                      <dt>{key}</dt>
                      <dd style={{ overflowWrap: "anywhere" }}>
                        {typeof value === "string" || typeof value === "number"
                          ? value
                          : "Not supplied"}
                      </dd>
                    </div>
                  ))}
                </dl>
                {!!run.result.worst_rows?.length && (
                  <ul>
                    {run.result.worst_rows.map((row) => (
                      <li key={row.row_id}>
                        {row.row_id}: {difference(row.absolute_difference)}
                      </li>
                    ))}
                  </ul>
                )}
              </details>
            </article>
          ))}
        </>
      )}
      <button
        type="button"
        disabled={busy || loading || offset === 0}
        onClick={() => refresh(Math.max(0, offset - pageSize))}
      >
        Previous parity runs
      </button>
      <button
        type="button"
        disabled={busy || loading || !!readError || runs.length < pageSize}
        onClick={() => refresh(offset + pageSize)}
      >
        Next parity runs
      </button>
    </section>
  );
}
