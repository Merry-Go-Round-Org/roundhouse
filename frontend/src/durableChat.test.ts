import { it, expect, vi } from "vitest";
import { createModelClient } from "./api";
import {
  readTurn,
  readMessage,
  readAction,
  consumeDurableEvents,
  canDecideAction,
  chatStorageKey,
  type ChatTurn,
} from "./durableChat";
const turn: ChatTurn = {
  id: "t1",
  conversation_id: "c1",
  client_message_id: "client1",
  user_message_id: "u1",
  assistant_message_id: "a1",
  status: "awaiting_confirmation",
  created_at: "2026-11-07T12:00:00Z",
  started_at: null,
  finished_at: null,
  error: null,
};
const action = {
  id: "p1",
  turn_id: "t1",
  action_version: "a".repeat(64),
  status: "pending",
  kind: "run",
  operation: "document",
  model_id: "m1",
  model_version: "1",
  allowed_decisions: ["approved", "rejected"],
  run_id: null,
  continuation_id: null,
  parameters: null,
};
it("rejects wrong conversation/message identity and malformed durable records", () => {
  expect(() => readTurn({ ...turn, conversation_id: "other" }, "c1")).toThrow();
  expect(() => readTurn({ ...turn, status: "approved" }, "c1")).toThrow();
  expect(() =>
    readMessage(
      {
        id: "a1",
        conversation_id: "other",
        role: "assistant",
        content: "private",
        status: "completed",
        created_at: turn.created_at,
      },
      "c1",
    ),
  ).toThrow();
  expect(() => readAction({ ...action, action_version: "old" })).toThrow();
  expect(() =>
    readAction({ ...action, operation: "monitor", parameters: null }),
  ).toThrow();
  expect(() => readAction({ ...action, status: "decided" })).toThrow();
});
it("uses one idempotent JSON submission through the shared credentialed transport", async () => {
  const f = vi.fn<typeof fetch>().mockResolvedValue(Response.json(turn));
  await createModelClient("/api/v1", f).chat.submit(
    "c1",
    "Help",
    "m1",
    "client1",
  );
  expect(f).toHaveBeenCalledTimes(1);
  expect(f.mock.calls[0][1]).toMatchObject({
    credentials: "include",
    method: "POST",
    headers: { Accept: "application/json" },
  });
  expect(JSON.parse(f.mock.calls[0][1]!.body as string)).toEqual({
    message: "Help",
    context: { model_id: "m1" },
    client_message_id: "client1",
  });
});
it("replays cursors, discards duplicates, strips unknown tool fields and does not append done text", async () => {
  const frames = [
    { seq: 1, event: "token", text: "partial" },
    { seq: 1, event: "token", text: "duplicate" },
    {
      seq: 8,
      event: "tool_call",
      text: "Public summary",
      arguments: { secret: "hidden" },
    },
    {
      seq: 11,
      event: "done",
      assistant_message_id: "a1",
      status: "completed",
      text: "full text from persisted store",
    },
  ];
  const body = frames
    .map(({ seq, ...v }) => `id: ${seq}\ndata: ${JSON.stringify(v)}\n\n`)
    .join("");
  const events: unknown[] = [],
    cursors: number[] = [];
  await consumeDurableEvents(
    new Response(body, { headers: { "content-type": "text/event-stream" } }),
    turn,
    0,
    (e) => events.push(e),
    (n) => cursors.push(n),
  );
  expect(cursors).toEqual([1, 8, 11]); // global sequence gaps are valid, not per-turn counters
  expect(JSON.stringify(events)).not.toContain("hidden");
  expect(JSON.stringify(events)).not.toContain("full text");
  expect(JSON.stringify(events)).not.toContain("duplicate");
});
it("rejects missing cursors and done for the wrong assistant; truncation is not completion", async () => {
  for (const body of [
    'data: {"event":"token","text":"hi"}\n\n',
    'id: 1\ndata: {"event":"done","assistant_message_id":"other","status":"completed"}\n\n',
    'id: 1\ndata: {"event":"token","text":"partial"}\n\n',
  ]) {
    await expect(
      consumeDurableEvents(
        new Response(body, {
          headers: { "content-type": "text/event-stream" },
        }),
        turn,
        0,
        () => {},
        () => {},
      ),
    ).rejects.toThrow();
  }
});
it("rechecks action and parent before one exact decision POST without reviewer identity", async () => {
  const f = vi
    .fn<typeof fetch>()
    .mockResolvedValueOnce(Response.json(action))
    .mockResolvedValueOnce(Response.json(turn))
    .mockResolvedValueOnce(
      Response.json({
        ...action,
        status: "decided",
        allowed_decisions: [],
        run_id: "run1",
        continuation_id: "t2",
      }),
    );
  await createModelClient("/api/v1", f).chat.decide(
    "c1",
    readAction(action),
    "developer",
    "m1",
    "approved",
    "Proceed",
    "d1",
  );
  expect(f.mock.calls.filter(([, i]) => i?.method === "POST")).toHaveLength(1);
  expect(JSON.parse(f.mock.calls[2][1]!.body as string)).toEqual({
    client_decision_id: "d1",
    action_version: action.action_version,
    outcome: "approved",
    rationale: "Proceed",
  });
});
it("blocks changed parameters, checkpoint or unauthorized operation before a decision POST", async () => {
  for (const changed of [
    { ...action, action_version: "b".repeat(64) },
    { ...action, model_version: "2" },
    { ...action, status: "decided", allowed_decisions: [] },
  ]) {
    const f = vi.fn<typeof fetch>().mockResolvedValue(Response.json(changed));
    await expect(
      createModelClient("/api/v1", f).chat.decide(
        "c1",
        readAction(action),
        "developer",
        "m1",
        "approved",
        "Proceed",
        "d1",
      ),
    ).rejects.toThrow();
    expect(f.mock.calls.some(([, i]) => i?.method === "POST")).toBe(false);
  }
  expect(
    canDecideAction(readAction(action), "approver", "m1", "approved"),
  ).toBe(false);
  expect(
    canDecideAction(readAction(action), "developer", "other", "approved"),
  ).toBe(false);
});
it("does not retry decision writes after HTTP conflict or connection loss", async () => {
  for (const status of [403, 409, 503]) {
    const f = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(Response.json(action))
      .mockResolvedValueOnce(Response.json(turn))
      .mockResolvedValueOnce(Response.json({ detail: "denied" }, { status }));
    await expect(
      createModelClient("/api/v1", f).chat.decide(
        "c1",
        readAction(action),
        "developer",
        "m1",
        "approved",
        "Proceed",
        "d1",
      ),
    ).rejects.toThrow();
    expect(f.mock.calls.filter(([, i]) => i?.method === "POST")).toHaveLength(
      1,
    );
  }
});
it("conversation lists are actor-bound and storage keys separate user/model contexts", async () => {
  const f = vi
    .fn<typeof fetch>()
    .mockResolvedValue(
      Response.json([
        {
          id: "c1",
          actor_id: "other",
          context: {},
          busy: false,
          updated_at: turn.created_at,
        },
      ]),
    );
  await expect(
    createModelClient("/api/v1", f).chat.conversations("user1"),
  ).rejects.toThrow();
  expect(chatStorageKey("u1", "m1")).not.toBe(chatStorageKey("u2", "m1"));
  expect(chatStorageKey("u1", "m1")).not.toBe(chatStorageKey("u1", null));
});
