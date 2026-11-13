import { expect, test } from "vitest";
import { compactChatActivity } from "./chatActivity";

test("groups identical tool labels while counting distinct events only", () => {
  const result = compactChatActivity([
    {seq: 1, kind: "tool_call", text: "Using a permitted platform tool..."},
    {seq: 1, kind: "tool_call", text: "Using a permitted platform tool..."},
    {seq: 2, kind: "tool_call", text: "Using a permitted platform tool..."},
    {seq: 3, kind: "error", text: "Failed"},
  ]);
  expect(result.toolCount).toBe(2);
  expect(result.tools).toEqual([{text: "Using a permitted platform tool...", count: 2}]);
  expect(result.notices).toEqual([{seq: 3, kind: "error", text: "Failed"}]);
});
test("shows each proposal once without merging separate proposals or confirmations", () => {
  const result = compactChatActivity([
    {seq: 1, kind: "review_required", proposal_id: "p1", text: "Review"},
    {seq: 2, kind: "review_required", proposal_id: "p1", text: "Review"},
    {seq: 3, kind: "review_required", proposal_id: "p2", text: "Review"},
    {seq: 4, kind: "confirmation_required", text: "Confirm"},
  ]);
  expect(result.notices.map(e => e.seq)).toEqual([1, 3, 4]);
});
