import type { createModelClient, RunRead } from "./api";
import type { AuthUser } from "./auth";
import { sameReviewCheckpoint, type ReviewItem } from "./reviews";
type Client = Pick<ReturnType<typeof createModelClient>, "getRun" | "getRunCheckpoint" | "getReview" | "resumeRun">;
export type Continuation = { run: RunRead; taskId: string; review: ReviewItem };

export async function loadContinuation(api: Client, runId: string, modelId: string, user: AuthUser, signal?: AbortSignal): Promise<Continuation> {
  const run = await api.getRun(runId, signal);
  if (run.model_id !== modelId) throw new Error("Run belongs to another model.");
  if (run.status !== "awaiting_review" || !["document", "rebuild"].includes(run.operation))
    throw new Error("No paused agent run to resume. Refresh run details.");
  const checkpoint = await api.getRunCheckpoint(runId, signal);
  if (!checkpoint) throw new Error("No resumable agent checkpoint is recorded.");
  const review = await api.getReview(checkpoint.reviewId, signal);
  if (review.run_id !== run.id || review.model_id !== modelId || review.model_version !== run.model_version ||
      review.gate !== (run.operation === "document" ? 1 : 2))
    throw new Error("Review does not match this run, model version or gate.");
  if (review.requested_by !== user.id) throw new Error("Only the initiating operator can resume this run.");
  if (review.status !== "decided" || !review.resolution)
    throw new Error("A recorded human decision is required. Use Review queue first.");
  // An accepted Gate 2 decision exists only after the Backend saw a passing official parity run.
  return { run, taskId: checkpoint.taskId, review };
}

export async function submitContinuation(api: Client, shown: Continuation, user: AuthUser, signal?: AbortSignal) {
  const fresh = await loadContinuation(api, shown.run.id, shown.run.model_id, user, signal);
  const left = shown.review.resolution!, right = fresh.review.resolution!;
  if (fresh.taskId !== shown.taskId || fresh.run.operation !== shown.run.operation ||
      fresh.run.dataset_id !== shown.run.dataset_id ||
      !sameReviewCheckpoint(shown.review, fresh.review) ||
      left.decision !== right.decision || left.rationale !== right.rationale ||
      left.reviewer_id !== right.reviewer_id || left.decided_at !== right.decided_at ||
      JSON.stringify(left.conditions) !== JSON.stringify(right.conditions))
    throw new Error("Checkpoint or decision changed. Refresh continuation and confirm again.");
  return api.resumeRun(fresh.run, fresh.review.id, signal);
}
