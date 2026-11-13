export type DeletionKind = "models" | "datasets";
export interface DeletionItem { id: string; name: string; version: string; }
export interface DeletionSelection { kind: DeletionKind; items: DeletionItem[]; }
export interface DeletionPreview {
  ids: string[]; items: (DeletionItem & { blockers: string[]; counts: Record<string, number> })[];
  token: string; canDelete: boolean; effect: string;
}
export interface DeletionRequest { kind: DeletionKind; ids: string[]; token: string; requestId: string; }
export interface DeletionReceipt { request_id: string; deleted: string[]; retained: string[]; cleanup_pending?: string[]; }
const object = (v: unknown): v is Record<string, unknown> => !!v && typeof v === "object" && !Array.isArray(v);
const strings = (v: unknown): v is string[] => Array.isArray(v) && v.every(x => typeof x === "string" && !!x.trim());
export function selectionIds(ids: string[]) {
  if (!strings(ids) || ids.length < 1 || ids.length > 20 || new Set(ids).size !== ids.length) throw new Error("Select 1–20 distinct records.");
  return ids;
}
const same = (a: string[], b: string[]) => a.length === b.length && new Set(a).size === a.length && a.every(x => b.includes(x));
export const selectionBody = (kind: DeletionKind, ids: string[]) => kind === "models" ? { model_ids: selectionIds(ids) } : { dataset_version_ids: selectionIds(ids) };
export function readDeletionPreview(value: unknown, kind: DeletionKind, ids: string[]): DeletionPreview {
  selectionIds(ids);
  if (!object(value)) throw new Error("Invalid deletion preview.");
  const returnedIds = value[kind === "models" ? "model_ids" : "dataset_version_ids"];
  const rows = value[kind];
  if (!strings(returnedIds) || !same(returnedIds, ids) || !Array.isArray(rows) || rows.length !== ids.length ||
    value.atomic !== true || value.max_selection !== 20 || typeof value.can_delete !== "boolean" ||
    typeof value.effect !== "string" || !value.effect.trim() || typeof value.preview_token !== "string" || !/^[a-f0-9]{64}$/.test(value.preview_token))
    throw new Error("Deletion preview does not match this selection or the atomic contract.");
  const items = rows.map(row => {
    if (!object(row)) throw new Error("Invalid deletion item.");
    const id = row[kind === "models" ? "id" : "version_id"];
    if (typeof id !== "string" || !ids.includes(id) || typeof row.version !== "string" || !row.version.trim()) throw new Error("Invalid deletion identity/version.");
    let blockers: string[];
    const counts: Record<string, number> = {};
    if (kind === "models") {
      if (!strings(row.blockers) || !object(row.related_counts)) throw new Error("Missing model dependency checks.");
      blockers = row.blockers;
      for (const [key, count] of Object.entries(row.related_counts)) {
        if (typeof count !== "number" || !Number.isSafeInteger(count) || count < 0) throw new Error("Invalid related record count.");
        counts[key] = count;
      }
    } else {
      if (!object(row.blockers)) throw new Error("Missing dataset dependency checks.");
      blockers = Object.entries(row.blockers).flatMap(([key, refs]) => {
        if (!strings(refs)) throw new Error("Invalid dataset dependency checks.");
        return refs.map(ref => `${key.replaceAll("_", " ")}: ${ref}`);
      });
    }
    return { id, name: typeof row.name === "string" && row.name.trim() ? row.name : String(row.id ?? id), version: row.version, blockers, counts };
  });
  if (!same(items.map(i => i.id), ids) || value.can_delete !== !items.some(i => i.blockers.length)) throw new Error("Inconsistent deletion eligibility.");
  return { ids, items, token: value.preview_token, canDelete: value.can_delete, effect: value.effect };
}
export function readDeletionReceipt(value: unknown, request: DeletionRequest): DeletionReceipt {
  if (!object(value) || value.request_id !== request.requestId || value.status !== "deleted" ||
    !strings(value.deleted) || !same(value.deleted, request.ids) || !Array.isArray(value.blocked) || value.blocked.length ||
    !Array.isArray(value.failed) || value.failed.length || !strings(value.retained) ||
    (value.cleanup_pending !== undefined && !strings(value.cleanup_pending)))
    throw new Error("Deletion result is unconfirmed. Check the saved receipt before any retry.");
  return value as unknown as DeletionReceipt;
}
export function readPendingDeletion(value: unknown): DeletionRequest | null {
  if (!object(value) || !["models", "datasets"].includes(String(value.kind)) || !strings(value.ids) ||
    typeof value.token !== "string" || !/^[a-f0-9]{64}$/.test(value.token) ||
    typeof value.requestId !== "string" || !/^[a-f0-9-]{36}$/i.test(value.requestId)) return null;
  try { selectionIds(value.ids); return value as unknown as DeletionRequest; } catch { return null; }
}
export function createDeletionClient(request: (path: string, init?: RequestInit) => Promise<unknown>) {
  return {
    async preview(kind: DeletionKind, ids: string[], signal?: AbortSignal) {
      return readDeletionPreview(await request(`/${kind}/deletions/preview`, { method: "POST", body: JSON.stringify(selectionBody(kind, ids)), signal }), kind, ids);
    },
    async remove(pending: DeletionRequest, signal?: AbortSignal) {
      if (!readPendingDeletion(pending)) throw new Error("Invalid deletion request.");
      return readDeletionReceipt(await request(`/${pending.kind}/deletions`, { method: "POST", signal,
        body: JSON.stringify({ ...selectionBody(pending.kind, pending.ids), preview_token: pending.token, request_id: pending.requestId }) }), pending);
    },
    async receipt(pending: DeletionRequest, signal?: AbortSignal) {
      return readDeletionReceipt(await request(`/${pending.kind}/deletions/${encodeURIComponent(pending.requestId)}`, { signal }), pending);
    },
  };
}
