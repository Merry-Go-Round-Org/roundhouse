export type AuditEvent = { id: string; resource_id: string; action: string; created_at: string; payload: Record<string, string | string[] | null> };
const text = (v: unknown): v is string => typeof v === "string" && !!v.trim();
export function readAuditEvents(value: unknown, limit = 20): AuditEvent[] {
  if (!Array.isArray(value) || value.length > limit) throw new Error("Invalid model history response.");
  const ids = new Set<string>();
  return value.map(row => {
    if (!row || typeof row !== "object" || ![row.id,row.resource_id,row.action,row.created_at].every(text) ||
        !Number.isFinite(Date.parse(row.created_at)) || ids.has(row.id) || !row.payload ||
        typeof row.payload !== "object" || Array.isArray(row.payload)) throw new Error("Invalid or duplicate audit event.");
    ids.add(row.id);
    const keys = row.action === "agent_event" ? ["type","task_id"] :
      ["tool_call","tool_output","tool_result"].includes(row.action) ? ["tool","status"] :
      ["artifact_id","report_id","review_id","candidate_model_id","upload_id","sha256","decision","conditions"];
    const payload: AuditEvent["payload"] = {};
    for (const key of keys) {
      if (!(key in row.payload)) continue;
      const v: unknown = row.payload[key];
      if (key === "conditions") {
        if (!Array.isArray(v) || !v.every(text)) throw new Error("Invalid audit conditions.");
        payload[key] = v;
      } else {
        if (!(v === null || typeof v === "string")) throw new Error("Invalid public audit field.");
        payload[key] = v;
      }
    }
    // Do not retain unknown payload keys or actor/private metadata for display.
    return {id:row.id,resource_id:row.resource_id,action:row.action,created_at:row.created_at,payload};
  });
}
