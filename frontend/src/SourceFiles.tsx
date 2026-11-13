import { useState } from "react";
export function SourceFiles() {
  const [model, setModel] = useState<File | null>(null);
  const [document, setDocument] = useState<File | null>(null);
  return (
    <details className="source-files">
      <summary>Select model and existing documentation files</summary>
      <p>
        Local selection only. File upload is not available on the backend yet.
        These files are not sent with Register model, are not available to the
        agent, and are cleared on refresh.
      </p>
      <label>
        Model file
        <input
          type="file"
          onChange={(e) => setModel(e.target.files?.[0] ?? null)}
        />
      </label>
      {model && (
        <p>
          Selected locally: {model.name} · {model.size.toLocaleString()} bytes
        </p>
      )}
      <label>
        Existing documentation file
        <input
          type="file"
          accept=".md,.txt,.pdf,.docx"
          onChange={(e) => setDocument(e.target.files?.[0] ?? null)}
        />
      </label>
      {document && (
        <p>
          Selected locally: {document.name} · {document.size.toLocaleString()}{" "}
          bytes
        </p>
      )}
      <button className="button secondary" type="button" disabled>
        Upload files — awaiting backend support
      </button>
      <p>
        For the current demo, register metadata then attach an existing backend
        package by name. Dataset selection uses the catalogue; dataset upload is
        not supported.
      </p>
    </details>
  );
}
