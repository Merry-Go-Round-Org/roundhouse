import { it, expect, vi } from "vitest";
import {
  prepareReviewDraft,
  readReviews,
  canDecideReview,
  canChooseReviewDecision,
  sameReviewCheckpoint,
  sameReviewPermissions,
  limitationConditions,
  waitingTime,
  type ReviewItem,
} from "./reviews";
import { createModelClient } from "./api";
const item: ReviewItem = {
  id: "q1",
  model_id: "m1",
  model_name: "Credit",
  model_version: "1",
  run_id: "r1",
  gate: 1,
  status: "pending",
  artifact_id: "a1",
  artifact_version: "2",
  created_at: "2026-11-05T12:00:00Z",
  resolution: null,
};
const limited: ReviewItem = { ...item, gate: 3, approval_available: false,
  blockers: ["split_provenance", "saved_metric_consistency"], hard_blockers: [],
  limitations: ["split_provenance", "saved_metric_consistency"],
  allowed_decisions: ["changes_requested", "accepted_with_conditions"] };
it("uses Gate 3 allowed decisions and requires one written acknowledgement per limitation", () => {
  const user = { id: "approver", role: "approver" } as const;
  expect(readReviews([limited], "pending")[0]).toEqual(limited);
  expect(canChooseReviewDecision(limited, user, "accepted")).toBe(false);
  expect(canChooseReviewDecision(limited, user, "accepted_with_conditions")).toBe(true);
  expect(canChooseReviewDecision(limited, { id: "v", role: "validator" }, "accepted_with_conditions")).toBe(false);
  const conditions = limitationConditions(limited.limitations!, { split_provenance: "Restrict\nuse", saved_metric_consistency: "Recheck quarterly" });
  expect(prepareReviewDraft(limited, "accepted_with_conditions", "Reviewed", conditions).conditions)
    .toEqual(["split_provenance: Restrict use", "saved_metric_consistency: Recheck quarterly"]);
  expect(() => limitationConditions(limited.limitations!, { split_provenance: " " })).toThrow("Acknowledge");
  for (const invalid of ["split_provenance: note", "split_provenance saved_metric_consistency: note\nunknown: note", "split_provenance: note\nsplit_provenance: note"])
    expect(() => prepareReviewDraft(limited, "accepted_with_conditions", "Reviewed", invalid)).toThrow("acknowledgement");
  for (const blocked of [
    { ...limited, hard_blockers: ["failure"] }, { ...limited, hard_blockers: undefined },
    { ...limited, allowed_decisions: undefined }, { ...limited, allowed_decisions: [] },
    { ...limited, blockers: [...limited.blockers!, "unclassified"] },
    { ...limited, limitations: [], blockers: [] },
  ]) expect(canChooseReviewDecision(blocked, user, "accepted_with_conditions")).toBe(false);
});
it("rejects malformed policy fields and detects changed limitation or permission sets", () => {
  for (const value of [ { ...limited, limitations: null }, { ...limited, limitations: ["same", "same"] },
    { ...limited, allowed_decisions: ["approve"] }, { ...limited, hard_blockers: "none" } ])
    expect(() => readReviews([value], "pending")).toThrow();
  expect(sameReviewPermissions(limited, { ...limited, limitations: [...limited.limitations!].reverse() })).toBe(true);
  for (const value of [{ ...limited, limitations: ["new"] }, { ...limited, allowed_decisions: [] },
    { ...limited, hard_blockers: ["failure"] }, { ...limited, approval_available: true }])
    expect(sameReviewPermissions(limited, value)).toBe(false);
});
it("adds one acknowledgement prefix without removing explanation text or accepting only a label", () => {
  const id = "split_provenance";
  for (const note of ["Restrict use", `${id}: Restrict use`, `${id} : ${id}: Restrict use`,
    `${id}\nRestrict use`, `${id}：Restrict use`])
    expect(limitationConditions([id], { [id]: note })).toBe(`${id}: Restrict use`);
  for (const note of [id, `${id}:`, `${id}: ${id}:`, " \n "])
    expect(() => limitationConditions([id], { [id]: note })).toThrow("Acknowledge");
  expect(limitationConditions([id], { [id]: "Keep split_provenance visible" }))
    .toBe(`${id}: Keep split_provenance visible`);
  expect(limitationConditions([id], { [id]: "split_provenance_detail requires review" }))
    .toBe(`${id}: split_provenance_detail requires review`);
});
it("reads only matching queue states and rejects missing version bindings or duplicate reviews", () => {
  expect(readReviews([item], "pending")).toEqual([item]);
  for (const invalid of [
    [{ ...item, artifact_version: undefined }],
    [item, item],
    [{ ...item, gate: 4 }],
    [{ ...item, status: "decided" }],
  ])
    expect(() => readReviews(invalid, "pending")).toThrow("proposed contract");
});
it("requires a recorded decision to bind the same artifact/version and a reviewer", () => {
  const resolution = {
    ...prepareReviewDraft(item, "accepted", "Reviewed", ""),
    reviewer_id: "u1",
    decided_at: "2026-11-05T13:00:00Z",
  };
  const decided = { ...item, status: "decided", resolution };
  expect(readReviews([decided], "decided")).toEqual([decided]);
  expect(() =>
    readReviews(
      [
        {
          ...decided,
          resolution: { ...resolution, artifact_version: "other" },
        },
      ],
      "decided",
    ),
  ).toThrow();
  expect(() =>
    readReviews(
      [{ ...decided, resolution: { ...resolution, reviewer_id: "" } }],
      "decided",
    ),
  ).toThrow();
});
it("prepares only a local version-bound payload with no client reviewer identity", () => {
  const payload = prepareReviewDraft(
    item,
    "accepted_with_conditions",
    "  Limit use  ",
    " Review quarterly\n Keep limits ",
  );
  expect(payload).toEqual({
    artifact_id: "a1",
    artifact_version: "2",
    decision: "accepted_with_conditions",
    rationale: "Limit use",
    conditions: ["Review quarterly", "Keep limits"],
  });
  expect(payload).not.toHaveProperty("reviewer_id");
  expect(
    prepareReviewDraft(
      item,
      "changes_requested",
      "Fix evidence",
      "stale condition",
    ).conditions,
  ).toEqual([]);
  expect(() => prepareReviewDraft(item, "", "reason", "")).toThrow("Choose");
  expect(() => prepareReviewDraft(item, "accepted", " ", "")).toThrow(
    "rationale",
  );
  expect(() =>
    prepareReviewDraft(item, "accepted_with_conditions", "reason", ""),
  ).toThrow("condition");
});
it("fetches the existing review route and surfaces unavailable implementations", async () => {
  const fetcher = vi
    .fn<typeof fetch>()
    .mockResolvedValueOnce(Response.json([item]))
    .mockResolvedValueOnce(new Response(null, { status: 501 }));
  const client = createModelClient("/api/v1", fetcher);
  expect(await client.listReviews("pending")).toEqual([item]);
  expect(fetcher.mock.calls[0][0]).toBe("/api/v1/reviews?status=pending");
  await expect(client.listReviews("decided")).rejects.toMatchObject({
    status: 501,
  });
  expect(
    fetcher.mock.calls.every(
      (call) => !call[1]?.method || call[1]?.method === "GET",
    ),
  ).toBe(true);
});
it("labels waiting estimates without inventing time for future or invalid timestamps", () => {
  expect(waitingTime(item.created_at, Date.parse("2026-11-05T13:15:00Z"))).toBe(
    "1 h 15 min",
  );
  expect(waitingTime("bad")).toContain("Unavailable");
  expect(waitingTime(item.created_at, 0)).toContain("Unavailable");
});


it("reads implemented Backend review flags, blocks ineligible approval drafts, and retains audited decision timing", () => {
  const gate2 = {
    ...item,
    gate: 2,
    blockers: ["official_parity_not_passed"],
    approval_available: false,
    candidate_model_id: "candidate-1",
    requested_by: "developer-id",
  } as const;
  const read = readReviews([gate2], "pending")[0];
  expect(read.blockers).toEqual(["official_parity_not_passed"]);
  expect(read.candidate_model_id).toBe("candidate-1");
  expect(read.approval_available).toBe(false);
  expect(() => prepareReviewDraft(read, "accepted", "Looks good", "")).toThrow("unavailable");
  expect(prepareReviewDraft(read, "changes_requested", "Need parity", "").decision).toBe("changes_requested");
  expect(() => readReviews([{ ...gate2, blockers: "invalid" }], "pending")).toThrow();
  expect(() => readReviews([{ ...gate2, approval_available: "yes" }], "pending")).toThrow();
  expect(() => readReviews([{ ...gate2, candidate_model_id: 7 }], "pending")).toThrow();
  const passed = { ...read, blockers: [], approval_available: true };
  expect(prepareReviewDraft(passed, "accepted", "Parity passed", "").decision).toBe("accepted");
  const resolution = {
    ...prepareReviewDraft(item, "accepted", "Evidence reviewed", ""),
    reviewer_id: "approver-01",
    decided_at: "2026-11-07T10:00:00Z",
    review_elapsed_seconds: 141.5,
    timing_limitation: "Elapsed since opening; not active effort",
  };
  const decided = { ...item, status: "decided", resolution };
  expect(readReviews([decided], "decided")[0].resolution?.review_elapsed_seconds).toBe(141.5);
  expect(() => readReviews([{ ...decided, resolution: { ...resolution, review_elapsed_seconds: -1 } }], "decided")).toThrow();
});


it("enforces reviewer independence, Gate 3 approver and Gate 2 parity before enabling a write", () => {
  const approver = { id: "approver-1", role: "approver" } as const;
  const validator = { id: "validator-1", role: "validator" } as const;
  const developer = { id: "developer-1", role: "developer" } as const;
  const gate3 = { ...item, gate: 3 as const, requested_by: "validator-1", blockers: [] };
  const gate2 = { ...item, gate: 2 as const, blockers: ["official_parity_not_passed"], approval_available: false };
  const gate2Passed = { ...gate2, blockers: [], approval_available: true, candidate_model_id: "candidate-1" };
  expect(canDecideReview(gate3, validator)).toBe(false);
  expect(canDecideReview(gate3, approver)).toBe(true);
  expect(canDecideReview(gate3, developer)).toBe(false);
  expect(canDecideReview({ ...item, requested_by: "approver-1" }, approver)).toBe(false);
  const admin = { id: "admin-1", role: "admin" } as const;
  expect(canDecideReview(gate3, admin)).toBe(true);
  expect(canDecideReview({ ...item, requested_by: "admin-1" }, admin)).toBe(false);
  expect(canChooseReviewDecision(gate2, approver, "accepted")).toBe(false);
  expect(canChooseReviewDecision(gate2, approver, "changes_requested")).toBe(true);
  expect(canChooseReviewDecision(gate2Passed, approver, "accepted")).toBe(true);
  expect(canChooseReviewDecision({ ...item, blockers: ["failed_required_check"] }, approver, "accepted")).toBe(false);
  expect(canChooseReviewDecision({ ...item, blockers: [] }, approver, "accepted_with_conditions")).toBe(true);
});

it("recognizes changed versions, run and model checkpoints before a decision", () => {
  expect(sameReviewCheckpoint(item, { ...item })).toBe(true);
  for (const modified of [
    { ...item, artifact_id: "other" },
    { ...item, artifact_version: "new-version" },
    { ...item, run_id: "other-run" },
    { ...item, model_version: "2.0" },
    { ...item, requested_by: "different-initiator" },
  ]) expect(sameReviewCheckpoint(item, modified)).toBe(false);
});

it("GETs the current checkpoint, explicitly opens it and posts one version-bound decision", async () => {
  const payload = prepareReviewDraft(item, "accepted_with_conditions", "Restricted use", "Quarterly checks");
  const decided = {
    ...item,
    status: "decided",
    resolution: { ...payload, reviewer_id: "approver-1", decided_at: "2026-11-07T13:00:00Z",
      review_elapsed_seconds: 12, timing_limitation: "Elapsed, not active time" },
  };
  const fetcher = vi.fn<typeof fetch>()
    .mockResolvedValueOnce(Response.json(item))
    .mockResolvedValueOnce(Response.json({
      review_id: item.id, opened_at: "2026-11-07T12:59:00Z",
      measurement: "elapsed time since explicit open; not active review effort",
    }))
    .mockResolvedValueOnce(Response.json(decided));
  const client = createModelClient("/api/v1", fetcher);
  expect((await client.getReview(item.id)).artifact_version).toBe("2");
  expect((await client.openReview(item.id)).opened_at).toBe("2026-11-07T12:59:00Z");
  expect((await client.decideReview(item, payload)).resolution?.reviewer_id).toBe("approver-1");
  expect(fetcher.mock.calls.map(([path, opts]) => [path, opts?.method ?? "GET"])).toEqual([
    ["/api/v1/reviews/q1", "GET"],
    ["/api/v1/reviews/q1/open", "POST"],
    ["/api/v1/reviews/q1/decision", "POST"],
  ]);
  expect(JSON.parse(fetcher.mock.calls[2][1]?.body as string)).toEqual(payload);
  expect(fetcher.mock.calls[2][1]?.credentials).toBe("include");
  expect(JSON.parse(fetcher.mock.calls[2][1]?.body as string)).not.toHaveProperty("reviewer_id");
});

it("rejects stale response versions and preserves no-retry behavior for 403/409/uncertain decision writes", async () => {
  const payload = prepareReviewDraft(item, "accepted", "Reviewed", "");
  const stale = {
    ...item, status: "decided", artifact_version: "unrelated",
    resolution: { ...payload, artifact_version: "unrelated", reviewer_id: "approver-1", decided_at: "2026-11-07T13:00:00Z" },
  };
  const mismatched = vi.fn<typeof fetch>().mockResolvedValue(Response.json(stale));
  await expect(createModelClient("/api/v1", mismatched).decideReview(item, payload)).rejects.toThrow("submitted artifact");
  expect(mismatched).toHaveBeenCalledTimes(1);
  for (const response of [new Response(null, { status: 403 }),
    new Response(null, { status: 409 })]) {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(response);
    await expect(createModelClient("/api/v1", fetcher).decideReview(item, payload))
      .rejects.toMatchObject({ status: response.status });
    expect(fetcher).toHaveBeenCalledTimes(1);
  }
  const unknown = vi.fn<typeof fetch>().mockRejectedValue(new TypeError("Connection lost"));
  await expect(createModelClient("/api/v1", unknown).decideReview(item, payload))
    .rejects.toMatchObject({ status: 0 });
  expect(unknown).toHaveBeenCalledTimes(1);
  await expect(createModelClient("/api/v1", vi.fn<typeof fetch>()).decideReview(item, {
    ...payload, artifact_version: "wrong",
  })).rejects.toThrow("exact artifact/version");
});

it("Gate 2 requires explicit approval eligibility and candidate identity", () => {
  const user = {id:"reviewer",role:"approver"} as const;
  const eligible = {...item,gate:2 as const,approval_available:true,candidate_model_id:"candidate",blockers:[]};
  for(const missing of [{...eligible,approval_available:undefined},{...eligible,candidate_model_id:null}]) {
    expect(canChooseReviewDecision(missing,user,"accepted")).toBe(false);
    expect(()=>prepareReviewDraft(missing,"accepted","Reviewed","")).toThrow();
    expect(canChooseReviewDecision(missing,user,"changes_requested")).toBe(true);
  }
  expect(sameReviewCheckpoint(eligible,{...eligible,candidate_model_id:"another-candidate"})).toBe(false);
});
