import { ChevronRight } from "lucide-react";
import type { ReactNode } from "react";

/**
 * Presentation-only inventory rows shared by Demo and Live.
 * Adapters supply truthful data; this component never imports demo fixtures,
 * requests Backend APIs, starts jobs or records decisions.
 */
export interface InventoryRow {
  id: string;
  name: string;
  version: string;
  language: "R" | "Python";
  datasetLabel: string;
  category: "Credit" | "Fraud" | "Survival";
  statusLabel: string;
  owner: string | null;
  statusTone?: "green" | "amber" | "red" | "not_assessable";
}

export function InventoryTable({
  rows,
  statusHeading,
  missingOwnerLabel = "Unassigned",
  onOpen,
  selection,
}: {
  rows: readonly InventoryRow[];
  statusHeading: string;
  missingOwnerLabel?: string;
  onOpen: (id: string) => void;
  selection?: { ids: ReadonlySet<string>; toggle: (id: string) => void };
}): ReactNode {
  return (
    <div className="table-scroll shared-inventory-table">
      <table aria-label="Model inventory">
        <thead>
          <tr>
            {selection && <th scope="col">Select</th>}
            <th scope="col">Model / version</th>
            <th scope="col">Type</th>
            <th scope="col">{statusHeading}</th>
            <th scope="col">Owner</th>
            <th scope="col"><span className="sr-only">Open model</span></th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.id}>
              {selection && <td><input type="checkbox" aria-label={`Select ${row.name} (${row.id}), version ${row.version}`} checked={selection.ids.has(row.id)} onChange={() => selection.toggle(row.id)} /></td>}
              <td>
                <button
                  type="button"
                  className="model-link"
                  aria-label={row.name}
                  onClick={() => onOpen(row.id)}
                >
                  <span className={"model-icon" + (row.category === "Fraud" ? " purple" : "")}>
                    {row.language === "R" ? "R" : "Py"}
                  </span>
                  <span>
                    <strong>{row.name}</strong>
                    <small>
                      {row.id} <span>·</span> {row.version}
                      <span>·</span> {row.datasetLabel}
                    </small>
                  </span>
                </button>
              </td>
              <td><span className="type-label">{row.category}</span></td>
              <td>
                <span className={"badge " + (row.statusTone ?? "neutral")}>
                  <span className="badge-dot" />
                  {row.statusLabel}
                </span>
              </td>
              <td className="owner">{row.owner ?? missingOwnerLabel}</td>
              <td>
                <button
                  type="button"
                  className="icon-button"
                  aria-label={"Open " + row.name}
                  onClick={() => onOpen(row.id)}
                >
                  <ChevronRight size={18} />
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
