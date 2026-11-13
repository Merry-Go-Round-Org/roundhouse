import { it, expect } from "vitest";
import { consumeChat } from "./chatStream";
import { readRun } from "./api";
import { parseQuestion } from "./chatChoices";

const question = {
  event: "choices",
  id: "validation-scope",
  question: "Which checks should we discuss?",
  options: [
    { id: "performance", label: "Performance" },
    { id: "fairness", label: "Fairness limitations" },
  ],
};
it("delivers validated agent choices only after a completed stream", async () => {
  const received: unknown[] = [];
  await consumeChat(
    stream(
      `data: ${JSON.stringify(question)}\r\n\r\ndata: {"event":"done"}\r\n\r\n`,
    ),
    () => {},
    (q) => received.push(q),
  );
  expect(received).toEqual([parseQuestion(question)]);
});
it("does not offer choices from an interrupted answer", async () => {
  const received: unknown[] = [];
  await expect(
    consumeChat(
      stream(`data: ${JSON.stringify(question)}\n\n`),
      () => {},
      (q) => received.push(q),
    ),
  ).rejects.toThrow("incomplete");
  expect(received).toEqual([]);
});
it("rejects malformed, duplicate and excessive choices", () => {
  for (const options of [
    [],
    [null, null],
    [question.options[0], question.options[0]],
    Array.from({ length: 7 }, (_, i) => ({ id: String(i), label: "Option" })),
    [{ id: "a", label: " " }, question.options[1]],
  ]) {
    expect(() => parseQuestion({ ...question, options })).toThrow(
      "Invalid chat choices",
    );
  }
});
function stream(text: string) {
  const encoder = new TextEncoder();
  return new Response(
    new ReadableStream({
      start(c) {
        for (const char of text) c.enqueue(encoder.encode(char));
        c.close();
      },
    }),
    { headers: { "Content-Type": "text/event-stream" } },
  );
}
it("handles fragmented SSE tokens and explicit completion", async () => {
  let text = "";
  await consumeChat(
    stream(
      'event: token\r\ndata: {"text":"Hello"}\r\n\r\ndata: {"event":"done"}\r\n\r\n',
    ),
    (v) => (text += v),
  );
  expect(text).toBe("Hello");
});
it("marks truncated streams incomplete", async () => {
  await expect(
    consumeChat(
      stream('data: {"event":"token","text":"partial"}\n\n'),
      () => {},
    ),
  ).rejects.toThrow("incomplete");
});
it("reports a backend agent error", async () => {
  await expect(
    consumeChat(
      stream('data: {"event":"error","text":"Provider unavailable"}\n\n'),
      () => {},
    ),
  ).rejects.toThrow("Provider unavailable");
});
it("does not fabricate replies for unavailable routes", async () => {
  await expect(
    consumeChat(new Response("", { status: 501 }), () => {}),
  ).rejects.toThrow("not implemented");
});
it("rejects approval as a run execution state", () => {
  expect(() => readRun({ id: "r", status: "approved" })).toThrow("Invalid");
});

it("displays public tool steps and pending decisions without exposing raw harness data", async () => {
  const notices: unknown[] = [];
  await consumeChat(
    stream(
      [
        'data: {"event":"tool_call","text":"Read inventory","arguments":{"secret":"hidden"}}',
        'data: {"event":"confirmation_required","pending_id":"p1","data":{"reasoning":"hidden"}}',
        'data: {"event":"review_required","text":"Review this draft"}',
        'data: {"event":"done"}',
      ].join("\n\n") + "\n\n",
    ),
    () => {},
    undefined,
    (n) => notices.push(n),
  );
  expect(notices).toHaveLength(3);
  expect(notices[0]).toEqual({ kind: "tool_call", text: "Read inventory" });
  expect(JSON.stringify(notices)).not.toContain("hidden");
});
it("keeps a pending-decision notice visible even if the stream then disconnects", async () => {
  const notices: unknown[] = [];
  await expect(
    consumeChat(
      stream('data: {"event":"confirmation_required"}\n\n'),
      () => {},
      undefined,
      (n) => notices.push(n),
    ),
  ).rejects.toThrow("incomplete");
  expect(notices).toHaveLength(1);
});
