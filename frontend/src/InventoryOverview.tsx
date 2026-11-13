import type { ModelRecord } from "./api";
import { ModelRecordCard } from "./ModelRecordCard";
import { liveModelFacts } from "./modelDetailAdapters";
import { intakeField, intakeSource, inventoryOwner } from "./inventoryProvenance";

type Field = { label: string; value?: string | null; source?: string };

function display(value: unknown): string | null {
  if (value === null || value === undefined) return null;
  if (typeof value === "string") return value.trim() || null;
  if (typeof value === "number" && Number.isFinite(value)) return String(value);
  if (typeof value === "boolean") return value ? "Yes" : "No";
  if (Array.isArray(value)) {
    const items = value.filter((v): v is string => typeof v === "string");
    return items.length === value.length ? items.join(", ") || null : null;
  }
  return null;
}

export function InventoryOverview({
  model,
  openDataset,
}: {
  model: ModelRecord;
  openDataset: () => void;
}) {
  const inventory = model.inventory;
  const manifest = inventory?.manifest;
  const provenance = inventory?.provenance;
  const owner = inventoryOwner(model);
  const synthetic = model.dataset_id === null || inventory?.synthetic_demo === true;

  function fromManifest(label: string, key: string): Field {
    const intakeKey = key === "feature_order" ? "features" : key;
    const intake = intakeField(model, intakeKey);
    if (intake) return { label, value: display(intake.value), source: intakeSource(model, intakeKey) };
    const supplied = provenance?.[key];
    const value = display(supplied ? supplied.value : manifest?.[key]);
    return {
      label,
      value,
      source: supplied?.source === "upload"
        ? "Source: verified uploaded manifest"
        : supplied?.source === "not_provided"
          ? "Not provided in upload"
          : undefined,
    };
  }
  const datasetId: Field = synthetic
    ? { label: "Dataset ID", value: "Not applicable (synthetic survival demo)", source: "Backend applicability" }
    : { label: "Dataset ID", value: model.dataset_id, source: "Source: existing model API" };
  const approval = inventory?.approval;
  const approvalDecision = approval && typeof approval.decision === "string"
    ? approval.decision : null;
  const reviewer = approval && typeof approval.reviewer_id === "string"
    ? approval.reviewer_id : null;
  const approvalConditions = approval && Array.isArray(approval.conditions)
    ? display(approval.conditions) : null;
  const lastMonitoring = inventory?.monitoring;
  const monitorSummary = lastMonitoring && typeof lastMonitoring === "object"
    ? JSON.stringify(lastMonitoring) : null;
  const validationSummary = inventory?.last_validation_date
    ? "Report recorded; inspect Validation for evidence" : null;
  const groups: { title: string; fields: Field[] }[] = [
    {
      title: "Purpose and ownership",
      fields: [
        { label: "Purpose", value: model.purpose, source: intakeField(model, "purpose")?.value === model.purpose ? intakeSource(model, "purpose") : "Source: existing model API" },
        { label: "Owner", value: owner.value, source: owner.source },
        ...(owner.conflict ? [{ label: "Owner discrepancy", value: owner.conflict, source: "Current model API compared with saved Gate 0 confirmation" }] : []),
        fromManifest("Intended use", "intended_use"),
        fromManifest("Target definition / population", "target"),
        fromManifest("Developers", "developers"),
      ],
    },
    {
      title: "Data and reproducibility",
      fields: [
        datasetId,
        { label: "Dataset version / hash", value: inventory?.dataset_version_id ?? undefined,
          source: inventory?.dataset_version_id ? "Source: uploaded dataset version" : undefined },
        fromManifest("Features / preprocessing", "feature_order"),
        fromManifest("Split provenance", "split_sha256"),
        { label: "Source / model hashes", value: inventory?.bundle_sha256,
          source: inventory?.bundle_sha256 ? "Source: uploaded bundle SHA-256" : undefined },
      ],
    },
    {
      title: "Risk and limitations",
      fields: [
        { label: "Risk tier / rationale", value: [
          display(manifest?.risk_tier), display(manifest?.tier_rationale),
        ].filter(Boolean).join(" · ") || null,
          source: intakeSource(model, "risk_tier") ?? (provenance?.risk_tier?.source === "upload" ? "Source: verified uploaded manifest" : undefined) },
        fromManifest("Assumptions / limitations", "limitations"),
        fromManifest("Use restrictions", "use_restrictions"),
        { label: "Workflow applicability", value: inventory?.applicability
          ? Object.entries(inventory.applicability).map(([key, value]) => key + ": " + value).join(" · ")
          : null, source: inventory?.applicability ? "Source: Backend applicability" : undefined },
      ],
    },
    {
      title: "Validation and findings",
      fields: [
        { label: "Last validation / status", value: inventory?.last_validation_date
          ? inventory.last_validation_date + " · " + validationSummary : null,
          source: inventory?.last_validation_date ? "Source: persisted validation report" : undefined },
        { label: "Frequency / next due", value: inventory?.validation_frequency ?? null,
          source: inventory?.validation_frequency ? "Source: uploaded manifest" : undefined },
        { label: "Open findings / owners / due dates", value: inventory?.open_findings !== undefined
          ? String(inventory.open_findings) + " open findings (owners/dates unavailable)" : null,
          source: inventory?.open_findings !== undefined ? "Source: backend findings count" : undefined },
      ],
    },
    {
      title: "Approval and monitoring",
      fields: [
        { label: "Approval / reviewer / conditions",
          value: approvalDecision ? [approvalDecision, reviewer, approvalConditions].filter(Boolean).join(" · ") : null,
          source: approvalDecision ? "Source: persisted review record; inspect human decision" : undefined },
        { label: "Latest PSI / breach status", value: monitorSummary,
          source: monitorSummary ? "Source: persisted Backend monitoring payload" : undefined },
      ],
    },
  ];
  return (
    <section aria-label="Inventory record" className="overview-card">
      <h2>Inventory record</h2>
      <p className="page-description">Model details and current review status. Lifecycle is not approval.</p>
      {synthetic && (
        <p className="notice">
          Synthetic survival demonstration. No external dataset is registered;
          binary scoring, rebuild, validation and monitoring are not applicable.
          This is not a passed check.
        </p>
      )}
      <div className="inventory-groups">
        <ModelRecordCard
          mode="live"
          title="Identity"
          subtitle="Model identity and version"
          facts={liveModelFacts(model)}
        />
        {groups.map((group) => (
          <section className="lifecycle-card" key={group.title} aria-label={group.title}>
            <h3>{group.title}</h3>
            <dl className="record-fields">
              {group.fields.map((field) => (
                <div key={field.label}>
                  <dt>{field.label}</dt>
                  <dd>
                    {field.value ?? "Not supplied by API"}
                    <details className="field-source"><summary>Source</summary>
                      {field.source ?? (field.value === null || field.value === undefined
                        ? "Source unavailable" : "Source: existing inventory API; original provenance unavailable")}
                    </details>
                  </dd>
                </div>
              ))}
            </dl>
            {group.title === "Data and reproducibility" && (
              <button className="button secondary" onClick={openDataset} disabled={synthetic}>
                View dataset description
              </button>
            )}
          </section>
        ))}
      </div>
<details className="help-details"><summary>About this record</summary><p>Fields reflect saved inventory data; a source is shown when supplied. Open Documentation or Validation for evidence, and History for recorded runs.</p></details>
    </section>
  );
}
