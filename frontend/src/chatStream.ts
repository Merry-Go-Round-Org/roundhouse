import { parseQuestion, type ChatQuestion } from "./chatChoices";

export type ChatNotice = {
  kind: "tool_call" | "confirmation_required" | "review_required";
  text: string;
};
// Display-only support for issue #10 event names. No pending action is executed
// until the backend defines an authenticated confirmation endpoint and payload.
export async function consumeChat(
  res: Response,
  onText: (text: string) => void,
  onQuestion?: (question: ChatQuestion) => void,
  onNotice?: (notice: ChatNotice) => void,
) {
  if (!res.ok)
    throw new Error(
      res.status === 501
        ? "Agent chat is not implemented by the backend yet."
        : `Chat request failed (${res.status}).`,
    );
  if (
    !res.body ||
    !res.headers.get("content-type")?.includes("text/event-stream")
  )
    throw new Error("Expected an SSE stream.");
  const reader = res.body.getReader(),
    decoder = new TextDecoder();
  let buffer = "",
    done = false;
  let question: ChatQuestion | undefined;
  try {
    while (!done) {
      const part = await reader.read();
      if (part.done) break;
      buffer += decoder.decode(part.value, { stream: true });
      buffer = buffer.replace(/\r\n/g, "\n");
      if (buffer.length > 1000000)
        throw new Error("Stream event exceeds size limit.");
      let end;
      while ((end = buffer.indexOf("\n\n")) >= 0) {
        const block = buffer.slice(0, end);
        buffer = buffer.slice(end + 2);
        const lines = block.split("\n"),
          data = lines
            .filter((l) => l.startsWith("data:"))
            .map((l) => l.slice(5).trimStart())
            .join("\n");
        if (!data) continue;
        const value = JSON.parse(data),
          kind =
            value.event ??
            lines
              .find((l) => l.startsWith("event:"))
              ?.slice(6)
              .trim();
        if (kind === "token" && typeof value.text === "string")
          onText(value.text);
        else if (kind === "evidence" && typeof value.artifact_id === "string")
          onText("\nEvidence: " + value.artifact_id);
        else if (
          ["tool_call", "confirmation_required", "review_required"].includes(
            kind,
          )
        ) {
          const defaults = {
            tool_call: "Agent requested a tool. No public summary supplied.",
            confirmation_required:
              "An action needs your confirmation. No action was confirmed by this frontend.",
            review_required:
              "An authenticated reviewer must decide. Chat text cannot approve this item.",
          };
          const noticeKind = kind as ChatNotice["kind"];
          // Only the browser-facing public text is shown, never raw harness data,
          // arguments, tool results or reasoning.
          const publicText =
            typeof value.text === "string" && value.text.trim()
              ? value.text
              : defaults[noticeKind];
          if (publicText.length > 10000)
            throw new Error("Chat notice exceeds size limit.");
          onNotice?.({ kind: noticeKind, text: publicText });
        } else if (kind === "choices") question = parseQuestion(value);
        else if (kind === "done") {
          done = true;
          break;
        } else if (kind === "error")
          throw new Error(
            typeof value.text === "string" ? value.text : "Agent error",
          );
        else throw new Error("Unsupported stream event.");
      }
    }
    if (!done)
      throw new Error(
        "Stream disconnected before completion; reply may be incomplete.",
      );
    if (question) onQuestion?.(question);
  } finally {
    await reader.cancel();
    reader.releaseLock();
  }
}
