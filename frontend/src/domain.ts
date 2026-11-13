export type Status = "green" | "amber" | "red" | "not_assessable";
export type Stage = "Inventory" | "Documentation" | "Rebuild" | "Validation";
export interface Metric {
  name: string;
  value: string;
  status: Status;
  reason: string;
  required: boolean;
}
export interface Review {
  reviewer: string;
  decision: "Changes requested" | "Accepted";
  note: string;
  at: string;
}
export interface Model {
  id: string;
  name: string;
  type: "Credit" | "Fraud";
  language: "R" | "Python";
  version: string;
  owner: string | null;
  dataset: "UCI" | "ULB";
  stage: Stage;
  tier: "High" | "Medium";
  purpose: string;
  limitations: string[];
  metrics: Metric[];
  document: string | null;
  parity: {
    status: Status;
    rows: number | null;
    maxDifference: string;
    tolerance: string;
    reason: string;
  };
  review: Review | null;
}
export function canAccept(model: Model): boolean {
  return (
    model.stage === "Validation" &&
    model.metrics.length > 0 &&
    model.metrics.every((m) => !m.required || m.status === "green") &&
    model.parity.status === "green"
  );
}
export function reviewModel(
  model: Model,
  reviewer: string,
  decision: Review["decision"],
  note: string,
): Model {
  if (!reviewer.trim() || !note.trim())
    throw new Error("Enter a reviewer name and a reason.");
  if (
    model.owner &&
    reviewer.trim().toLowerCase() === model.owner.toLowerCase()
  )
    throw new Error("The reviewer must be different from the model owner.");
  if (decision === "Accepted" && !canAccept(model))
    throw new Error("Resolve required checks and parity before acceptance.");
  return {
    ...model,
    review: {
      reviewer: reviewer.trim(),
      decision,
      note: note.trim(),
      at: new Date().toISOString(),
    },
  };
}
export function draftDocument(model: Model): string {
  return `DEMO TEMPLATE — NOT AI-GENERATED OR VALIDATED\n\n${model.name} · ${model.id} · ${model.version}\n\n1. Purpose\n${model.purpose}\n\n2. Ownership\n${model.owner ?? "Not assigned"}\n\n3. Data\n${model.dataset === "UCI" ? "UCI Credit Default. Positive label: default=1; next-month target." : "ULB Credit Card Fraud. Positive label: Class=1 (fraud)."}\nFile checksum, transformations and split manifest: awaiting backend evidence.\n\n4. Methodology\nPending source-code extraction and human review.\n\n5. Implementation\n${model.language}; ${model.version}. Runtime and dependencies not yet recorded.\n\n6. Development testing\nNo executed evidence attached.\n\n7. Limitations\n${model.limitations.join("\n")}\n\n8. Monitoring\nProposed PSI review bands: 0.10 / 0.25. Outcome monitoring requires mature labels.\n\n9. Review\nHuman sign-off required. This template is not evidence of compliance.`;
}
