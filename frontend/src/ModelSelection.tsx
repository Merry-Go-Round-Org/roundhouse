import { useState } from "react";
import { InventoryTable, type InventoryRow } from "./InventoryTable";
import type { DeletionSelection } from "./deletion";

/** Page-bounded selection; deletion always uses the server preview workflow. */
export function ModelSelection({ rows, onOpen, canDelete, requestDeletion }: { rows: readonly InventoryRow[]; onOpen: (id: string) => void; canDelete: boolean; requestDeletion: (selection: DeletionSelection) => void }) {
  const [ids, setIds] = useState<Set<string>>(new Set());
  const [preview, setPreview] = useState(false);
  const selected = rows.filter(row => ids.has(row.id));
  const all = rows.length > 0 && selected.length === rows.length;
  function change(next: Set<string>) { setIds(next); setPreview(false); }
  return <>
    <section className="model-selection" aria-label="Model selection">
      <div className="form-actions">
        <label className="filter-checkbox"><input type="checkbox" checked={all}
          ref={node => { if (node) node.indeterminate = selected.length > 0 && !all; }}
          disabled={!rows.length} onChange={() => change(all ? new Set() : new Set(rows.map(row => row.id)))} />Select visible models</label>
        <span role="status">{selected.length} selected</span>
        <button type="button" disabled={!selected.length} onClick={() => change(new Set())}>Clear selection</button>
        <button type="button" disabled={!selected.length} aria-expanded={preview} onClick={() => setPreview(value => !value)}>Review selection</button>
        <button type="button" disabled={!selected.length || !canDelete} onClick={() => requestDeletion({ kind: "models", items: selected.map(({ id, name, version }) => ({ id, name, version })) })}>Delete selected</button>
      </div>
      <p className="page-description">{canDelete ? "Deletion requires an impact review and confirmation. " : "Deletion requires a developer or administrator account. "}Selection clears when filters, pages or saved records are refreshed.</p>
      {preview && <section aria-label="Selected models" className="lifecycle-card">
        <h3>Selected models ({selected.length})</h3>
        <ul>{selected.map(row => <li key={row.id}>{row.name} · version {row.version} · <code>{row.id}</code></li>)}</ul>
        <p>This is a selection preview. Use Delete selected to check dependencies before confirming deletion.</p>
        <button type="button" onClick={() => setPreview(false)}>Close selection preview</button>
      </section>}
    </section>
    <InventoryTable rows={rows} statusHeading="Lifecycle (not approval)" missingOwnerLabel="Not supplied" onOpen={onOpen}
      selection={{ ids, toggle: id => { const next = new Set(ids); if (next.has(id)) next.delete(id); else next.add(id); change(next); } }} />
  </>;
}
