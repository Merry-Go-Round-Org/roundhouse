import type { ReactNode } from "react";

/**
 * Common presentation boundary for documentation, rebuild/parity and validation.
 * The content/actions are supplied by each mode; this component makes no
 * approval decision and never treats demo results as real evidence.
 */
export function ModelEvidenceSection({
  title,
  subtitle,
  mode,
  icon,
  children,
}: {
  title: string;
  subtitle: string;
  mode: "demo" | "live";
  icon?: ReactNode;
  children: ReactNode;
}) {
  return (
    <section className="panel model-evidence-section" aria-label={title}>
      <div className="panel-title">
        <div>
          {icon}
          <h2>{title}</h2>
        </div>
        <p>{subtitle}</p>
      </div>
      <p className="model-evidence-boundary">
        {mode === "demo"
          ? "Demo illustration only. No Backend jobs, uploads or human review decisions are executed here."
          : "Review the evidence before approving a model."}
      </p>
      <div className="section-content">{children}</div>
    </section>
  );
}
