import { useState } from "react";
import type { DatasetRead } from "./api";
import { DatasetsView } from "./DatasetsView";
import type { DeletionSelection } from "./deletion";

export function DatasetSelection({ datasets, selectedId, select, findModels, canDelete, requestDeletion }: {
  datasets: DatasetRead[]; selectedId: string | null; select: (id: string | null) => void;
  findModels: (id: DatasetRead["id"]) => void; canDelete: boolean; requestDeletion: (selection: DeletionSelection) => void;
}) {
  const [query, setQuery] = useState(""), [page, setPage] = useState(0);
  const [ids, setIds] = useState<Set<string>>(new Set()), [review, setReview] = useState(false);
  const filtered = datasets.filter(d => `${d.id} ${d.name} ${d.version ?? ""}`.toLowerCase().includes(query.trim().toLowerCase()));
  const pages = Math.max(1, Math.ceil(filtered.length / 20)), current = Math.min(page, pages - 1);
  const visible = filtered.slice(current * 20, current * 20 + 20);
  const eligible = visible.filter(d => d.verified_upload === true && d.version_id);
  const selected = eligible.filter(d => ids.has(d.version_id!));
  const all = eligible.length > 0 && selected.length === eligible.length;
  function clear() { setIds(new Set()); setReview(false); }
  function open(id: string | null) { clear(); select(id); }
  if (selectedId) return <DatasetsView datasets={datasets} selectedId={selectedId} select={open} findModels={findModels} />;
  return <>
    <label className="inventory-search">Search datasets<input value={query} onChange={e => { setQuery(e.target.value); setPage(0); clear(); }} /></label>
    <section aria-label="Dataset selection" className="model-selection">
      <div className="form-actions">
        <label className="filter-checkbox"><input type="checkbox" checked={all} disabled={!eligible.length}
          ref={node => { if (node) node.indeterminate = selected.length > 0 && !all; }}
          onChange={() => { setIds(all ? new Set() : new Set(eligible.map(d => d.version_id!))); setReview(false); }} />Select visible datasets</label>
        <span role="status">{selected.length} datasets selected</span>
        <button disabled={!selected.length} onClick={clear}>Clear selection</button>
        <button disabled={!selected.length} aria-expanded={review} onClick={() => setReview(v => !v)}>Review selection</button>
        <button disabled={!selected.length || !canDelete} onClick={() => requestDeletion({ kind: "datasets", items: selected.map(d => ({ id: d.version_id!, name: d.name, version: d.version ?? "Unknown" })) })}>Delete selected datasets</button>
      </div>
      <p className="page-description">Select uploaded versions on this page only. Referenced versions and catalogue fixtures are protected. { !canDelete && "Deletion requires a developer or administrator account." }</p>
      {review && <section aria-label="Selected datasets"><h3>Selected dataset versions</h3><ul>{selected.map(d => <li key={d.version_id}>{d.name} · version {d.version} · <code>{d.version_id}</code></li>)}</ul></section>}
    </section>
    <DatasetsView datasets={visible} selectedId={null} select={open} findModels={findModels} selection={{ ids, toggle: id => {
      const next = new Set(ids); if (next.has(id)) next.delete(id); else next.add(id); setIds(next); setReview(false);
    } }} />
    <nav className="pagination" aria-label="Dataset pages">
      <button disabled={current === 0} onClick={() => { setPage(current - 1); clear(); }}>Previous datasets</button>
      <span>Page {current + 1} of {pages} · {filtered.length} matching datasets</span>
      <button disabled={current + 1 >= pages} onClick={() => { setPage(current + 1); clear(); }}>Next datasets</button>
    </nav>
    <p className="page-description">The catalogue supplies the latest uploaded version for each dataset. Older versions are not listed here.</p>
  </>;
}
