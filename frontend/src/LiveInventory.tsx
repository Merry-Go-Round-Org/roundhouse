import { ModelDocuments } from "./ModelDocuments";
import { SourceFiles } from "./SourceFiles";
import { ScoringPanel } from "./ScoringPanel";
import { useCallback, useEffect, useState, type FormEvent } from "react";
import {
  createModelClient,
  type DatasetRead,
  type ModelCreate,
  type ModelRecord,
  type RunRead,
} from "./api";
import { AgentPanel, RunPanel } from "./AgentPanel";
const client = createModelClient();
export function LiveInventory() {
  const [rows, setRows] = useState<ModelRecord[]>([]),
    [offset, setOffset] = useState(0),
    [revision, setRevision] = useState(0);
  const [loading, setLoading] = useState(true),
    [error, setError] = useState(""),
    [saving, setSaving] = useState(false),
    [notice, setNotice] = useState("");
  const [runRevision, setRunRevision] = useState(0);
  const refreshRuns = useCallback(() => setRunRevision((v) => v + 1), []);
  const [selected, setSelected] = useState<ModelRecord | null>(null);
  const [selectedId, setSelectedId] = useState(
    () => new URLSearchParams(window.location.search).get("model") ?? "",
  );
  const [selectionError, setSelectionError] = useState("");
  function selectModel(id: string) {
    const url = new URL(window.location.href);
    url.searchParams.set("model", id);
    window.history.replaceState(null, "", url);
    setSelected(null);
    setSelectedId(id);
  }
  useEffect(() => {
    const c = new AbortController();
    setSelected(null);
    setSelectionError("");
    if (selectedId)
      client
        .get(selectedId, c.signal)
        .then((row) => {
          if (!c.signal.aborted) setSelected(row);
        })
        .catch((e) => {
          if (!c.signal.aborted) setSelectionError(e.message);
        });
    return () => c.abort();
  }, [selectedId, revision]);
  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError("");
    client
      .list(offset, 20, controller.signal)
      .then((rows) => {
        if (!controller.signal.aborted) setRows(rows);
      })
      .catch((e) => {
        if (e.name !== "AbortError") setError(e.message);
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [offset, revision]);
  const [datasets, setDatasets] = useState<DatasetRead[]>([]);
  const [datasetId, setDatasetId] = useState("");
  const [datasetError, setDatasetError] = useState("");
  const [datasetRevision, setDatasetRevision] = useState(0);
  const [datasetsLoading, setDatasetsLoading] = useState(true);
  useEffect(() => {
    const c = new AbortController();
    setDatasetsLoading(true);
    setDatasetError("");
    client
      .listDatasets(c.signal)
      .then((rows) => {
        if (c.signal.aborted) return;
        setDatasets(rows);
        setDatasetId((old) =>
          rows.some((d) => d.id === old) ? old : (rows[0]?.id ?? ""),
        );
      })
      .catch((e) => {
        if (!c.signal.aborted) setDatasetError(e.message);
      })
      .finally(() => {
        if (!c.signal.aborted) setDatasetsLoading(false);
      });
    return () => c.abort();
  }, [datasetRevision]);
  async function register(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (
      saving ||
      datasetsLoading ||
      datasetError ||
      !datasets.some((d) => d.id === datasetId)
    )
      return;
    const form = event.currentTarget;
    const data = new FormData(form);
    setSaving(true);
    setNotice("");
    const input: ModelCreate = {
      name: String(data.get("name")).trim(),
      version: String(data.get("version")).trim(),
      purpose: String(data.get("purpose")).trim(),
      owner: String(data.get("owner")).trim() || null,
      language: data.get("language") === "R" ? "R" : "Python",
      dataset_id:
        data.get("dataset") === "uci_credit" ? "uci_credit" : "ulb_fraud",
    };
    try {
      const record = await client.create(input);
      setNotice(
        `Registered model ${record.name} (${record.id}). Next: attach a backend package. Selected local files have not been uploaded.`,
      );
      selectModel(record.id);
      form.reset();
      setOffset(0);
      setRevision((r) => r + 1);
    } catch (e) {
      setNotice(
        `${(e as Error).message} Refresh the inventory before retrying: a connection failure may occur after the server saves a record.`,
      );
    } finally {
      setSaving(false);
    }
  }
  return (
    <main className="live-page">
      <div className="page-heading">
        <div>
          <p className="eyebrow">CONNECTED METADATA / LOCAL BACKEND</p>
          <h1>Live model inventory</h1>
          <p>
            Records from the Python API. No illustrative metrics or simulated
            approvals.
          </p>
        </div>
        <a className="button secondary" href="/">
          Return to demo
        </a>
      </div>
      <div className="notice">
        Local development only. The backend scaffold does not implement
        authentication. Run and chat routes are connected; unavailable backend
        capabilities show explicit errors. Human review submission remains
        pending.
      </div>
      <div className="connected-layout" style={{ marginTop: 24 }}>
        <section className="panel">
          <div className="panel-title">
            <h2>Registered models</h2>
            <p>
              Lifecycle is the backend record status, not validation progress.
            </p>
          </div>
          <div className="section-content" aria-live="polite">
            {loading ? (
              <p role="status">Loading backend records…</p>
            ) : error ? (
              <div role="alert">
                <p>{error}</p>
                <button
                  className="button secondary"
                  onClick={() => setRevision((r) => r + 1)}
                >
                  Retry connection
                </button>
              </div>
            ) : (
              <>
                {rows.length === 0 ? (
                  <p>No models on this page.</p>
                ) : (
                  rows.map((row) => (
                    <button
                      className="live-record"
                      key={row.id}
                      onClick={() => selectModel(row.id)}
                    >
                      <strong>{row.name}</strong>
                      <span>
                        {row.id} · {row.language} · {row.version}
                      </span>
                      <span>
                        {row.dataset_id} · {row.lifecycle_status}
                      </span>
                    </button>
                  ))
                )}
                <div className="form-actions">
                  <button
                    className="button secondary"
                    disabled={offset === 0}
                    onClick={() => setOffset((v) => Math.max(0, v - 20))}
                  >
                    Previous
                  </button>
                  <span>Page {offset / 20 + 1}</span>
                  <button
                    className="button secondary"
                    disabled={rows.length < 20}
                    onClick={() => setOffset((v) => v + 20)}
                  >
                    Next
                  </button>
                  <button
                    className="button secondary"
                    onClick={() => setRevision((r) => r + 1)}
                  >
                    Refresh
                  </button>
                </div>
              </>
            )}
            {selectionError && <p role="alert">{selectionError}</p>}
            {selected && (
              <article className="purpose">
                <h2>{selected.name}</h2>
                <p>{selected.purpose}</p>
                <p>Owner: {selected.owner ?? "Not assigned"}</p>
                <p>Created: {selected.created_at}</p>
                <p>
                  Validation, parity, risk tier and evidence: not supplied by
                  this endpoint.
                </p>
                <ScoringPanel key={`scoring-${selected.id}`} model={selected} />
                <ModelDocuments
                  key={`documents-${selected.id}`}
                  modelId={selected.id}
                />
                <RunPanel
                  key={selected.id}
                  model={selected}
                  onRunChange={refreshRuns}
                />
                <RunHistory
                  key={`history-${selected.id}`}
                  modelId={selected.id}
                  revision={runRevision}
                />
              </article>
            )}
          </div>
        </section>
        <section className="panel">
          <div className="panel-title">
            <h2>Register model</h2>
            <p>
              Add a model to the inventory. Define its intended use, then attach
              its package. Registration does not train or upload a model.
            </p>
          </div>
          <SourceFiles />
          {datasetsLoading && <p role="status">Loading dataset catalogue…</p>}
          {datasetError && (
            <div role="alert">
              <p>{datasetError}</p>
              <button
                className="button secondary"
                onClick={() => setDatasetRevision((v) => v + 1)}
              >
                Retry dataset catalogue
              </button>
            </div>
          )}
          {!datasetsLoading && !datasetError && datasets.length === 0 && (
            <p>No datasets available for registration.</p>
          )}
          <form className="form" onSubmit={register}>
            <fieldset
              disabled={
                saving ||
                datasetsLoading ||
                !!datasetError ||
                datasets.length === 0
              }
            >
              <label>
                Name
                <input name="name" required maxLength={120} />
              </label>
              <label>
                Version
                <input
                  name="version"
                  required
                  maxLength={40}
                  defaultValue="1.0.0"
                />
              </label>
              <label>
                Language
                <select name="language">
                  <option>R</option>
                  <option>Python</option>
                </select>
              </label>
              <label>
                Dataset
                <select
                  name="dataset"
                  required
                  value={datasetId}
                  onChange={(e) => setDatasetId(e.target.value)}
                >
                  {datasets.map((d) => (
                    <option key={d.id} value={d.id}>
                      {d.name}
                    </option>
                  ))}
                </select>
              </label>
              {datasets.find((d) => d.id === datasetId) && (
                <p>
                  {datasets.find((d) => d.id === datasetId)!.target_description}{" "}
                  ·{" "}
                  {datasets
                    .find((d) => d.id === datasetId)!
                    .limitations.join("; ")}
                </p>
              )}
              <label>
                Purpose / intended use
                <textarea
                  name="purpose"
                  required
                  maxLength={1000}
                  placeholder="What does this model predict, for whom, and how will its output be used?"
                />
              </label>
              <label>
                Owner (optional)
                <input name="owner" maxLength={120} />
              </label>
              <button
                className="button primary full"
                style={{ marginTop: 20 }}
                type="submit"
              >
                {saving ? "Registering…" : "Register model"}
              </button>
            </fieldset>
            <p role="status">{notice}</p>
          </form>
        </section>
        <AgentPanel key={selected?.id ?? "general"} model={selected} />
      </div>
    </main>
  );
}

function RunHistory({
  modelId,
  revision,
}: {
  modelId: string;
  revision: number;
}) {
  const [runs, setRuns] = useState<RunRead[]>([]);
  const [offset, setOffset] = useState(0);
  const [refresh, setRefresh] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError("");
    setRuns([]);
    client
      .listRuns(modelId, offset, 20, controller.signal)
      .then((rows) => {
        if (!controller.signal.aborted) setRuns(rows);
      })
      .catch((e: Error) => {
        if (!controller.signal.aborted) setError(e.message);
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [modelId, offset, revision, refresh]);
  return (
    <section aria-label="Run history">
      <h3>Run history</h3>
      <button
        className="button secondary"
        disabled={loading}
        onClick={() => setRefresh((v) => v + 1)}
      >
        Refresh runs
      </button>
      {loading ? (
        <p role="status">Loading runs…</p>
      ) : error ? (
        <p role="alert">{error}</p>
      ) : runs.length === 0 ? (
        <p>No runs yet.</p>
      ) : (
        <ul>
          {runs.map((run) => (
            <li key={run.id}>
              <strong>
                {run.operation} · {run.status}
              </strong>
              <p>
                Run: {run.id} · Version: {run.model_version}
              </p>
              <p>
                Created: {run.created_at} · Stage: {run.stage ?? "Not started"}
              </p>
              {run.error && <p>Error: {run.error}</p>}
            </li>
          ))}
        </ul>
      )}
      <div className="form-actions">
        <button
          className="button secondary"
          disabled={loading || offset === 0}
          onClick={() => setOffset((v) => Math.max(0, v - 20))}
        >
          Previous runs
        </button>
        <span>Page {offset / 20 + 1}</span>
        <button
          className="button secondary"
          disabled={loading || !!error || runs.length < 20}
          onClick={() => setOffset((v) => v + 20)}
        >
          Next runs
        </button>
      </div>
    </section>
  );
}
