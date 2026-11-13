import { useEffect, useRef, useState, type ReactNode } from "react";
import { createModelClient } from "./api";
import { sourcePath } from "./modelFiles";
const api = createModelClient();
export function SourceCitation({ modelId, href, children }: { modelId: string; href: string; children: ReactNode }) {
  const [file, setFile] = useState<{text: string; hash: string} | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const request = useRef<AbortController | null>(null);
  useEffect(() => () => request.current?.abort(), []);
  async function open() {
    if (request.current) return;
    const path = sourcePath(href);
    if (!path) return;
    const c = new AbortController(); request.current = c;
    setBusy(true); setError(""); setFile(null);
    try {
      const files = await api.listModelFiles(modelId, c.signal);
      const found = files.find(f => f.path === path);
      if (!found) throw new Error("This citation is not in the authorized model file list.");
      const text = await api.getModelFile(modelId, found, c.signal);
      if (!c.signal.aborted) setFile({text, hash:found.sha256});
    } catch (e) { if (!c.signal.aborted) setError((e as Error).message); }
    finally { request.current = null; if (!c.signal.aborted) setBusy(false); }
  }
  return <span className="source-citation">
    <button type="button" disabled={busy} onClick={() => void open()}>{children} (open source)</button>
    {error && <span role="alert">{error}</span>}
    {file && <span className="source-citation-preview">
      <span>{sourcePath(href)} · Backend SHA-256: {file.hash}. Registered package source; association with this report version is not supplied. Entire file shown.</span>
      <button type="button" onClick={() => setFile(null)}>Close source preview</button>
      <code>{file.text}</code>
    </span>}
  </span>;
}
