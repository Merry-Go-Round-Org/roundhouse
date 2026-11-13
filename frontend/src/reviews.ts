// Matches the implemented backend review_service.review_read response.
// Only the Backend can bind an authenticated reviewer and persist a decision.
import type { AuthUser } from "./auth";
export type ReviewStatus = "pending" | "decided";
export type ReviewChoice =
  "accepted" | "changes_requested" | "accepted_with_conditions";
export interface ReviewDraft {
  artifact_id: string;
  artifact_version: string;
  decision: ReviewChoice;
  rationale: string;
  conditions: string[];
}
export interface ReviewItem {
  id: string;
  model_id: string;
  model_name: string;
  model_version: string;
  run_id: string;
  gate: 1 | 2 | 3;
  status: ReviewStatus;
  artifact_id: string;
  artifact_version: string;
  created_at: string;
  resolution: (ReviewDraft & {
    reviewer_id: string;
    decided_at: string;
    review_elapsed_seconds?: number | null;
    timing_limitation?: string;
  }) | null;
  /** Backend eligibility and version-bound gate information; optional for older read fixtures. */
  blockers?: string[];
  approval_available?: boolean;
  hard_blockers?: string[];
  limitations?: string[];
  allowed_decisions?: ReviewChoice[];
  /** Gate 2 only: the rebuild candidate under review, on which official parity runs. */
  candidate_model_id?: string | null;
  /** Read-only evidence; validated with candidate/run binding in InputContractChecks. */
  input_contract?: unknown;
  requested_by?: string;
}
const choices: readonly string[] = [
  "accepted",
  "changes_requested",
  "accepted_with_conditions",
];
function text(v: unknown): v is string {
  return typeof v === "string" && !!v.trim();
}
function object(v: unknown): v is Record<string, unknown> {
  return !!v && typeof v === "object" && !Array.isArray(v);
}
function date(v: unknown): v is string {
  return text(v) && Number.isFinite(Date.parse(v));
}
export function readReviews(
  value: unknown,
  status: ReviewStatus,
): ReviewItem[] {
  const invalid = () => {
    throw new Error(
      "Review list does not match the proposed contract. Backend agreement is required.",
    );
  };
  if (!Array.isArray(value)) return invalid();
  const ids = new Set<string>();
  for (const r of value) {
    if (
      !object(r) ||
      ![
        "id",
        "model_id",
        "model_name",
        "model_version",
        "run_id",
        "artifact_id",
        "artifact_version",
      ].every((k) => text(r[k])) ||
      ![1, 2, 3].includes(r.gate as number) ||
      r.status !== status ||
      !date(r.created_at) ||
      ids.has(r.id as string)
    )
      return invalid();
    ids.add(r.id as string);
    for (const key of ["hard_blockers", "limitations", "allowed_decisions"] as const) {
      const list = r[key];
      if (list !== undefined && (!Array.isArray(list) || !list.every(text) ||
          new Set(list).size !== list.length ||
          (key === "allowed_decisions" && !list.every(v => choices.includes(v))))) return invalid();
    }
    if ((r.blockers !== undefined &&
          (!Array.isArray(r.blockers) || !r.blockers.every(text))) ||
        (r.approval_available !== undefined && typeof r.approval_available !== "boolean") ||
        (r.candidate_model_id !== undefined && r.candidate_model_id !== null && !text(r.candidate_model_id)) ||
        (r.requested_by !== undefined && !text(r.requested_by)))
      return invalid();
    if (status === "pending") {
      if (r.resolution !== null) return invalid();
    } else {
      const d = r.resolution;
      if (
        !object(d) ||
        !text(d.reviewer_id) ||
        !date(d.decided_at) ||
        Date.parse(d.decided_at) < Date.parse(r.created_at as string) ||
        !choices.includes(String(d.decision)) ||
        !text(d.rationale) ||
        d.artifact_id !== r.artifact_id ||
        d.artifact_version !== r.artifact_version ||
        !Array.isArray(d.conditions) ||
        !d.conditions.every(text) ||
        (d.decision === "accepted_with_conditions" && !d.conditions.length) ||
        (d.review_elapsed_seconds !== undefined && d.review_elapsed_seconds !== null &&
          (typeof d.review_elapsed_seconds !== "number" ||
            !Number.isFinite(d.review_elapsed_seconds) || d.review_elapsed_seconds < 0)) ||
        (d.timing_limitation !== undefined && typeof d.timing_limitation !== "string")
      )
        return invalid();
    }
  }
  return value as ReviewItem[];
}
export function prepareReviewDraft(
  item: ReviewItem,
  decision: string,
  rationale: string,
  conditions: string,
): ReviewDraft {
  if (item.status !== "pending")
    throw new Error("A decided review cannot receive a new local draft.");
  if (!choices.includes(decision)) throw new Error("Choose a decision.");
  if (!decisionAvailable(item, decision))
    throw new Error("Backend approval is unavailable for this review gate or its blocking checks.");
  if (!rationale.trim()) throw new Error("Enter a rationale.");
  const lines = conditions
    .split(/\r?\n/)
    .map((s) => s.trim())
    .filter(Boolean);
  if (decision === "accepted_with_conditions" && !lines.length)
    throw new Error("Enter at least one condition.");
  if (decision === "accepted_with_conditions" && item.gate === 3 && item.limitations !== undefined) {
    const ids = item.limitations;
    if (lines.length !== ids.length ||
        ids.some(id => lines.filter(line => line.includes(id)).length !== 1) ||
        lines.some(line => ids.filter(id => line.includes(id)).length !== 1))
      throw new Error("Write one acknowledgement naming each limitation, without naming another limitation in the same condition.");
  }
  return {
    artifact_id: item.artifact_id,
    artifact_version: item.artifact_version,
    decision: decision as ReviewChoice,
    rationale: rationale.trim(),
    conditions: decision === "accepted_with_conditions" ? lines : [],
  };
}
/** Frontend guard only. Server permissions, independence, parity and gates are authoritative. */
export function canDecideReview(item: ReviewItem, user: AuthUser): boolean {
  return item.status === "pending" &&
    (user.role === "validator" || user.role === "approver" || user.role === "admin") &&
    user.id !== item.requested_by &&
    !(item.gate === 3 && user.role === "validator");
}

export function canChooseReviewDecision(
  item: ReviewItem, user: AuthUser, choice: string,
): boolean {
  if (!canDecideReview(item, user)) return false;
  return decisionAvailable(item, choice);
}

function decisionAvailable(item: ReviewItem, choice: string): boolean {
  if (!choices.includes(choice)) return false;
  if (item.allowed_decisions && !item.allowed_decisions.includes(choice as ReviewChoice)) return false;
  if (choice === "changes_requested") return true;
  const hasPolicy = item.hard_blockers !== undefined || item.limitations !== undefined || item.allowed_decisions !== undefined;
  if (hasPolicy) {
    // A partial response must never relax the older blocking contract.
    if (!item.hard_blockers || !item.limitations || !item.allowed_decisions) return false;
    if (item.hard_blockers.length || item.blockers?.some(id => !item.limitations!.includes(id))) return false;
    if (item.gate === 3 && choice === "accepted_with_conditions")
      return item.limitations.length > 0;
    if (item.limitations.length) return false;
  }
  // Gate 2 is blocked by the Backend's official_parity_not_passed until parity passes.
  return approvalAvailable(item);
}

export function sameReviewPermissions(left: ReviewItem, right: ReviewItem): boolean {
  const sorted = (list: string[] | undefined) => list === undefined ? undefined : JSON.stringify([...list].sort());
  return left.approval_available === right.approval_available &&
    (["blockers", "hard_blockers", "limitations", "allowed_decisions"] as const)
      .every(key => sorted(left[key]) === sorted(right[key]));
}

export function limitationConditions(ids: string[], notes: Record<string, string>): string {
  return ids.map(id => {
    let note = notes[id]?.trim() ?? "";
    // Operators may paste the check label with their explanation. Remove only
    // this exact leading ID (and repeated copies), never IDs in the explanation.
    while (note.startsWith(id)) {
      const rest = note.slice(id.length);
      if (rest && !/^[\s:：]/.test(rest)) break;
      note = rest.trimStart().replace(/^[:：]\s*/, "").trim();
    }
    if (!note) throw new Error(`Acknowledge ${id} in writing.`);
    return `${id}: ${note.replace(/\s+/g, " ")}`;
  }).join("\n");
}

function approvalAvailable(item: ReviewItem): boolean {
  if (item.gate === 2 && (item.approval_available !== true || !item.candidate_model_id)) return false;
  return item.approval_available !== false && !item.blockers?.length;
}

export function sameReviewCheckpoint(left: ReviewItem, right: ReviewItem): boolean {
  return left.id === right.id && left.model_id === right.model_id &&
    left.model_version === right.model_version &&
    left.run_id === right.run_id && left.gate === right.gate &&
    left.artifact_id === right.artifact_id &&
    left.artifact_version === right.artifact_version &&
    left.requested_by === right.requested_by &&
    left.candidate_model_id === right.candidate_model_id;
}

export function waitingTime(createdAt: string, now = Date.now()): string {
  const difference = now - Date.parse(createdAt);
  if (!Number.isFinite(difference) || difference < 0)
    return "Unavailable (check timestamps)";
  const minutes = Math.floor(difference / 60000);
  return minutes < 60
    ? `${minutes} min`
    : `${Math.floor(minutes / 60)} h ${minutes % 60} min`;
}
export const gateLabels = {
  1: "Document review",
  2: "Rebuild review",
  3: "Validation sign-off",
} as const;
