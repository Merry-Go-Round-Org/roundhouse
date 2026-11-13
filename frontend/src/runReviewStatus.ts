/** The review service closes a reviewed run using failed/changes_requested. */
export function isRevisionRequested(run: { status: string; stage?: string | null } | null) {
  return run?.status === "failed" && run.stage === "changes_requested";
}
export const revisionRequestedMessage = "Changes requested — this reviewed run is closed for revision. Check History for follow-up work and its current status.";
