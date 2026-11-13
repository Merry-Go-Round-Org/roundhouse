import { useEffect, useRef, useState } from "react";
import { createModelClient } from "./api";
import { useAuth } from "./AuthContext";
import type { Archive, Candidate, GateZero, Mapping } from "./nativeApi";
import { pretty } from "./LiveProposals";
import "./nativeWorkflows.css";
const api = createModelClient();
const active = (a: Archive) =>
  ["queued", "running", "confirmed", "building"].includes(a.status);
export function IntakePage({ openModel }: { openModel: (id: string) => void }) {
  const { user } = useAuth();
  const key = "intake-uncertain:" + user.id;
  const [offset, setOffset] = useState(0),
    [revision, setRevision] = useState(0),
    [rows, setRows] = useState<Archive[]>([]),
    [selected, setSelected] = useState("");
  const [error, setError] = useState(""),
    [loading, setLoading] = useState(true),
    [busy, setBusy] = useState(false),
    [progress, setProgress] = useState<number | null>(null),
    [file, setFile] = useState<File | null>(null),
    [capable, setCapable] = useState(false);
  const [uncertain, setUncertain] = useState(() => {
    try {
      return sessionStorage.getItem(key) === "true";
    } catch {
      return false;
    }
  });
  const fileInput = useRef<HTMLInputElement>(null);
  const lock = useRef(false),
    life = useRef<AbortController | null>(null);
  useEffect(() => {
    const c = new AbortController();
    life.current = c;
    api.native
      .capabilities(c.signal)
      .then((v) => {
        if (!c.signal.aborted)
          setCapable(
            v.gate_0_role === "developer" &&
              !!v.harness_profiles.survey &&
              !!v.harness_profiles.intake,
          );
      })
      .catch((e) => {
        if (!c.signal.aborted) setError(e.message);
      });
    return () => c.abort();
  }, []);
  useEffect(() => {
    const c = new AbortController();
    setLoading(true);
    setRows([]);
    api.native
      .archives(offset, c.signal)
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
  }, [offset, revision]);
  function mark(value: boolean) {
    setUncertain(value);
    try {
      if (value) sessionStorage.setItem(key, "true");
      else sessionStorage.removeItem(key);
    } catch {
      /* retain in memory */
    }
  }
  async function upload() {
    if (
      lock.current ||
      uncertain ||
      !file ||
      !capable ||
      !["developer", "admin"].includes(user.role)
    )
      return;
    lock.current = true;
    setBusy(true);
    setError("");
    setProgress(null);
    mark(true);
    try {
      const a = await api.native.upload(file, life.current!.signal, (value) => {
        if (!life.current!.signal.aborted) setProgress(value);
      });
      if (!life.current!.signal.aborted) {
        mark(false);
        setSelected(a.id);
        setFile(null);
        if (fileInput.current) fileInput.current.value = "";
        setRevision((v) => v + 1);
      }
    } catch (e) {
      if (!life.current!.signal.aborted) {
        setError(
          (e as Error).message +
            " Inspect saved archives before uploading again.",
        );
        const id = (e as { archiveId?: string }).archiveId;
        if (id) setSelected(id);
        setRevision((v) => v + 1);
      }
    } finally {
      lock.current = false;
      if (!life.current!.signal.aborted) setBusy(false);
    }
  }
  return (
    <section className="native-workflows" aria-label="Archive intake">
      <h2>Upload a model archive</h2>
      <p>
        Upload a ZIP with fitted models, code and notes. The agent proposes a
        plan for you to review. Upload datasets separately on Datasets.
      </p>
      <fieldset
        disabled={busy || uncertain || !capable || !["developer", "admin"].includes(user.role)}
      >
        <legend>New archive</legend>
        <label>
          Model archive (ZIP)
          <input
            ref={fileInput}
            type="file"
            accept=".zip"
            onChange={(e) => setFile(e.target.files?.[0] ?? null)}
          />
        </label>
        <button disabled={!file} onClick={() => void upload()}>
          {busy ? "Uploading archive…" : "Upload archive"}
        </button>
      </fieldset>
      {!["developer", "admin"].includes(user.role) && (
        <p>Archive upload and Gate 0 require a developer or administrator account.</p>
      )}
      {!capable && <p>Waiting for installed survey/intake capabilities.</p>}
      {error && <p role="alert">{error}</p>}
      {busy && (
        <div role="status">
          <progress aria-label="Archive upload progress" max={100} value={progress ?? undefined} />
          <p>{progress === 100 ? "File sent. Waiting for the server to save your archive…" : progress === null ? "Sending archive…" : `Sending archive: ${progress}%`}</p>
        </div>
      )}
      {uncertain && !busy && (
        <div role="status">
          <p>
            An upload may have been saved. Review archive history before
            deliberately starting another upload.
          </p>
          <button
            disabled={busy || loading}
            onClick={() => {
              mark(false);
              setFile(null);
        if (fileInput.current) fileInput.current.value = "";
            }}
          >
            I checked saved archives; allow a new upload
          </button>
        </div>
      )}
      <h3>Saved archives</h3>
      <button
        disabled={loading || busy}
        onClick={() => {
          setError("");
          setRevision((v) => v + 1);
        }}
      >
        Refresh archives
      </button>
      {loading ? (
        <p role="status">Loading archives…</p>
      ) : (
        <ul className="native-record-list">
          {rows.map((a) => (
            <li key={a.id}>
              <button onClick={() => setSelected(a.id)}>
                Inspect archive {a.id} · {a.status} ·{" "}
                {new Date(a.created_at).toLocaleString()}
              </button>
            </li>
          ))}
        </ul>
      )}
      {!loading && !rows.length && <p>No archives on this page.</p>}
      <div className="native-toolbar">
        <button
          disabled={loading || offset === 0}
          onClick={() => setOffset((v) => Math.max(0, v - 20))}
        >
          Previous archives
        </button>
        <span>Page {offset / 20 + 1}</span>
        <button
          disabled={loading || rows.length < 20}
          onClick={() => setOffset((v) => v + 20)}
        >
          Next archives
        </button>
      </div>
      {selected && (
        <ArchiveDetail key={selected} id={selected} openModel={openModel} />
      )}
    </section>
  );
}
function ArchiveDetail({
  id,
  openModel,
}: {
  id: string;
  openModel: (id: string) => void;
}) {
  const { user } = useAuth();
  const [record, setRecord] = useState<Archive | null>(null),
    [error, setError] = useState(""),
    [busy, setBusy] = useState(false),
    [uncertain, setUncertain] = useState(false),
    [generation, setGeneration] = useState(0),
    [confirm, setConfirm] = useState(false);
  const life = useRef<AbortController | null>(null),
    lock = useRef(false);
  useEffect(() => {
    const c = new AbortController();
    life.current = c;
    setBusy(true);
    api.native
      .archive(id, c.signal)
      .then((v) => {
        if (!c.signal.aborted) setRecord(v);
      })
      .catch((e) => {
        if (!c.signal.aborted) setError(e.message);
      })
      .finally(() => {
        if (!c.signal.aborted) setBusy(false);
      });
    return () => c.abort();
  }, [id]);
  useEffect(() => {
    if (!record || !active(record) || busy || uncertain) return;
    const c = new AbortController();
    const timer = setTimeout(() => {
      api.native
        .archive(id, c.signal)
        .then((v) => {
          if (!c.signal.aborted) setRecord(v);
        })
        .catch((e) => {
          if (!c.signal.aborted) {
            setError(e.message);
            setUncertain(true);
          }
        });
    }, 3000);
    return () => {
      clearTimeout(timer);
      c.abort();
    };
  }, [record, busy, uncertain, id]);
  async function reload() {
    if (lock.current) return;
    setBusy(true);
    setRecord(null);
    setConfirm(false);
    setGeneration((v) => v + 1);
    setError("");
    try {
      const v = await api.native.archive(id, life.current!.signal);
      if (!life.current!.signal.aborted) {
        setRecord(v);
        setUncertain(false);
      }
    } catch (e) {
      if (!life.current!.signal.aborted) setError((e as Error).message);
    } finally {
      if (!life.current!.signal.aborted) setBusy(false);
    }
  }
  async function write(work: (signal: AbortSignal) => Promise<Archive>) {
    if (lock.current || busy || uncertain) return;
    lock.current = true;
    setBusy(true);
    setConfirm(false);
    setError("");
    try {
      const v = await work(life.current!.signal);
      if (!life.current!.signal.aborted) {
        setRecord(v);
        setGeneration((n) => n + 1);
      }
    } catch (e) {
      if (!life.current!.signal.aborted) {
        setError(
          (e as Error).message +
            " Reload the saved archive before another action.",
        );
        setUncertain(true);
      }
    } finally {
      lock.current = false;
      if (!life.current!.signal.aborted) setBusy(false);
    }
  }
  async function download(path: string, candidate?: string) {
    try {
      const blob = await api.native.file(
        id,
        path,
        candidate,
        life.current!.signal,
      );
      if (life.current!.signal.aborted) return;
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = path.split("/").at(-1) || "intake-file";
      a.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (e) {
      if (!life.current!.signal.aborted) setError((e as Error).message);
    }
  }
  return (
    <section aria-label="Saved archive" className="native-card">
      <h3>Archive review</h3>
      <button disabled={busy} onClick={() => void reload()}>
        Reload saved archive
      </button>
      {busy && <p role="status">Loading saved state…</p>}
      {error && <p role="alert">{error}</p>}
      {record && (
        <>
          <p>
            Status: <strong>{record.status}</strong>
          </p>
          <p role="status">{record.status === "awaiting_review"
            ? "Ready for Gate 0. A developer or administrator reviews and confirms the plan here; there is no separate Send to review step."
            : record.status === "confirmed" || record.status === "building"
              ? "Gate 0 is saved. Selected models are being registered and documentation work is being queued. This view refreshes automatically."
              : record.status === "completed"
                ? record.models.length ? "Intake is complete. Open a registered model below to follow its documentation and later reviews." : "Intake is complete. No models were registered from this plan."
                : record.status === "blocked" ? "Intake is blocked. Check the reported problems below before submitting a corrected archive."
                  : record.status === "cancelled" ? "This archive was cancelled. It cannot be approved."
                    : record.status === "failed" ? "Intake failed. Check the error below; the uploading developer can retry this saved archive."
                      : "The agent is inspecting your archive. The intake plan will appear here when ready."}</p>
          {record.error && <p role="alert">{record.error}</p>}
          {record.plan && (
            <GateZeroForm
              key={record.plan_sha256 + ":" + generation}
              archive={record}
              disabledReason={uncertain ? "Reload the saved archive to check the outcome before another action."
                : busy ? "Waiting for saved archive state…"
                  : !["developer", "admin"].includes(user.role) ? "Gate 0 requires a developer or administrator. Your current role can inspect this plan but cannot confirm it."
                    : record.status !== "awaiting_review" ? "This plan is not awaiting a Gate 0 decision." : ""}
              disabled={
                busy ||
                uncertain ||
                !["developer", "admin"].includes(user.role) ||
                record.status !== "awaiting_review"
              }
              submit={(body) =>
                write((signal) => api.native.gateZero(record, body, signal))
              }
              download={download}
            />
          )}
          {!record.plan && (
            <p>
              {active(record)
                ? "The agent is inspecting your archive. This view refreshes automatically."
                : "No intake plan is available."}
            </p>
          )}
          {record.models.length > 0 && (
            <section>
              <h4>Registered models</h4>
              {record.models.map((m) => (
                <div className="native-card" key={m.model_id}>
                  <button onClick={() => openModel(m.model_id)}>
                    {m.name} · {m.model_version}
                  </button>
                  <p>Document run: {m.document_run_id ?? "Not queued"}</p>
                  <p>
                    Reproduction:{" "}
                    {pretty(m.reproduction?.status ?? "Not available")}
                  </p>
                </div>
              ))}
            </section>
          )}
          <details>
            <summary>Original files and archive identifiers</summary>
            <p>
              {record.id} · SHA-256 {record.sha256}
            </p>
            <ul>
              {record.files.map((f) => (
                <li key={f.path}>
                  <button onClick={() => void download(f.path)}>
                    Download {f.path}
                  </button>
                </li>
              ))}
            </ul>
          </details>
          {(((user.id === record.actor_id || user.role === "admin") &&
            ["queued", "running", "awaiting_review"].includes(record.status)) ||
            (user.id === record.actor_id &&
              ["developer", "admin"].includes(user.role) &&
              record.status === "failed")) && (
            <fieldset disabled={busy || uncertain}>
              <legend>Archive action</legend>
              <label>
                <input
                  type="checkbox"
                  checked={confirm}
                  onChange={(e) => setConfirm(e.target.checked)}
                />
                I confirm{" "}
                {record.status === "failed"
                  ? "retrying this failed archive"
                  : "cancelling this archive"}
              </label>
              <button
                disabled={!confirm}
                onClick={() =>
                  void write((signal) =>
                    api.native.archiveAction(
                      id,
                      record.status === "failed" ? "retry" : "cancel",
                      signal,
                    ),
                  )
                }
              >
                {record.status === "failed"
                  ? "Retry failed archive"
                  : "Cancel archive"}
              </button>
            </fieldset>
          )}
        </>
      )}
    </section>
  );
}
function GateZeroForm({
  archive,
  disabledReason,
  disabled,
  submit,
  download,
}: {
  archive: Archive;
  disabledReason: string;
  disabled: boolean;
  submit: (body: GateZero) => Promise<void>;
  download: (path: string, candidate?: string) => Promise<void>;
}) {
  const candidates = archive.plan!.candidates;
  const [choices, setChoices] = useState<
      Record<string, "" | "accept" | "exclude">
    >({}),
    [edits, setEdits] = useState<Record<string, Record<string, unknown>>>({}),
    [mappings, setMappings] = useState<Record<string, string>>({}),
    [reason, setReason] = useState(""),
    [confirmed, setConfirmed] = useState(false),
    [error, setError] = useState("");
  function edit(id: string, key: string, value: string) {
    setConfirmed(false);
    setEdits((old) => ({ ...old, [id]: { ...old[id], [key]: value } }));
  }
  async function send() {
    if (disabled || !confirmed || blockers.length) return;
    setConfirmed(false);
    setError("");
    try {
      const body: GateZero = {
        plan_sha256: archive.plan_sha256!,
        rationale: reason.trim(),
        candidates: candidates.map((c) => {
          let mapping: Mapping | undefined;
          if (choices[c.candidate_id] === "accept" && mappings[c.candidate_id] !== undefined) {
            const value: unknown = JSON.parse(mappings[c.candidate_id]);
            if (!value || typeof value !== "object" || Array.isArray(value))
              throw new Error("Dataset mapping must be an object.");
            mapping = value as Mapping;
          }
          return {
            candidate_id: c.candidate_id,
            accept: choices[c.candidate_id] === "accept",
            manifest: edits[c.candidate_id] ?? {},
            ...(mapping ? { dataset_mapping: mapping } : {}),
          };
        }),
      };
      await submit(body);
    } catch (e) {
      setError((e as Error).message);
    }
  }
  const blockers: string[] = [];
  if (!candidates.length) blockers.push("No model candidates were found. Submit an archive containing a fitted model and its supporting files.");
  for (const c of candidates) {
    const id = c.candidate_id;
    if (!choices[id]) blockers.push(`Choose Accept into inventory or Leave out for ${id}.`);
    if (choices[id] !== "accept") continue;
    if (c.fitted_model === "training_code_only" || c.problems.length) blockers.push(`${id} cannot be accepted. Resolve its reported problems or choose Leave out.`);
    const value = (key: string) => edits[id]?.[key] ?? c.manifest[key]?.value;
    const name = value("name"), version = value("version"), language = value("language");
    if (typeof name !== "string" || !/^[A-Za-z0-9][A-Za-z0-9_.-]{0,119}$/.test(name)) blockers.push(`${id}: enter a model name of 1–120 letters, numbers, dots, underscores or hyphens, starting with a letter or number.`);
    if (typeof version !== "string" || !version.trim() || version.length > 40) blockers.push(`${id}: enter a version of 1–40 characters.`);
    if (typeof language !== "string" || !language.trim() || language.length > 10) blockers.push(`${id}: enter a language of 1–10 characters.`);
    if (mappings[id] !== undefined) {
      try {
        const m: unknown = JSON.parse(mappings[id]);
        if (!m || typeof m !== "object" || Array.isArray(m)) throw new Error();
      } catch { blockers.push(`${id}: correct the dataset mapping JSON before confirming.`); }
    }
  }
  if (!reason.trim()) blockers.push("Enter a Gate 0 rationale.");
  if (!confirmed) blockers.push("Confirm that you reviewed this exact plan.");
  return (
    <section aria-label="Gate 0 intake plan">
      <h3>Gate 0: review the intake plan</h3>
      <p>
        Review each candidate and its sources. Accept it into inventory or leave
        it out. Unknown business metadata needs your input. Unstructured archives
        do not require an existing manifest; review the proposed metadata and
        wrappers below. Candidate problems must be resolved before acceptance.
      </p>
      {candidates.map((c) => (
        <article className="native-card" key={c.candidate_id}>
          <h4>
            {pretty(c.manifest.name?.value) === "Not supplied"
              ? c.candidate_id
              : pretty(c.manifest.name?.value)}
          </h4>
          <p>
            {c.fitted_model.replaceAll("_", " ")} ·{" "}
            {c.executable
              ? "Executable in this environment"
              : "Not executable; reproduction is not assessable"}
          </p>
          {c.execution_error && <p>{c.execution_error}</p>}
          {c.problems.length > 0 && (
            <section className="notice" aria-label={`Candidate ${c.candidate_id} problems`}>
            <h4>Resolve before accepting this candidate</h4>
            <ul>
              {c.problems.map((p, i) => (
                <li key={i}>{p}</li>
              ))}
            </ul>
            <p>These are the intake agent’s reported problems for this candidate. Correct the source files and submit a corrected archive if needed.</p>
            </section>
          )}
          {c.fitted_model === "training_code_only" && <p>Training code alone cannot be registered as a fitted model. Choose Leave out, or upload the fitted model in a corrected archive.</p>}
          <fieldset disabled={disabled}>
            <legend>Candidate {c.candidate_id}</legend>
            <label>
              Include candidate {c.candidate_id}
              <select
                value={choices[c.candidate_id] ?? ""}
                onChange={(e) => {
                  setChoices((v) => ({
                    ...v,
                    [c.candidate_id]: e.target.value as "accept" | "exclude",
                  }));
                  setConfirmed(false);
                }}
              >
                <option value="">Choose for this candidate</option>
                <option
                  value="accept"
                  disabled={
                    c.fitted_model === "training_code_only" ||
                    c.problems.length > 0
                  }
                >
                  Accept into inventory
                </option>
                <option value="exclude">Leave out</option>
              </select>
            </label>
            <div className="native-metadata">
              {Object.entries({
                name: { value: null, source: "unknown" },
                version: { value: null, source: "unknown" },
                language: { value: null, source: "unknown" },
                ...c.manifest,
              } as Candidate["manifest"]).map(([key, field]) => (
                <div key={key}>
                  <label>
                    {key.replaceAll("_", " ")}
                    {["name", "version", "language"].includes(key) || typeof field.value === "string" || field.value === null ? (
                      <input
                        aria-label={`${c.candidate_id} ${key}`}
                        value={String(
                          edits[c.candidate_id]?.[key] ?? field.value ?? "",
                        )}
                        onChange={(e) =>
                          edit(c.candidate_id, key, e.target.value)
                        }
                      />
                    ) : (
                      <span className="native-value">
                        {pretty(field.value)}
                      </span>
                    )}
                  </label>
                  <small>
                    Source: {field.source}
                    {["document", "code", "proposed"].includes(field.source)
                      ? " · needs confirmation"
                      : ""}
                    {field.evidence ? " · " + field.evidence : ""}
                    {field.reason ? " · " + field.reason : ""}
                  </small>
                </div>
              ))}
            </div>
            <h4>Dataset mapping</h4>
            {c.dataset_mapping ? (
              <>
                <p>
                  {c.dataset_mapping.dataset_id} · {c.dataset_mapping.rationale}
                </p>
                <dl>
                  {Object.entries(c.dataset_mapping.feature_map).map(
                    ([from, to]) => (
                      <div key={from}>
                        <dt>{from}</dt>
                        <dd>{to}</dd>
                      </div>
                    ),
                  )}
                </dl>
                {c.dataset_mapping.unmatched_features.length > 0 && (
                  <p>
                    Unmatched: {c.dataset_mapping.unmatched_features.join(", ")}
                  </p>
                )}
              </>
            ) : (
              <p>No dataset mapping proposed.</p>
            )}
            <details>
              <summary>Correct dataset mapping (advanced)</summary>
              <label>
                Dataset mapping JSON for {c.candidate_id}
                <textarea
                  value={
                    mappings[c.candidate_id] ??
                    JSON.stringify(
                      c.dataset_mapping ?? {
                        dataset_id: "",
                        feature_map: {},
                        unmatched_features: [],
                        rationale: "",
                      },
                      null,
                      2,
                    )
                  }
                  onChange={(e) => {
                    setMappings((v) => ({
                      ...v,
                      [c.candidate_id]: e.target.value,
                    }));
                    setConfirmed(false);
                  }}
                />
              </label>
            </details>
          </fieldset>
          <details>
            <summary>Candidate files and scoring evidence</summary>
            <ul>
              {c.files.map((path) => (
                <li key={path}>
                  <button onClick={() => void download(path)}>
                    Download {path}
                  </button>
                </li>
              ))}
            </ul>
            <p>Entry point: {c.scoring.entry_point ?? "Not found"}</p>
            <p>
              Agent self-check: {c.scoring.self_check ?? "Not supplied"}. This
              is not official parity.
            </p>
            {c.scoring.wrapper && (
              <>
                <p>
                  Agent-written wrapper · SHA-256 {c.scoring.wrapper_sha256}
                </p>
                <button
                  onClick={() => void download("score.py", c.candidate_id)}
                >
                  Download scoring wrapper
                </button>
              </>
            )}
          </details>
        </article>
      ))}
      <details open={archive.plan!.unassigned_files.length > 0}>
        <summary>
          Unassigned files ({archive.plan!.unassigned_files.length})
        </summary>
        <ul>
          {archive.plan!.unassigned_files.map((path) => (
            <li key={path}>{path}</li>
          ))}
        </ul>
      </details>
      {disabledReason && <p role="status">{disabledReason}</p>}
      {archive.status === "awaiting_review" && (
        <fieldset disabled={disabled}>
          <legend>Confirm this plan</legend>
          <p>Confirming Gate 0 registers the candidates you accept and starts their documentation work. It does not approve the rebuilt model or its later governance reviews.</p>
          {blockers.length > 0 && <div role="status" aria-label="Gate 0 requirements"><ul>{blockers.map(text => <li key={text}>{text}</li>)}</ul></div>}
          <label>
            Gate 0 rationale
            <textarea
              value={reason}
              maxLength={2000}
              onChange={(e) => {
                setReason(e.target.value);
                setConfirmed(false);
              }}
            />
          </label>
          <label>
            <input
              type="checkbox"
              checked={confirmed}
              onChange={(e) => setConfirmed(e.target.checked)}
            />
            I reviewed the candidate files, metadata and any agent-written
            wrapper for this exact plan
          </label>
          <button
            disabled={blockers.length > 0}
            onClick={() => void send()}
          >
            Confirm Gate 0 plan
          </button>
        </fieldset>
      )}
      {error && <p role="alert">{error}</p>}
    </section>
  );
}
