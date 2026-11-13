import type { DatasetRead } from "./api";

const unavailable = "Not supplied by API";
const rate = (value: number | null | undefined) =>
  value === null || value === undefined ? unavailable : (value * 100).toFixed(2) + "%";
const partitions = (d: DatasetRead) => {
  const counts = d.statistics?.partition_counts;
  return counts
    ? `Train ${counts.train.toLocaleString()} · Validation ${counts.validation.toLocaleString()} · Test ${counts.test.toLocaleString()}`
    : unavailable;
};
const sourceInfo = (d: DatasetRead) => d.verified_upload === true
  ? "Source: Backend-verified dataset upload and immutable version"
  : "Source: legacy dataset catalogue (no uploaded version evidence)";

export function DatasetsView({
  datasets,
  selectedId,
  select,
  findModels,
  selection,
}: {
  datasets: DatasetRead[];
  selectedId: string | null;
  select: (id: string | null) => void;
  findModels: (id: DatasetRead["id"]) => void;
  selection?: { ids: ReadonlySet<string>; toggle: (id: string) => void };
}) {
  const selected = datasets.find((d) => d.id === selectedId);
  if (selectedId && !selected)
    return (
      <section>
        <p role="alert">This dataset is not in the current catalogue.</p>
        <button className="button secondary" onClick={() => select(null)}>Back to datasets</button>
      </section>
    );
  if (selected) {
    const d = selected;
    const desc = d.descriptor;
    const details = [
      ["Version", d.version ?? unavailable],
      ["SHA-256", d.sha256 ?? unavailable],
      ["Rows", d.statistics?.row_count.toLocaleString() ?? unavailable],
      ["Positive rate", rate(d.statistics?.positive_rate)],
      ["Train / validation / test counts", partitions(d)],
      ["Partition hashes", d.statistics?.partition_sha256
        ? Object.entries(d.statistics.partition_sha256).map(([k, value]) => k + ": " + value).join(" · ")
        : unavailable],
      ["Split file SHA-256", d.split_sha256 ?? unavailable],
      ["Source URL / licence", typeof desc?.source_url === "string" && typeof desc.license === "string"
        ? desc.source_url + " · " + desc.license : unavailable],
      ["Uploaded version ID", d.version_id ?? unavailable],
    ];
    return (
      <section aria-label="Dataset description" className="dataset-evidence">
        <button className="button secondary" onClick={() => select(null)}>Back to datasets</button>
        <h2>{d.name}</h2>
        <p>
          {d.verified_upload === true
            ? "Verified uploaded dataset version. Values below come from Backend-ingested evidence, not browser estimates."
            : "This is a catalogue description, not a verified uploaded dataset version."}
        </p>
        <section className="lifecycle-card">
          <h3>Descriptor</h3>
          <dl className="record-fields">
            {Object.entries({
              "Dataset ID": d.id,
              "Source filename": d.filename,
              "Target description": d.target_description,
            }).map(([k, v]) => (
              <div key={k}>
                <dt>{k}</dt>
                <dd>{v}<small className="field-source">{sourceInfo(d)}</small></dd>
              </div>
            ))}
          </dl>
        </section>
        <section className="lifecycle-card">
          <h3>Version and partitions</h3>
          <dl className="record-fields">
            {details.map(([k, v]) => (
              <div key={k}>
                <dt>{k}</dt>
                <dd>{v}<small className="field-source">{v === unavailable ? "Source unavailable" : sourceInfo(d)}</small></dd>
              </div>
            ))}
          </dl>
        </section>
        <section className="lifecycle-card">
          <h3>Limitations</h3>
          {d.limitations.length ? (
            <ul>{d.limitations.map((l, i) => <li key={i}>{l}</li>)}</ul>
          ) : <p>No limitations supplied by the catalogue.</p>}
        </section>
        <section className="lifecycle-card">
          <h3>Models using this dataset</h3>
          <p>
            {d.model_count === undefined
              ? "Linked model count is not supplied. Open Inventory with this dataset filter to inspect one fetched page at a time."
              : `${d.model_count} linked model(s) reported by the Backend. Inventory search still filters one fetched page at a time.`}
          </p>
          <button className="button secondary" onClick={() => findModels(d.id)}>
            Find models using this dataset
          </button>
        </section>
      </section>
    );
  }
  return (
    <section aria-label="Dataset catalogue">
      <h2>Dataset catalogue</h2>
      {!datasets.length ? (
        <p>No datasets returned by the backend.</p>
      ) : (
        <div className="inventory-scroll dataset-evidence">
          <table>
            <thead>
              <tr>
                {selection && <th scope="col">Select</th>}
                {["Dataset", "Source filename", "Version", "Rows", "Positive rate", "Partitions", "SHA-256", "Linked models"].map((h) => (
                  <th key={h}>{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {datasets.map((d) => (
                <tr key={d.version_id ?? d.id}>
                  {selection && <td><input type="checkbox" aria-label={`Select dataset ${d.name} (${d.version_id ?? d.id})`}
                    disabled={!d.version_id || d.verified_upload !== true} checked={!!d.version_id && selection.ids.has(d.version_id)}
                    onChange={() => { if (d.version_id) selection.toggle(d.version_id); }} />
                    {(!d.version_id || d.verified_upload !== true) && <small>Catalogue fixture — retained</small>}</td>}
                  <td>
                    <h3>{d.name}</h3>
                    <button onClick={() => select(d.id)}>View {d.name}</button>
                  </td>
                  <td>{d.filename}</td>
                  <td>{d.version ?? "Not supplied"}</td>
                  <td>{d.statistics?.row_count.toLocaleString() ?? "Not supplied"}</td>
                  <td>{d.statistics ? rate(d.statistics.positive_rate) : "Not supplied"}</td>
                  <td>{d.statistics ? partitions(d) : "Not supplied"}</td>
                  <td>{d.sha256 ?? "Not supplied"}</td>
                  <td>{d.model_count ?? "Not supplied"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
