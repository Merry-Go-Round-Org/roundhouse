export type FindingStatus = "open" | "in_progress" | "resolved";
export type Finding = { id: string; model_id: string; report_id: string; check_id: string; severity: string;
  status: FindingStatus; summary: string; owner_id: string | null; due_date: string | null;
  comments: {actor_id: string; comment: string; at: string}[] };
export type FindingOwner = {id: string; username: string; role: string};
export type FindingPatch = {comment: string; status?: FindingStatus; owner_id?: string | null; due_date?: string | null};
const text = (v: unknown): v is string => typeof v === "string" && !!v.trim();
export function readFinding(value: unknown, modelId: string): Finding {
  const r = value as Finding;
  if (!r || typeof r !== "object" || r.model_id !== modelId ||
      ![r.id,r.report_id,r.check_id,r.severity,r.summary].every(text) ||
      !["open","in_progress","resolved"].includes(r.status) ||
      !(r.owner_id === null || text(r.owner_id)) ||
      !(r.due_date === null || (text(r.due_date) && Number.isFinite(Date.parse(r.due_date)))) ||
      !Array.isArray(r.comments) || r.comments.some(c => !c || !text(c.actor_id) || !text(c.comment) || !text(c.at) || !Number.isFinite(Date.parse(c.at))))
    throw new Error("Finding response does not match this model or the backend contract.");
  return r;
}
export function readFindingOwners(value: unknown): FindingOwner[] {
  if (!Array.isArray(value) || value.some(r => !r || ![r.id,r.username,r.role].every(text)) ||
      new Set(value.map(r => r.id)).size !== value.length) throw new Error("Invalid finding owner list.");
  return value;
}
export function sameFinding(a: Finding, b: Finding): boolean {
  return ["id","model_id","report_id","check_id","severity","status","summary","owner_id","due_date"].every(k => a[k as keyof Finding] === b[k as keyof Finding]) &&
    JSON.stringify(a.comments) === JSON.stringify(b.comments);
}
export function findingPatch(finding: Finding, status: FindingStatus, owner: string, due: string, comment: string): FindingPatch {
  if (!comment.trim() || comment.trim().length > 4000) throw new Error("Enter an update comment (maximum 4,000 characters).");
  const patch: FindingPatch = {comment:comment.trim()};
  if (!["open","in_progress","resolved"].includes(status)) throw new Error("Invalid finding status.");
  if (status !== finding.status) patch.status = status;
  if ((owner || null) !== finding.owner_id) patch.owner_id = owner || null;
  const original = finding.due_date ? new Date(finding.due_date).toISOString().slice(0,16) : "";
  if (due !== original) {
    if (!due) patch.due_date = null;
    else {
      if (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$/.test(due)) throw new Error("Enter a valid UTC due date.");
      const d = new Date(due + ":00Z");
      if (!Number.isFinite(d.getTime()) || d.toISOString().slice(0,16) !== due) throw new Error("Enter a valid UTC due date.");
      patch.due_date = d.toISOString();
    }
  }
  return patch;
}
