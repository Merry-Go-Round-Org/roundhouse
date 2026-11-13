/** Browser contracts from backend/AI_NATIVE_HANDOFF.md at c06d921. */
export type Proposal = {
  id: string;
  source_run_id: string | null;
  source_task_id: string | null;
  conversation_id: string | null;
  model_id: string;
  type: string;
  args: Record<string, unknown>;
  rationale: string;
  evidence: { kind: string; ref: string }[];
  required_role: string;
  preconditions: Record<string, unknown>;
  status:
    "pending" | "approved" | "rejected" | "superseded" | "expired" | "executed";
  decision: Record<string, unknown> | null;
  result: Record<string, unknown> | null;
  created_at: string;
};
export type Mapping = {
  dataset_id: string;
  feature_map: Record<string, string>;
  unmatched_features: string[];
  rationale: string;
};
export type Candidate = {
  candidate_id: string;
  fitted_model: "saved_model" | "parameters_in_code" | "training_code_only";
  executable: boolean;
  execution_error: string | null;
  manifest: Record<
    string,
    {
      value: unknown;
      source: string;
      evidence?: string | null;
      reason?: string | null;
    }
  >;
  dataset_mapping: Mapping | null;
  scoring: {
    entry_point: string | null;
    wrapper: boolean;
    wrapper_sha256?: string | null;
    self_check?: string | null;
  };
  files: string[];
  problems: string[];
};
export type Archive = {
  id: string;
  actor_id: string;
  sha256: string;
  status:
    | "queued"
    | "running"
    | "awaiting_review"
    | "confirmed"
    | "building"
    | "completed"
    | "failed"
    | "cancelled"
    | "blocked";
  files: { path: string; sha256: string }[];
  plan: { candidates: Candidate[]; unassigned_files: string[] } | null;
  plan_sha256: string | null;
  decision: Record<string, unknown> | null;
  error: string | null;
  created_at: string;
  models: {
    model_id: string;
    name: string;
    model_version: string;
    document_run_id: string | null;
    reproduction: Record<string, unknown> | null;
  }[];
};
export type GateZero = {
  plan_sha256: string;
  rationale: string;
  candidates: {
    candidate_id: string;
    accept: boolean;
    manifest: Record<string, unknown>;
    dataset_mapping?: Mapping;
  }[];
};
export type ActionCatalog = Record<
  string,
  {
    required_role: string;
    execution_available: boolean;
    args_schema: Record<string, unknown>;
  }
>;
export type Analysis = {
  run_id: string;
  kind: string;
  result: {
    ai_native_run_id?: string;
    outcome?: Record<string, unknown>;
    exports: Record<string, string>;
    artifact_ids: string[];
    proposal_ids: string[];
  } | null;
};
const obj = (v: unknown): v is Record<string, unknown> =>
  !!v && typeof v === "object" && !Array.isArray(v);
const str = (v: unknown): v is string => typeof v === "string" && !!v.trim();
const nullable = (v: unknown) => v === null || typeof v === "string";
const strings = (v: unknown): v is string[] => Array.isArray(v) && v.every(str);
const hash = (v: unknown) => typeof v === "string" && /^[a-f0-9]{64}$/.test(v);
const date = (v: unknown) => str(v) && Number.isFinite(Date.parse(v));
function bad(): never {
  throw new Error("AI workflow response does not match the current contract.");
}
export function readProposal(v: unknown, id?: string): Proposal {
  if (
    !obj(v) ||
    ![v.id, v.model_id, v.type, v.required_role].every(str) ||
    (id && v.id !== id) ||
    ![v.source_run_id, v.source_task_id, v.conversation_id].every(nullable) ||
    !obj(v.args) ||
    !obj(v.preconditions) ||
    typeof v.rationale !== "string" ||
    !Array.isArray(v.evidence) ||
    !v.evidence.every((e) => obj(e) && str(e.kind) && str(e.ref)) ||
    ![
      "pending",
      "approved",
      "rejected",
      "superseded",
      "expired",
      "executed",
    ].includes(String(v.status)) ||
    !(v.decision === null || obj(v.decision)) ||
    !(v.result === null || obj(v.result)) ||
    !date(v.created_at)
  )
    bad();
  return v as unknown as Proposal;
}
export function readArchive(v: unknown, id?: string): Archive {
  if (
    !obj(v) ||
    !str(v.id) ||
    (id && v.id !== id) ||
    !str(v.actor_id) ||
    !hash(v.sha256) ||
    !date(v.created_at) ||
    ![
      "queued",
      "running",
      "awaiting_review",
      "confirmed",
      "building",
      "completed",
      "failed",
      "cancelled",
      "blocked",
    ].includes(String(v.status)) ||
    !Array.isArray(v.files) ||
    !v.files.every((f) => obj(f) && str(f.path) && hash(f.sha256)) ||
    !(v.plan_sha256 === null || hash(v.plan_sha256)) ||
    !nullable(v.error) ||
    !(v.decision === null || obj(v.decision)) ||
    !Array.isArray(v.models) ||
    !v.models.every(
      (m) =>
        obj(m) &&
        [m.model_id, m.name, m.model_version].every(str) &&
        nullable(m.document_run_id) &&
        (m.reproduction === null || obj(m.reproduction)),
    )
  )
    bad();
  if (v.plan !== null) {
    const p = v.plan;
    if (
      !obj(p) ||
      !strings(p.unassigned_files) ||
      !Array.isArray(p.candidates) ||
      !p.candidates.every((c) => {
        if (
          !obj(c) ||
          !str(c.candidate_id) ||
          !["saved_model", "parameters_in_code", "training_code_only"].includes(
            String(c.fitted_model),
          ) ||
          typeof c.executable !== "boolean" ||
          !nullable(c.execution_error) ||
          !strings(c.files) ||
          !strings(c.problems) ||
          !obj(c.manifest) ||
          !obj(c.scoring) ||
          typeof c.scoring.wrapper !== "boolean" ||
          !nullable(c.scoring.entry_point)
        )
          return false;
        if (c.scoring.wrapper && !hash(c.scoring.wrapper_sha256)) return false;
        if (
          !Object.values(c.manifest).every(
            (f) =>
              obj(f) &&
              [
                "file",
                "artifact",
                "code",
                "document",
                "proposed",
                "unknown",
              ].includes(String(f.source)) &&
              (f.evidence === undefined || nullable(f.evidence)) &&
              (f.reason === undefined || nullable(f.reason)),
          )
        )
          return false;
        const m = c.dataset_mapping;
        return (
          m === null ||
          (obj(m) &&
            str(m.dataset_id) &&
            obj(m.feature_map) &&
            Object.values(m.feature_map).every(str) &&
            strings(m.unmatched_features) &&
            str(m.rationale))
        );
      }) ||
      new Set(p.candidates.map((c) => c.candidate_id)).size !==
        p.candidates.length
    )
      bad();
  }
  if (v.status === "awaiting_review" && (!v.plan || !v.plan_sha256)) bad();
  return v as unknown as Archive;
}
export function proposalSnapshot(p: Proposal) {
  return JSON.stringify([
    p.id,
    p.model_id,
    p.type,
    p.args,
    p.preconditions,
    p.status,
    p.required_role,
    p.decision,
    p.source_run_id,
    p.source_task_id,
    p.conversation_id,
  ]);
}
type Request = (path: string, init?: RequestInit) => Promise<unknown>;
type ResponseFor = (path: string, init?: RequestInit) => Promise<Response>;
type UploadMultipart = (path: string, form: FormData, progress: (percent: number | null) => void, signal?: AbortSignal) => Promise<unknown>;
export function createNativeClient(request: Request, responseFor: ResponseFor, uploadMultipart: UploadMultipart) {
  const get = (p: string, signal?: AbortSignal) =>
    request(p, { signal, cache: "no-store" });
  const post = (p: string, body?: unknown, signal?: AbortSignal) =>
    request(p, {
      method: "POST",
      signal,
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    });
  const enc = encodeURIComponent;
  const client = {
    async capabilities(signal?: AbortSignal) {
      const v = await get("/intake/capabilities", signal);
      if (!obj(v) || !obj(v.harness_profiles) || !str(v.gate_0_role)) bad();
      return v as {
        harness_profiles: Record<string, unknown>;
        gate_0_role: string;
      };
    },
    async actions(signal?: AbortSignal): Promise<ActionCatalog> {
      const v = await get("/proposals/actions", signal);
      if (
        !obj(v) ||
        !Object.values(v).every(
          (x) =>
            obj(x) &&
            str(x.required_role) &&
            typeof x.execution_available === "boolean" &&
            obj(x.args_schema),
        )
      )
        bad();
      return v as ActionCatalog;
    },
    async proposals(
      filters: { model_id?: string; source_run_id?: string; status?: string },
      offset = 0,
      signal?: AbortSignal,
    ) {
      const q = new URLSearchParams({
        offset: String(offset),
        limit: "20",
        ...Object.fromEntries(Object.entries(filters).filter(([, v]) => !!v)),
      });
      const v = await get("/proposals?" + q, signal);
      if (!Array.isArray(v) || v.length > 20) bad();
      const rows = v.map((x) => readProposal(x));
      if (
        new Set(rows.map((x) => x.id)).size !== rows.length ||
        rows.some(
          (x) =>
            (filters.model_id && x.model_id !== filters.model_id) ||
            (filters.source_run_id &&
              x.source_run_id !== filters.source_run_id),
        )
      )
        bad();
      return rows;
    },
    async proposal(id: string, signal?: AbortSignal) {
      return readProposal(await get("/proposals/" + enc(id), signal), id);
    },
    async decide(
      p: Proposal,
      body: {
        decision: "approved" | "rejected";
        rationale: string;
        owner_id?: string;
        due_date?: string;
      },
      signal?: AbortSignal,
    ) {
      const fresh = await client.proposal(p.id, signal);
      if (
        fresh.status !== "pending" ||
        proposalSnapshot(fresh) !== proposalSnapshot(p)
      )
        throw new Error("Proposal changed. Reload and inspect it again.");
      return readProposal(
        await post("/proposals/" + enc(p.id) + "/decision", body, signal),
        p.id,
      );
    },
    async execute(p: Proposal, signal?: AbortSignal) {
      const fresh = await client.proposal(p.id, signal);
      if (
        fresh.status !== "approved" ||
        proposalSnapshot(fresh) !== proposalSnapshot(p)
      )
        throw new Error("Proposal changed. Reload before execution.");
      return readProposal(
        await post("/proposals/" + enc(p.id) + "/execute", undefined, signal),
        p.id,
      );
    },
    async archives(offset = 0, signal?: AbortSignal) {
      const v = await get(`/intake/archives?offset=${offset}&limit=20`, signal);
      if (!Array.isArray(v) || v.length > 20) bad();
      const rows = v.map((x) => readArchive(x));
      if (new Set(rows.map((x) => x.id)).size !== rows.length) bad();
      return rows;
    },
    async archive(id: string, signal?: AbortSignal) {
      return readArchive(await get("/intake/archives/" + enc(id), signal), id);
    },
    async upload(file: File, signal?: AbortSignal, progress: (percent: number | null) => void = () => {}) {
      if (!file.size || !file.name.toLowerCase().endsWith(".zip"))
        throw new Error("Choose a non-empty ZIP archive.");
      const form = new FormData();
      form.append("archive", file);
      return readArchive(
        await uploadMultipart("/intake/archives", form, progress, signal),
      );
    },
    async gateZero(archive: Archive, body: GateZero, signal?: AbortSignal) {
      const fresh = await client.archive(archive.id, signal);
      if (
        fresh.status !== "awaiting_review" ||
        fresh.plan_sha256 !== body.plan_sha256
      )
        throw new Error("Intake plan changed. Reload before reviewing.");
      return readArchive(
        await post(
          "/intake/archives/" + enc(archive.id) + "/decision",
          body,
          signal,
        ),
        archive.id,
      );
    },
    async archiveAction(
      id: string,
      action: "cancel" | "retry",
      signal?: AbortSignal,
    ) {
      return readArchive(
        await post(`/intake/archives/${enc(id)}/${action}`, undefined, signal),
        id,
      );
    },
    async file(
      id: string,
      path: string,
      candidate?: string,
      signal?: AbortSignal,
    ) {
      const q = new URLSearchParams({
        path,
        ...(candidate ? { candidate_id: candidate } : {}),
      });
      return (
        await responseFor(`/intake/archives/${enc(id)}/file?${q}`, {
          signal,
          cache: "no-store",
        })
      ).blob();
    },
    async analysis(id: string, signal?: AbortSignal): Promise<Analysis> {
      const v = await get(`/runs/${enc(id)}/analysis`, signal);
      if (!obj(v) || v.run_id !== id || !str(v.kind)) bad();
      const out = v.result;
      if (
        out !== null &&
        (!obj(out) ||
          (out.ai_native_run_id !== undefined && !str(out.ai_native_run_id)) ||
          (out.outcome !== undefined && !obj(out.outcome)) ||
          !obj(out.exports) ||
          !Object.values(out.exports).every((x) => typeof x === "string") ||
          !strings(out.artifact_ids) ||
          !strings(out.proposal_ids))
      )
        bad();
      return { run_id: id, kind: v.kind, result: out } as Analysis;
    },
  };
  return client;
}
