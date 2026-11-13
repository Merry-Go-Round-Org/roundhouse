import type { Model, Metric } from "./domain";
const creditMetrics: Metric[] = [
  {
    name: "ROC AUC",
    value: "0.781",
    status: "green",
    reason:
      "Illustrative result only. Example delta CI: −0.018 to +0.011; proposed margin −0.03.",
    required: true,
  },
  {
    name: "Calibration gap",
    value: "+1.8 pp",
    status: "amber",
    reason:
      "Example confidence interval crosses the proposed tolerance. Reviewer investigation required.",
    required: true,
  },
  {
    name: "Score PSI",
    value: "0.072",
    status: "green",
    reason:
      "Illustrative reference comparison; below the conventional 0.10 review band.",
    required: true,
  },
  {
    name: "Temporal back-test",
    value: "Unavailable",
    status: "not_assessable",
    reason:
      "UCI monthly columns are features within one snapshot, not separately labelled cohorts.",
    required: true,
  },
  {
    name: "Group fairness",
    value: "Review needed",
    status: "amber",
    reason:
      "Small intersectional groups require counts and uncertainty; no fairness conclusion is established.",
    required: true,
  },
];
export const demoModels: Model[] = [
  {
    id: "CR-001",
    name: "Credit default scorecard",
    type: "Credit",
    language: "R",
    version: "v1.2",
    owner: "Demo owner A",
    dataset: "UCI",
    stage: "Validation",
    tier: "High",
    purpose:
      "Estimate next-month default risk for the historical Taiwan credit-card cohort.",
    limitations: [
      "Historical Taiwan data is not evidence of suitability for UK lending.",
      "No calendar out-of-time evaluation is available.",
      "Small sex-by-age groups limit fairness conclusions.",
    ],
    metrics: creditMetrics,
    document: null,
    parity: {
      status: "green",
      rows: 6000,
      maxDifference: "2.1 × 10⁻¹²",
      tolerance: "1 × 10⁻⁸ (demo)",
      reason:
        "Illustrative matching predictions. No R/Python run has been performed by this frontend.",
    },
    review: null,
  },
  {
    id: "FR-001",
    name: "Transaction fraud baseline",
    type: "Fraud",
    language: "Python",
    version: "v0.4",
    owner: "Demo owner B",
    dataset: "ULB",
    stage: "Validation",
    tier: "High",
    purpose: "Rank transactions for a simulated fraud review queue.",
    limitations: [
      "Protected-group attributes are unavailable; do not infer them from PCA inputs.",
      "Two days of relative time do not establish long-term stability.",
      "PCA transformation provenance and label delay are unavailable.",
    ],
    metrics: [
      {
        name: "Average precision",
        value: "0.742",
        status: "green",
        reason:
          "Illustrative metric; operational acceptance floor is not yet agreed.",
        required: false,
      },
      {
        name: "Precision at capacity",
        value: "Not configured",
        status: "not_assessable",
        reason: "Alert capacity and threshold require team agreement.",
        required: true,
      },
      {
        name: "Score PSI",
        value: "0.283",
        status: "red",
        reason:
          "Illustrative breach of the 0.25 convention. Investigate distribution change.",
        required: true,
      },
      {
        name: "Protected-group fairness",
        value: "Unavailable",
        status: "not_assessable",
        reason:
          "ULB publishes no protected-group attributes. This is not a fairness pass.",
        required: true,
      },
    ],
    document: null,
    parity: {
      status: "not_assessable",
      rows: null,
      maxDifference: "—",
      tolerance: "Not agreed",
      reason: "No migration comparison is attached.",
    },
    review: null,
  },
  {
    id: "CR-002",
    name: "Credit challenger",
    type: "Credit",
    language: "Python",
    version: "v0.1",
    owner: null,
    dataset: "UCI",
    stage: "Documentation",
    tier: "Medium",
    purpose: "Compare a candidate credit model with the legacy scorecard.",
    limitations: [
      "Model methodology and split manifest are awaiting review.",
      "Same snapshot-only temporal limitations as the UCI scorecard.",
    ],
    metrics: [],
    document: null,
    parity: {
      status: "not_assessable",
      rows: null,
      maxDifference: "—",
      tolerance: "Not agreed",
      reason: "No comparison evidence attached.",
    },
    review: null,
  },
  {
    id: "CR-003",
    name: "Legacy approval rules",
    type: "Credit",
    language: "R",
    version: "v0.8",
    owner: null,
    dataset: "UCI",
    stage: "Inventory",
    tier: "Medium",
    purpose:
      "Document a sample legacy rule model and its operating boundaries.",
    limitations: [
      "Rules and intended use have not been independently reviewed.",
    ],
    metrics: [],
    document: null,
    parity: {
      status: "not_assessable",
      rows: null,
      maxDifference: "—",
      tolerance: "Not agreed",
      reason: "Rebuild has not been started.",
    },
    review: null,
  },
];
