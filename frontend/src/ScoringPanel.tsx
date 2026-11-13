import { useEffect, useRef, useState, type FormEvent } from "react";
import {
  ApiError,
  createModelClient,
  type ModelRecord,
  type PackageRead,
  type ScoringRead,
} from "./api";
const api = createModelClient();
const active = (r: ScoringRead) =>
  r.status === "queued" || r.status === "running";
const message = (e: unknown) =>
  e instanceof Error ? e.message : "Request failed.";

export function ScoringPanel({ model }: { model: ModelRecord }) {
  const [packages, setPackages] = useState<PackageRead[]>([]);
  const [selected, setSelected] = useState("");
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [revision, setRevision] = useState(0);
  const lifetime = useRef<AbortController | null>(null);
  const lock = useRef(false);
  useEffect(() => {
    const c = new AbortController();
    lifetime.current = c;
    return () => c.abort();
  }, []);
  useEffect(() => {
    const c = new AbortController();
    setLoading(true);
    setError("");
    api
      .listPackages(model.id, c.signal)
      .then((rows) => {
        if (c.signal.aborted) return;
        setPackages(rows);
        setSelected((old) =>
          rows.some((p) => p.id === old) ? old : (rows[0]?.id ?? ""),
        );
      })
      .catch((e) => {
        if (!c.signal.aborted) setError(message(e));
      })
      .finally(() => {
        if (!c.signal.aborted) setLoading(false);
      });
    return () => c.abort();
  }, [model.id, revision]);
  async function register(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    if (lock.current) return;
    const name = String(
      new FormData(e.currentTarget).get("package_name") ?? "",
    );
    lock.current = true;
    setBusy(true);
    setError("");
    const signal = lifetime.current!.signal;
    try {
      const row = await api.registerPackage(model.id, name, signal);
      if (!signal.aborted) {
        setPackages((old) => [...old.filter((p) => p.id !== row.id), row]);
        setSelected(row.id);
      }
    } catch (e) {
      if (!signal.aborted)
        setError(
          message(e) + " Reload packages before retrying a failed request.",
        );
    } finally {
      lock.current = false;
      if (!signal.aborted) setBusy(false);
    }
  }
  const pkg = packages.find((p) => p.id === selected);
  return (
    <section className="scoring-panel" aria-label="Development scoring">
      <h2>Development scoring</h2>
      <p>
        Run a registered model on its development fixture. This does not
        establish validation, migration parity or approval.
      </p>
      <button
        className="button secondary"
        disabled={loading || busy}
        onClick={() => setRevision((v) => v + 1)}
      >
        Reload packages
      </button>
      {loading && <p role="status">Loading packages…</p>}
      {error && <p role="alert">{error}</p>}
      {!loading && !error && packages.length === 0 && (
        <p>No package registered for this model.</p>
      )}
      <form onSubmit={register} className="form">
        <label>
          Package name
          <input
            name="package_name"
            required
            maxLength={120}
            placeholder="credit_logistic_r"
            disabled={busy || loading}
          />
        </label>
        <p>
          Use a package already available on the backend. Model version,
          language and dataset must match. The R credit example uses version
          1.0.0.
        </p>
        <button className="button secondary" disabled={busy || loading}>
          {busy ? "Registering…" : "Register package"}
        </button>
      </form>
      {packages.length > 0 && (
        <label>
          Registered package
          <select
            value={selected}
            disabled={busy || loading}
            onChange={(e) => setSelected(e.target.value)}
          >
            {packages.map((p) => (
              <option key={p.id} value={p.id}>
                {p.name} · {p.version}
              </option>
            ))}
          </select>
        </label>
      )}
      {pkg && !loading && <PackageScoring key={pkg.id} pkg={pkg} />}
    </section>
  );
}

function PackageScoring({ pkg }: { pkg: PackageRead }) {
  const [rows, setRows] = useState<ScoringRead[]>([]);
  const [offset, setOffset] = useState(0);
  const [revision, setRevision] = useState(0);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [download, setDownload] = useState<string | null>(null);
  const [error, setError] = useState("");
  const [submissionError, setSubmissionError] = useState("");
  const [uncertain, setUncertain] = useState(false);
  const lifetime = useRef<AbortController | null>(null);
  const lock = useRef(false);
  const downloading = useRef(false);
  useEffect(() => {
    const c = new AbortController();
    lifetime.current = c;
    return () => c.abort();
  }, []);
  useEffect(() => {
    const c = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    setLoading(true);
    setError("");
    setRows([]);
    async function load() {
      try {
        const history = await api.listScoring(pkg.id, offset, c.signal);
        // Read active execution IDs explicitly; never assume HTTP 202 means finished.
        const current = await Promise.all(
          history.map((r) =>
            active(r) ? api.getScoring(pkg.id, r.id, c.signal) : r,
          ),
        );
        if (c.signal.aborted) return;
        setRows(current);
        setLoading(false);
        setUncertain(false);
        if (current.some(active)) timer = setTimeout(load, 2000);
      } catch (e) {
        if (!c.signal.aborted) {
          setError(message(e));
          setLoading(false);
        }
      }
    }
    void load();
    return () => {
      c.abort();
      clearTimeout(timer);
    };
  }, [pkg.id, offset, revision]);
  async function start() {
    if (lock.current || uncertain) return;
    lock.current = true;
    setBusy(true);
    setSubmissionError("");
    const signal = lifetime.current!.signal;
    try {
      const run = await api.startScoring(pkg.id, signal);
      if (!signal.aborted) {
        setRows((old) => [run, ...old.filter((r) => r.id !== run.id)]);
        setOffset(0);
        setRevision((v) => v + 1);
      }
    } catch (e) {
      if (!signal.aborted) {
        setUncertain(true);
        setSubmissionError(
          message(e) +
            (e instanceof ApiError && e.runId
              ? ` Saved execution: ${e.runId}.`
              : "") +
            " Reload scoring history before deciding whether to retry; the request may have created a job.",
        );
      }
    } finally {
      lock.current = false;
      if (!signal.aborted) setBusy(false);
    }
  }
  async function save(run: ScoringRead) {
    if (downloading.current) return;
    downloading.current = true;
    setDownload(run.id);
    setError("");
    const signal = lifetime.current!.signal;
    try {
      const blob = await api.downloadPredictions(run.id, signal);
      if (signal.aborted) return;
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = `predictions-${run.id.replace(/[^a-zA-Z0-9_-]/g, "_")}.csv`;
      document.body.append(link);
      link.click();
      link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (e) {
      if (!signal.aborted) setError(message(e));
    } finally {
      downloading.current = false;
      if (!signal.aborted) setDownload(null);
    }
  }
  return (
    <div>
      <p>
        {pkg.n_rows} development rows · {pkg.language} · {pkg.dataset_id}
      </p>
      <details>
        <summary>Package evidence</summary>
        <p>
          Package SHA-256: <code>{pkg.sha256}</code>
        </p>
        <p>
          Input SHA-256: <code>{pkg.input_sha256}</code>
        </p>
      </details>
      <div className="form-actions">
        <button
          className="button primary"
          disabled={
            busy ||
            loading ||
            uncertain ||
            !!error ||
            offset !== 0 ||
            rows.some(active)
          }
          onClick={start}
        >
          {busy ? "Submitting…" : "Score development data"}
        </button>
        <button
          className="button secondary"
          disabled={busy || loading}
          onClick={() => {
            setOffset(0);
            setRevision((v) => v + 1);
          }}
        >
          Reload scoring history
        </button>
      </div>
      {submissionError && <p role="alert">{submissionError}</p>}
      {error && (
        <p role="alert">{error} Use Reload scoring history to retry reads.</p>
      )}
      {loading && <p role="status">Loading scoring history…</p>}
      {!loading && !error && rows.length === 0 && <p>No scoring runs yet.</p>}
      <div aria-live="polite">
        {rows.map((run) => (
          <article className="scoring-result" key={run.id}>
            <h3>Scoring · {run.status}</h3>
            <p>Execution: {run.id}</p>
            <p>
              Created: {run.created_at}
              <br />
              Started: {run.started_at ?? "Not started"}
              <br />
              Finished: {run.finished_at ?? "Not finished"}
            </p>
            {run.error && <p role="alert">Job failed: {run.error}</p>}
            {run.result ? (
              <>
                <p>{run.result.n_rows} prediction rows</p>
                <p>{run.result.limitation}</p>
                <details>
                  <summary>Result evidence</summary>
                  <p>
                    Package SHA-256: <code>{run.result.package_sha256}</code>
                  </p>
                  <p>
                    Input SHA-256: <code>{run.result.input_sha256}</code>
                  </p>
                  <p>
                    Predictions SHA-256:{" "}
                    <code>{run.result.predictions_sha256}</code>
                  </p>
                  <p>
                    Runtime: <code>{run.result.runtime_image}</code>
                  </p>
                </details>
              </>
            ) : (
              <p>No result evidence supplied yet.</p>
            )}
            <button
              className="button secondary"
              disabled={run.status !== "completed" || download !== null}
              onClick={() => void save(run)}
            >
              {download === run.id
                ? "Downloading…"
                : "Download predictions CSV"}
            </button>
          </article>
        ))}
      </div>
      <div className="form-actions">
        <button
          className="button secondary"
          disabled={loading || busy || offset === 0}
          onClick={() => setOffset((v) => Math.max(0, v - 20))}
        >
          Previous scoring runs
        </button>
        <span>Page {offset / 20 + 1}</span>
        <button
          className="button secondary"
          disabled={loading || busy || !!error || rows.length < 20}
          onClick={() => setOffset((v) => v + 20)}
        >
          Next scoring runs
        </button>
      </div>
    </div>
  );
}
