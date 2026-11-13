import { parseQuestion, type ChatQuestion } from "./chatChoices";
import { canStartOperation, type AuthRole, type RunOperation } from "./auth";
export const chatStates = [
  "queued",
  "running",
  "completed",
  "failed",
  "cancelled",
  "interrupted",
  "awaiting_confirmation",
  "awaiting_review",
  "resolved",
] as const;
export type ChatState = (typeof chatStates)[number];
export type ChatTurn = {
  id: string;
  conversation_id: string;
  client_message_id: string;
  user_message_id: string;
  assistant_message_id: string;
  status: ChatState;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  error: string | null;
};
export type ChatMessage = {
  id: string;
  conversation_id: string;
  role: "user" | "assistant";
  content: string;
  status: ChatState;
  created_at: string;
};
export type Conversation = {
  id: string;
  actor_id: string;
  context: {
    model_id?: string | null;
    run_id?: string | null;
    report_id?: string | null;
  };
  updated_at: string;
  busy: boolean;
};
export type ChatAction = {
  id: string;
  turn_id: string;
  action_version: string;
  status: "pending" | "decided";
  kind: "run";
  operation: RunOperation;
  model_id: string;
  model_version: string;
  allowed_decisions: ("approved" | "rejected")[];
  run_id: string | null;
  continuation_id: string | null;
  parameters: null | {
    mode: "simulated";
    window_label: string;
    shift: { feature: string; factor: number };
  };
};
export type PublicChatEvent = {
  seq: number;
  kind: string;
  text?: string;
  question?: ChatQuestion;
  pending_id?: string;
  proposal_id?: string;
};
const object = (v: unknown): v is Record<string, unknown> =>
  !!v && typeof v === "object" && !Array.isArray(v);
const text = (v: unknown): v is string => typeof v === "string" && !!v.trim();
const nullable = (v: unknown) => v === null || text(v);
const date = (v: unknown) => text(v) && Number.isFinite(Date.parse(v));
const fail = (): never => {
  throw new Error("Chat response does not match the current Backend contract.");
};
export const activeTurn = (t: ChatTurn) =>
  t.status === "queued" || t.status === "running";
export const pausedTurn = (t: ChatTurn) =>
  t.status === "awaiting_confirmation" || t.status === "awaiting_review";
export function readTurn(
  v: unknown,
  conversation: string,
  id?: string,
): ChatTurn {
  if (
    !object(v) ||
    ![
      "id",
      "client_message_id",
      "user_message_id",
      "assistant_message_id",
    ].every((k) => text(v[k])) ||
    v.conversation_id !== conversation ||
    (id && v.id !== id) ||
    !chatStates.includes(v.status as ChatState) ||
    !date(v.created_at) ||
    ![v.started_at, v.finished_at].every((x) => x === null || date(x)) ||
    !(v.error === null || typeof v.error === "string")
  )
    return fail();
  return v as ChatTurn;
}
export function readMessage(
  v: unknown,
  conversation: string,
  id?: string,
): ChatMessage {
  if (
    !object(v) ||
    !text(v.id) ||
    v.conversation_id !== conversation ||
    (id && v.id !== id) ||
    !["user", "assistant"].includes(String(v.role)) ||
    typeof v.content !== "string" ||
    !chatStates.includes(v.status as ChatState) ||
    !date(v.created_at)
  )
    return fail();
  return v as ChatMessage;
}
function list<T extends { id: string }>(
  v: unknown,
  parse: (v: unknown) => T,
): T[] {
  if (!Array.isArray(v)) return fail();
  const rows = v.map(parse);
  if (new Set(rows.map((x) => x.id)).size !== rows.length) return fail();
  return rows;
}
export function readAction(v: unknown, id?: string): ChatAction {
  if (
    !object(v) ||
    !["id", "turn_id", "model_id", "model_version"].every((k) => text(v[k])) ||
    (id && v.id !== id) ||
    typeof v.action_version !== "string" ||
    !/^[a-f0-9]{64}$/.test(v.action_version) ||
    !["pending", "decided"].includes(String(v.status)) ||
    v.kind !== "run" ||
    !["document", "rebuild", "validate", "monitor"].includes(
      String(v.operation),
    ) ||
    ![v.run_id, v.continuation_id].every(nullable) ||
    !Array.isArray(v.allowed_decisions) ||
    v.allowed_decisions.some((x) => !["approved", "rejected"].includes(x)) ||
    new Set(v.allowed_decisions).size !== v.allowed_decisions.length ||
    (v.status === "decided" && v.allowed_decisions.length)
  )
    return fail();
  const p = v.parameters;
  if (v.operation === "monitor") {
    if (
      !object(p) ||
      p.mode !== "simulated" ||
      !text(p.window_label) ||
      !object(p.shift) ||
      !text(p.shift.feature) ||
      typeof p.shift.factor !== "number" ||
      !Number.isFinite(p.shift.factor) ||
      p.shift.factor <= 0 ||
      p.shift.factor > 100
    )
      return fail();
  } else if (p !== null) return fail();
  // Return only browser-approved operation parameters, not unknown extra backend fields.
  return {
    ...v,
    parameters:
      p === null
        ? null
        : {
            mode: "simulated",
            window_label: (p as Record<string, unknown>).window_label,
            shift: {
              feature: (
                (p as Record<string, unknown>).shift as Record<string, unknown>
              ).feature,
              factor: (
                (p as Record<string, unknown>).shift as Record<string, unknown>
              ).factor,
            },
          },
  } as ChatAction;
}
export function sameAction(a: ChatAction, b: ChatAction): boolean {
  return (
    [
      "id",
      "turn_id",
      "action_version",
      "kind",
      "operation",
      "model_id",
      "model_version",
    ].every((k) => a[k as keyof ChatAction] === b[k as keyof ChatAction]) &&
    JSON.stringify(a.parameters) === JSON.stringify(b.parameters)
  );
}
export function canDecideAction(
  a: ChatAction,
  role: AuthRole,
  modelId: string | null,
  outcome: string,
): boolean {
  return (
    a.status === "pending" &&
    a.model_id === modelId &&
    a.allowed_decisions.includes(outcome as "approved" | "rejected") &&
    (outcome === "rejected" ||
      (outcome === "approved" && canStartOperation(role, a.operation)))
  );
}
/** IDs only. No messages, drafts, credentials or action parameters in browser storage. */
export function chatStorageKey(user: string, model: string | null): string {
  return "mrf-chat:" + JSON.stringify([user, model]);
}

export async function consumeDurableEvents(
  response: Response,
  turn: ChatTurn,
  after: number,
  onEvent: (event: PublicChatEvent) => void,
  onCursor: (seq: number) => void,
): Promise<void> {
  if (
    !response.ok ||
    !response.body ||
    !response.headers.get("content-type")?.includes("text/event-stream")
  )
    throw new Error("Expected a chat SSE stream.");
  const reader = response.body.getReader(),
    decoder = new TextDecoder();
  let buffer = "",
    cursor = after,
    done = false;
  try {
    while (!done) {
      const part = await reader.read();
      buffer += decoder.decode(part.value, { stream: !part.done });
      buffer = buffer.replace(/\r\n/g, "\n");
      if (buffer.length > 1000000)
        throw new Error("Chat event exceeds size limit.");
      let end;
      while ((end = buffer.indexOf("\n\n")) >= 0) {
        const block = buffer.slice(0, end);
        buffer = buffer.slice(end + 2);
        const lines = block.split("\n"),
          data = lines
            .filter((x) => x.startsWith("data:"))
            .map((x) => x.slice(5).trimStart())
            .join("\n");
        if (!data) continue; // heartbeat
        const id = lines
          .find((x) => x.startsWith("id:"))
          ?.slice(3)
          .trim();
        if (!id || !/^\d+$/.test(id))
          throw new Error("Chat event is missing its replay cursor.");
        const seq = Number(id);
        if (!Number.isSafeInteger(seq) || seq <= 0) return fail();
        if (seq <= cursor) continue;
        const v: unknown = JSON.parse(data);
        if (!object(v) || !text(v.event)) return fail();
        const event: PublicChatEvent = { seq, kind: v.event };
        if (
          [
            "token",
            "tool_call",
            "confirmation_required",
            "review_required",
            "error",
          ].includes(v.event)
        ) {
          if (typeof v.text !== "string" || v.text.length > 16000)
            return fail();
          event.text = v.text;
          if (text(v.pending_id)) event.pending_id = v.pending_id;
          if (v.proposal_id !== undefined) {
            if (!text(v.proposal_id) || v.event !== "review_required") return fail();
            event.proposal_id = v.proposal_id;
          }
        } else if (v.event === "evidence") {
          if (!text(v.artifact_id)) return fail();
          event.text = "Evidence: " + v.artifact_id;
        } else if (v.event === "choices") event.question = parseQuestion(v);
        else if (v.event === "done") {
          if (
            v.assistant_message_id !== turn.assistant_message_id ||
            !chatStates.includes(v.status as ChatState)
          )
            return fail();
          done = true; // canonical text is retrieved by ID, not trusted from deltas or done.text
        } else throw new Error("Unsupported chat event. Refresh saved chat.");
        onEvent(event);
        cursor = seq;
        onCursor(seq);
      }
      if (part.done) break;
    }
    if (!done)
      throw new Error(
        "Stream disconnected; reply may be incomplete. Refresh saved chat.",
      );
  } finally {
    await reader.cancel();
    reader.releaseLock();
  }
}

type Request = (path: string, init?: RequestInit) => Promise<unknown>;
type ResponseFor = (path: string, init?: RequestInit) => Promise<Response>;
export function createDurableChatClient(
  request: Request,
  responseFor: ResponseFor,
) {
  const root = (id: string) => "/conversations/" + encodeURIComponent(id);
  const read = (path: string, signal?: AbortSignal) =>
    request(path, { signal, cache: "no-store" });
  const client = {
    async conversations(actor: string, signal?: AbortSignal) {
      return list(await read("/conversations?limit=100", signal), (v) => {
        if (
          !object(v) ||
          !text(v.id) ||
          v.actor_id !== actor ||
          !object(v.context) ||
          typeof v.busy !== "boolean" ||
          !date(v.updated_at) ||
          ["model_id", "run_id", "report_id"].some(
            (k) =>
              v.context &&
              (v.context as Record<string, unknown>)[k] !== undefined &&
              !nullable((v.context as Record<string, unknown>)[k]),
          )
        )
          return fail();
        return v as Conversation;
      });
    },
    async messages(c: string, offset = 0, signal?: AbortSignal) {
      return list(
        await read(root(c) + `/messages?offset=${offset}&limit=100`, signal),
        (v) => readMessage(v, c),
      );
    },
    async message(c: string, id: string, signal?: AbortSignal) {
      return readMessage(
        await read(root(c) + "/messages/" + encodeURIComponent(id), signal),
        c,
        id,
      );
    },
    async turns(c: string, signal?: AbortSignal, clientId?: string) {
      return list(
        await read(
          root(c) +
            "/turns?limit=100" +
            (clientId
              ? "&client_message_id=" + encodeURIComponent(clientId)
              : ""),
          signal,
        ),
        (v) => readTurn(v, c),
      );
    },
    async turn(c: string, id: string, signal?: AbortSignal) {
      return readTurn(
        await read(root(c) + "/turns/" + encodeURIComponent(id), signal),
        c,
        id,
      );
    },
    async submit(
      c: string,
      message: string,
      modelId: string | null,
      clientId: string,
      signal?: AbortSignal,
    ) {
      if (!message.trim() || message.length > 10000 || !clientId)
        throw new Error("Enter a message of at most 10,000 characters.");
      const t = readTurn(
        await request(root(c) + "/messages", {
          method: "POST",
          headers: { Accept: "application/json" },
          body: JSON.stringify({
            message,
            context: { model_id: modelId },
            client_message_id: clientId,
          }),
          signal,
        }),
        c,
      );
      if (t.client_message_id !== clientId) return fail();
      return t;
    },
    async actions(c: string, signal?: AbortSignal) {
      return list(await read(root(c) + "/pending-actions", signal), (v) =>
        readAction(v),
      );
    },
    async action(c: string, id: string, signal?: AbortSignal) {
      return readAction(
        await read(
          root(c) + "/pending-actions/" + encodeURIComponent(id),
          signal,
        ),
        id,
      );
    },
    async decide(
      c: string,
      shown: ChatAction,
      role: AuthRole,
      modelId: string | null,
      outcome: "approved" | "rejected",
      rationale: string,
      clientId: string,
      signal?: AbortSignal,
    ) {
      if (
        !rationale.trim() ||
        rationale.length > 4000 ||
        !clientId ||
        !canDecideAction(shown, role, modelId, outcome)
      )
        throw new Error(
          "This decision is not allowed or requires a rationale.",
        );
      const fresh = await client.action(c, shown.id, signal);
      if (
        !sameAction(shown, fresh) ||
        !canDecideAction(fresh, role, modelId, outcome)
      )
        throw new Error(
          "Action changed. Refresh saved chat and inspect it again.",
        );
      const t = await client.turn(c, fresh.turn_id, signal);
      if (t.status !== "awaiting_confirmation")
        throw new Error("Turn is not awaiting this confirmation.");
      const result = readAction(
        await request(
          root(c) +
            "/pending-actions/" +
            encodeURIComponent(shown.id) +
            "/decision",
          {
            method: "POST",
            body: JSON.stringify({
              client_decision_id: clientId,
              action_version: shown.action_version,
              outcome,
              rationale: rationale.trim(),
            }),
            signal,
          },
        ),
        shown.id,
      );
      if (!sameAction(shown, result) || result.status !== "decided")
        return fail();
      return result;
    },
    async cancel(c: string, id: string, signal?: AbortSignal) {
      return readTurn(
        await request(
          root(c) + "/turns/" + encodeURIComponent(id) + "/cancel",
          { method: "POST", signal },
        ),
        c,
        id,
      );
    },
    async events(
      t: ChatTurn,
      after: number,
      onEvent: (e: PublicChatEvent) => void,
      onCursor: (n: number) => void,
      signal?: AbortSignal,
    ) {
      const res = await responseFor(
        root(t.conversation_id) +
          "/turns/" +
          encodeURIComponent(t.id) +
          "/events?after_seq=" +
          after,
        { headers: { Accept: "text/event-stream" }, cache: "no-store", signal },
      );
      await consumeDurableEvents(res, t, after, onEvent, onCursor);
    },
  };
  return client;
}
