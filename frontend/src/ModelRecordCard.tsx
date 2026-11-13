import type { ReactNode } from "react";

/** A provenance-aware model metadata card, with no inferred risk or approval. */
export type ModelFact = {
  label: string;
  value?: string | null;
};

export function ModelRecordCard({
  title,
  subtitle,
  facts,
  mode,
  icon,
  purpose,
}: {
  title: string;
  subtitle: string;
  facts: readonly ModelFact[];
  mode: "demo" | "live";
  icon?: ReactNode;
  purpose?: string;
}) {
  return (
    <section className="panel model-record-card" aria-label={title}>
      <div className="panel-title">
        <div>
          {icon}
          <h3>{title}</h3>
        </div>
        <p>{subtitle}</p>
      </div>
      <dl className="record-grid">
        {facts.map((fact) => (
          <div key={fact.label}>
            <dt>{fact.label}</dt>
            <dd>
              {fact.value ?? (mode === "live" ? "Not supplied by API" : "Not supplied in demo")}
            </dd>
          </div>
        ))}
      </dl>
      {purpose !== undefined && (
        <div className="purpose">
          <h3>Intended use</h3>
          <p>{purpose}</p>
        </div>
      )}
      <p className="model-record-provenance">
        {mode === "demo"
          ? "Illustrative record only; these fields are not Backend evidence."
          : "Source: existing inventory API; original upload provenance is unavailable. Lifecycle is not approval."}
      </p>
    </section>
  );
}
