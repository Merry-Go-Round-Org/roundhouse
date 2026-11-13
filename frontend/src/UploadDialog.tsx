import { UploadProblems } from "./UploadProblems";
import { useEffect, useRef, useState, type FormEvent } from "react";
import { createModelClient } from "./api";
import type { UploadState, UploadCheck } from "./uploads";
const api = createModelClient();
export function UploadDialog({
  kind,
  close,
  complete,
  enabled,
}: {
  kind: "model" | "dataset";
  close: () => void;
  complete: (id: string) => void;
  enabled: boolean;
}) {
  const dialog = useRef<HTMLDialogElement>(null),
    controller = useRef<AbortController | null>(null),
    lock = useRef(false);
  const [busy, setBusy] = useState(false),
    [progress, setProgress] = useState<number | null>(null),
    [error, setError] = useState("");
  const [state, setState] = useState<UploadState | null>(null),
    [dataset, setDataset] = useState<{
      id: string;
      checks: UploadCheck[];
    } | null>(null),
    [attempted, setAttempted] = useState(false);
  const [rejected, setRejected] = useState(false);
  const form = useRef<HTMLFormElement>(null);
  const [files, setFiles] = useState<string[]>([]);
  useEffect(() => {
    dialog.current?.showModal();
    return () => controller.current?.abort();
  }, []);
  useEffect(() => {
    if (!state || !["queued", "running"].includes(state.status)) return;
    const c = new AbortController();
    const timer = setTimeout(() => {
      api
        .getUpload(state.id, c.signal)
        .then((s) => {
          if (!c.signal.aborted) {
            setState(s);
            setError("");
          }
        })
        .catch((e) => {
          if (!c.signal.aborted) setError(e.message);
        });
    }, 2000);
    return () => {
      c.abort();
      clearTimeout(timer);
    };
  }, [state]);
  async function submit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    if (!enabled || lock.current || attempted) return;
    const data = new FormData(e.currentTarget);
    const bundle = data.get("bundle");
    if (kind === "model" && (!(bundle instanceof File) || !bundle.size || !bundle.name.toLowerCase().endsWith(".zip"))) {
      setError("Choose a non-empty model ZIP bundle.");
      return;
    }
    const selectedSplit = data.get("split");
    const split = selectedSplit instanceof File && selectedSplit.name ? selectedSplit : null;
    if (kind === "dataset") {
      if (![data.get("source"), data.get("descriptor")].every(f => f instanceof File && f.size > 0)) {
        setError("Choose a non-empty source dataset and dataset.json.");
        return;
      }
      if (split && !split.size) {
        setError("The selected split file is empty. Choose a non-empty file or clear the selection.");
        return;
      }
    }
    lock.current = true;
    setBusy(true);
    setAttempted(true);
    setError("");
    setProgress(null);
    controller.current = new AbortController();
    const signal = controller.current.signal;
    try {
      const progress = (n: number | null) => {
        if (!signal.aborted) setProgress(n);
      };
      if (kind === "model") {
        const s = await api.uploadModel(
          data.get("bundle") as File,
          progress,
          signal,
        );
        if (!signal.aborted) setState(s);
      } else {
        const id = await api.uploadDataset(
          data.get("source") as File,
          data.get("descriptor") as File,
          split,
          progress,
          signal,
        );
        if (!signal.aborted) setDataset(id);
      }
    } catch (e) {
      const safeToReplace = [413, 415, 422].includes((e as { status?: number }).status ?? 0);
      setRejected(safeToReplace);
      setError(
        (e as Error).message +
          (safeToReplace ? " Choose corrected files to try again." : " Inspect inventory/catalogue before starting another upload; server processing may continue."),
      );
    } finally {
      lock.current = false;
      setBusy(false);
    }
  }
  return (
    <dialog
      className="upload-dialog"
      ref={dialog}
      aria-labelledby="upload-title"
      onCancel={(e) => {
        // File picker cancellation bubbles; only dismiss for the dialog itself.
        if (e.target !== e.currentTarget) return;
        e.preventDefault();
        close();
      }}
    >
      <div className="modal-head">
        <h2 id="upload-title">Upload {kind}</h2>
        <button type="button" className="button secondary" onClick={close}>
          Close
        </button>
      </div>
      <form
        ref={form}
        className="form"
        onSubmit={submit}
        onChange={(e) => {
          const d = new FormData(e.currentTarget);
          setFiles(
            [...d.values()]
              .filter((v): v is File => v instanceof File && !!v.name)
              .map((f) => `${f.name} (${f.size.toLocaleString()} bytes)`),
          );
        }}
      >
        {!enabled && (
          <p className="notice">
            Uploads are disabled in this workspace.
          </p>
        )}
        <fieldset disabled={attempted}>
          {kind === "model" ? (
            <>
              <label>
                Model ZIP bundle
                <input type="file" name="bundle" accept=".zip" required />
              </label>
              <p>
                Choose a prepared model bundle. Metadata is read from its manifest.
              </p>
            </>
          ) : (
            <>
              <label>
                Source dataset
                <input
                  type="file"
                  name="source"
                  accept=".csv,.xls,.xlsx"
                  required
                />
              </label>
              <label>
                Dataset descriptor (dataset.json)
                <input type="file" name="descriptor" accept=".json" required />
              </label>
              <label>
                Split File (Optional)
                <input type="file" name="split" accept=".csv" />
              </label>
              <a
                className="split-policy-link"
                href="/f1-split-policy.html"
                target="_blank"
                rel="noopener noreferrer"
                aria-label="Read F1 Split Policy (opens in a new tab)"
              >
                F1 Split Policy <span aria-hidden="true">↗</span>
              </a>
              <p>
                Choose the original data and dataset.json. For verified UCI/ULB sources, leaving the split blank reuses the frozen membership. New-dataset automatic generation is planned but not enabled yet.
              </p>
            </>
          )}
        </fieldset>
        {files.length > 0 && (
          <details className="help-details"><summary>Selected files ({files.length})</summary><ul aria-label="Selected files">
            {files.map((f) => (
              <li key={f}>{f}</li>
            ))}
          </ul></details>
        )}
        <button className="button primary" disabled={!enabled || attempted}>
          {kind === "model" ? "Upload and check model" : "Add dataset"}
        </button>
        {busy && (
          <>
            <progress
              aria-label="File transfer"
              max={100}
              value={progress ?? undefined}
            />
            <p role="status">
              {progress === null
                ? "Transferring files…"
                : `${progress}% transferred. Waiting for backend processing.`}
            </p>
            <button type="button" onClick={() => controller.current?.abort()}>
              Stop local transfer
            </button>
          </>
        )}
        {error && <p role="alert">{error}</p>}
        {!busy && attempted && (rejected || (state?.status === "failed" && !state.model_id)) && (
          <button type="button" className="button secondary" onClick={() => {
            if (lock.current) return;
            controller.current?.abort();
            form.current?.reset();
            setState(null);
            setDataset(null);
            setFiles([]);
            setError("");
            setProgress(null);
            setRejected(false);
            setAttempted(false);
          }}>
            {kind === "model" ? "Choose another model ZIP" : "Choose corrected dataset files"}
          </button>
        )}
        {state && (
          <section aria-label="Upload checks">
            <h3>
              Upload {state.id}: {state.status}
            </h3>
            <UploadProblems upload={state} />
            {state.files.length > 0 && (
              <details>
                <summary>Bundle files reported by backend</summary>
                <ul>
                  {state.files.map((f) => (
                    <li key={f}>{f}</li>
                  ))}
                </ul>
              </details>
            )}
            {state.checks.length === 0 ? (
              <p>No checks reported yet.</p>
            ) : (
              <ul>
                {state.checks.map((c, i) => (
                  <li key={i}>
                    <strong>
                      {c.name}: {c.status}
                    </strong>{" "}
                    · {c.blocking ? "Blocking" : "Advisory"}
                    <p>{c.reason}</p>
                  </li>
                ))}
              </ul>
            )}
            {state.error && <p role="alert">{state.error}</p>}
            {error && ["queued", "running"].includes(state.status) && (
              <button type="button" onClick={() => setState({ ...state })}>
                Retry status read
              </button>
            )}
            {state.status === "completed" && state.model_id && (
              <button
                className="button primary"
                type="button"
                onClick={() => complete(state.model_id!)}
              >
                Open in inventory
              </button>
            )}
          </section>
        )}
        {dataset && (
          <>
            <p role="status">
              Dataset {dataset.id} uploaded. Review its checks below.
            </p>
            <details className="help-details" open={dataset.checks.some(c => c.status === "failed")}><summary>Upload checks ({dataset.checks.length})</summary><ul>
              {dataset.checks.map((c, i) => (
                <li key={i}>
                  {c.name}: {c.status} · {c.reason}
                </li>
              ))}
            </ul></details>
            <button
              type="button"
              className="button primary"
              onClick={() => complete(dataset.id)}
            >
              Refresh datasets
            </button>
          </>
        )}
        {attempted && (
          <p>
            Closing this window does not cancel processing.
          </p>
        )}
      </form>
    </dialog>
  );
}
