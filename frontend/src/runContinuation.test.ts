import { expect, it, vi } from "vitest";
import { createModelClient } from "./api";
import { loadContinuation, submitContinuation } from "./continuation";
const user = { id: "operator", role: "developer" as const };
const run = {id:"r1",model_id:"m1",model_version:"v1",dataset_id:"uci_credit",operation:"rebuild",status:"awaiting_review",stage:"awaiting_review",created_at:"2026-11-07T10:00:00Z",started_at:null,finished_at:null,error:null};
const review = {id:"review1",run_id:"r1",model_id:"m1",model_name:"Credit",model_version:"v1",gate:2,status:"decided",artifact_id:"a1",artifact_version:"v1",created_at:"2026-11-07T10:00:00Z",requested_by:"operator",resolution:{artifact_id:"a1",artifact_version:"v1",decision:"changes_requested",rationale:"Revise the candidate",conditions:[],reviewer_id:"independent",decided_at:"2026-11-07T11:00:00Z"}};
function fixture() {
  const state = { run: structuredClone(run), review: structuredClone(review), taskId:"t1", checkpoint:"review1", status:202 };
  const transport = vi.fn<typeof fetch>(async (url, init) => {
    const path = String(url);
    if (init?.method === "POST") return new Response(JSON.stringify(state.status === 202 ? {...state.run,status:"queued",stage:"resuming"} : {detail:"Continuation saved; recovery will retry."}), {status:state.status});
    return new Response(JSON.stringify(path.endsWith("/result") ? {run_id:"r1",result:{status:"awaiting_review",task_id:state.taskId,checkpoint_id:state.checkpoint}} : path.includes("/reviews/") ? state.review : state.run));
  });
  return {state,transport,api:createModelClient("/api/v1",transport)};
}
it("resumes an exact decided checkpoint with only review_id and cookie identity", async () => {
  const {api,transport} = fixture();
  const shown = await loadContinuation(api,"r1","m1",user);
  expect((await submitContinuation(api,shown,user)).status).toBe("queued");
  const posts = transport.mock.calls.filter(([,init]) => init?.method === "POST");
  expect(posts).toHaveLength(1);
  expect(posts[0][0]).toBe("/api/v1/runs/r1/resume");
  expect(posts[0][1]?.body).toBe(JSON.stringify({review_id:"review1"}));
  expect(posts[0][1]?.credentials).toBe("include");
});
it("blocks another actor, pending review and completed run without writes", async () => {
  const {api,state,transport} = fixture();
  await expect(loadContinuation(api,"r1","m1",{...user,id:"someone"})).rejects.toThrow("initiating");
  state.run.status = "completed";
  await expect(loadContinuation(api,"r1","m1",user)).rejects.toThrow("paused");
  state.run.status = "awaiting_review"; state.review.status = "pending";
  // A pending response has no resolution; parser must reject this malformed fixture too.
  await expect(loadContinuation(api,"r1","m1",user)).rejects.toThrow();
  expect(transport.mock.calls.filter(([,init]) => init?.method === "POST")).toHaveLength(0);
});
it("rechecks task and decision immediately before write", async () => {
  for (const change of ["task", "rationale", "version"]) {
    const {api,state,transport} = fixture();
    const shown = await loadContinuation(api,"r1","m1",user);
    if (change === "task") state.taskId = "new-task";
    if (change === "rationale") state.review.resolution.rationale = "Changed rationale";
    if (change === "version") state.review.model_version = "v2";
    await expect(submitContinuation(api,shown,user)).rejects.toThrow();
    expect(transport.mock.calls.filter(([,init]) => init?.method === "POST")).toHaveLength(0);
  }
});
it("never retries denied, stale or uncertain resume submissions", async () => {
  for (const status of [403,409,503]) {
    const {api,state,transport} = fixture();
    const shown = await loadContinuation(api,"r1","m1",user); state.status = status;
    await expect(submitContinuation(api,shown,user)).rejects.toThrow();
    expect(transport.mock.calls.filter(([,init]) => init?.method === "POST")).toHaveLength(1);
  }
});
it("blocks a mismatched model and resumes an accepted Gate 2 decision", async () => {
  const {api,state} = fixture();
  await expect(loadContinuation(api,"r1","other",user)).rejects.toThrow("another model");
  // The Backend accepts Gate 2 only after a passing official parity run on the candidate.
  state.review.resolution.decision = "accepted";
  expect((await loadContinuation(api,"r1","m1",user)).review.resolution?.decision).toBe("accepted");
});
