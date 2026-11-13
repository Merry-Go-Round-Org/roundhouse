import { OperationStatusCard } from "./OperationStatusCard";
import { compactChatActivity } from "./chatActivity";
import { LiveProposal, LiveProposals } from "./LiveProposals";
import { ChatMarkdown } from "./ChatMarkdown";
import { ChatReviews } from "./ChatReviews";
import { useEffect, useRef, useState } from "react";
import { createModelClient, type ModelRecord } from "./api";
import { useAuth } from "./AuthContext";
import {
  activeTurn,
  pausedTurn,
  canDecideAction,
  chatStorageKey,
  type ChatAction,
  type ChatMessage,
  type ChatTurn,
  type Conversation,
  type PublicChatEvent,
} from "./durableChat";
import type { ChatQuestion } from "./chatChoices";
const api = createModelClient();
export function DurableAgentPanel({ model }: { model: ModelRecord | null }) {
  const { user } = useAuth();
  return (
    <ChatWorkspace key={JSON.stringify([user.id, model?.id])} model={model} />
  );
}
function ChatWorkspace({ model }: { model: ModelRecord | null }) {
  const { user } = useAuth(),
    modelId = model?.id ?? null,
    storageKey = chatStorageKey(user.id, modelId);
  const stored = () => {
    try {
      return sessionStorage.getItem(storageKey) || "";
    } catch {
      return "";
    }
  };
  const [conversation, setConversation] = useState(stored),
    conversationRef = useRef(conversation);
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [messages, setMessages] = useState<ChatMessage[]>([]),
    [turns, setTurns] = useState<ChatTurn[]>([]),
    [actions, setActions] = useState<ChatAction[]>([]);
  const [text, setText] = useState(""),
    [error, setError] = useState(""),
    [info, setInfo] = useState(""),
    [busy, setBusy] = useState(false),
    [loaded, setLoaded] = useState(!conversation);
  const [events, setEvents] = useState<PublicChatEvent[]>([]),
    [provisional, setProvisional] = useState<{ turnId: string; conversationId: string; messageId: string; text: string } | null>(null),
    [question, setQuestion] = useState<ChatQuestion | null>(null);
  const [offset, setOffset] = useState(0),
    [pageFull, setPageFull] = useState(false),
    [generation, setGeneration] = useState(0);
  const [uncertain, setUncertain] = useState<string | null>(() => {
    try {
      return sessionStorage.getItem(storageKey + ":submission");
    } catch {
      return null;
    }
  });
  const [cancelConfirm, setCancelConfirm] = useState(false);
  const [showProposals, setShowProposals] = useState(false);
  const lock = useRef(false),
    controller = useRef<AbortController | null>(null),
    alive = useRef(true),
    cursor = useRef<Record<string, number>>({});
  const blocked =
    !!uncertain ||
    !loaded ||
    turns.some((t) => activeTurn(t) || pausedTurn(t)) ||
    actions.some((a) => a.status === "pending");
  function prepareRequest(request: string) {
    setText(old => old.trim() ? `${old}\n\n${request}` : request);
    setInfo("Retry request added to your message draft. Review and send it; no operation has started.");
  }
  function remember(id: string) {
    conversationRef.current = id;
    setConversation(id);
    try {
      sessionStorage.setItem(storageKey, id);
    } catch {
      /* server selector still works */
    }
  }
  function markUncertain(id: string | null) {
    setUncertain(id);
    try {
      if (id) sessionStorage.setItem(storageKey + ":submission", id);
      else sessionStorage.removeItem(storageKey + ":submission");
    } catch {
      /* retain state in memory */
    }
  }
  function clearView() {
    setShowProposals(false);
    setMessages([]);
    setTurns([]);
    setActions([]);
    setEvents([]);
    setProvisional(null);
    cursor.current = {};
    setQuestion(null);
    setCancelConfirm(false);
    setGeneration((v) => v + 1);
  }
  async function task(work: (signal: AbortSignal) => Promise<void>) {
    if (lock.current) return;
    lock.current = true;
    setBusy(true);
    setError("");
    setInfo("");
    const c = new AbortController();
    controller.current = c;
    try {
      await work(c.signal);
    } catch (e) {
      if (alive.current) {
        setError(
          c.signal.aborted
            ? "Observation stopped locally; server work may continue. Refresh saved chat."
            : (e as Error).message,
        );
        setLoaded(false);
      }
    } finally {
      lock.current = false;
      if (alive.current) setBusy(false);
    }
  }
  async function snapshot(id: string, signal: AbortSignal, page = offset) {
    setLoaded(false);
    setActions([]);
    setGeneration((v) => v + 1);
    const [history, savedTurns, pending] = await Promise.all([
      api.chat.messages(id, page, signal),
      api.chat.turns(id, signal),
      api.chat.actions(id, signal),
    ]);
    // Retrieve canonical latest assistant text separately even if not on this history page.
    const latest = savedTurns[0];
    const canonical = latest
      ? await api.chat.message(id, latest.assistant_message_id, signal)
      : null;
    if (signal.aborted) return { turns: savedTurns, canonical };
    if (canonical && canonical.role !== "assistant")
      throw new Error("Canonical assistant message has the wrong role.");
    setMessages(
      canonical && !history.some((m) => m.id === canonical.id)
        ? [...history, canonical]
        : history.map((m) => (canonical?.id === m.id ? canonical : m)),
    );
    setPageFull(history.length === 100);
    setTurns(savedTurns);
    setActions(pending);
    setLoaded(true);
    // A terminal empty message cannot replace a preview. Keep it explicitly
    // unsaved in this mounted chat only; never promote deltas to canonical text.
    setProvisional(previous => {
      if (!previous || previous.conversationId !== id) return null;
      if (latest && latest.id !== previous.turnId) return null;
      return canonical?.id === previous.messageId && canonical.content.trim() &&
        latest && !activeTurn(latest) ? null : previous;
    });
    setOffset(page);
    return { turns: savedTurns, canonical };
  }
  async function observe(turn: ChatTurn, signal: AbortSignal) {
    setProvisional(previous => previous?.turnId === turn.id && previous.conversationId === turn.conversation_id ? previous : null);
    setEvents([]);
    setQuestion(null);
    let streamError: Error | null = null;
    try {
      await api.chat.events(
        turn,
        cursor.current[turn.id] ?? 0,
        (event) => {
          if (signal.aborted) return;
          if (event.kind === "token")
            setProvisional(previous => ({
              turnId: turn.id, conversationId: turn.conversation_id,
              messageId: turn.assistant_message_id,
              text: ((previous?.turnId === turn.id ? previous.text : "") + (event.text ?? "")).slice(-100000),
            }));
          else if (event.kind === "choices")
            setQuestion(event.question ?? null);
          else if (event.kind !== "done")
            setEvents((v) => [...v, event].slice(-50));
        },
        (seq) => {
          cursor.current[turn.id] = seq;
        },
        signal,
      );
    } catch (e) {
      streamError = e as Error;
    }
    if (signal.aborted) return;
    const refreshed = await snapshot(turn.conversation_id, signal);
    if (streamError && !signal.aborted) {
      const saved = refreshed.turns.find((t) => t.id === turn.id);
      const hasReply = refreshed.canonical?.id === turn.assistant_message_id && !!refreshed.canonical.content.trim();
      setInfo(
        saved && !activeTurn(saved)
          ? `Saved turn status: ${saved.status}. ${hasReply ? "The saved reply is displayed." : "The server has no saved reply text. Any streaming preview below is incomplete and unsaved."} No message was resubmitted.`
          : "Stream disconnected; reply may be incomplete. Refresh saved chat or Resume stream. No message was resubmitted.",
      );
    }
  }
  async function restore(id: string, signal: AbortSignal, page = offset) {
    const saved = await snapshot(id, signal, page);
    if (uncertain) {
      const found = await api.chat.turns(id, signal, uncertain);
      if (signal.aborted) return;
      if (found.length) markUncertain(null);
      else
        setInfo(
          "No saved turn found for the uncertain submission yet. Refresh again before deliberately starting a new conversation; no write was retried.",
        );
    }
    return saved;
  }
  useEffect(() => {
    alive.current = true;
    const timer = setTimeout(() => {
      if (conversationRef.current)
        void task(async (signal) => {
          await restore(conversationRef.current, signal, 0);
        });
    }, 0);
    return () => {
      clearTimeout(timer);
      alive.current = false;
      controller.current?.abort();
    };
  }, []);
  function selectConversation(id: string) {
    clearView();
    remember(id);
    markUncertain(null);
    setOffset(0);
    setLoaded(!id);
    if (id)
      void task(async (signal) => {
        await snapshot(id, signal, 0);
      });
  }
  const current = turns.find(activeTurn),
    paused = turns.find(pausedTurn);
  const recoveryAttempts = useRef<Record<string, number>>({});
  useEffect(() => {
    if (!current || busy || !loaded || uncertain || error || actions.some(a => a.status === "pending")) return;
    if ((recoveryAttempts.current[current.id] ?? 0) >= 3) return;
    let timer: ReturnType<typeof setTimeout>;
    const recover = () => {
      if (document.visibilityState !== "visible") {
        timer = setTimeout(recover, 5000);
        return;
      }
      if (lock.current) return;
      recoveryAttempts.current[current.id] = (recoveryAttempts.current[current.id] ?? 0) + 1;
      void task(async signal => {
        const saved = await snapshot(conversation, signal);
        const active = saved.turns.find(t => t.id === current.id && activeTurn(t));
        if (!signal.aborted && active) await observe(active, signal);
      });
    };
    timer = setTimeout(recover, 5000);
    return () => clearTimeout(timer);
  }, [current, busy, loaded, uncertain, error, conversation, actions]);
  const activity = compactChatActivity(events);
  return (
    <aside className="panel agent-panel durable-chat" aria-label="Agent chat">
      <div className="chat-scroll-area">
      <div className="panel-title chat-heading">
        <span className="chat-avatar" aria-hidden="true">✦</span>
        <div><h2>Agent chat</h2>
        <p>{model ? "Working with " + model.name : "Your model workflow assistant"}</p></div>
      </div>
      <div className="evidence-actions chat-toolbar">
        <button
          disabled={busy}
          onClick={() =>
            void task(async (signal) => {
              const rows = await api.chat.conversations(user.id, signal);
              if (!signal.aborted)
                setConversations(
                  rows.filter(
                    (c) =>
                      (c.context.model_id ?? null) === modelId &&
                      !c.context.run_id &&
                      !c.context.report_id,
                  ),
                );
            })
          }
        >
          Load saved conversations
        </button>
        <button
          disabled={busy}
          onClick={() => {
            selectConversation("");
            setText("");
          }}
        >
          New conversation
        </button>
      </div>
      {conversations.length > 0 && (
        <label>
          Saved conversation
          <select
            aria-label="Saved conversation"
            disabled={busy}
            value={conversation}
            onChange={(e) => selectConversation(e.target.value)}
          >
            <option value="">Choose conversation</option>
            {conversations.map((c) => (
              <option value={c.id} key={c.id}>
                {c.id} · {c.updated_at}
              </option>
            ))}
          </select>
        </label>
      )}
      <details className="chat-session-details"><summary>Conversation details</summary><p>
        Conversation: {conversation || "Not created yet"}. Saved list shows at
        most 100 recent conversations in this model context. New conversation
        leaves existing work running.
      </p></details>
      {conversation && (
        <div className="evidence-actions chat-toolbar">
          <button
            disabled={busy}
            onClick={() =>
              void task(async (signal) => {
                await restore(conversation, signal);
              })
            }
          >
            Refresh saved chat
          </button>
          <button
            disabled={busy || offset === 0}
            onClick={() =>
              void task(async (signal) => {
                await snapshot(conversation, signal, Math.max(0, offset - 100));
              })
            }
          >
            Previous messages
          </button>
          <button
            disabled={busy || !pageFull}
            onClick={() =>
              void task(async (signal) => {
                await snapshot(conversation, signal, offset + 100);
              })
            }
          >
            Next messages
          </button>
        </div>
      )}
      <details className="chat-session-details"><summary>History and recovery details</summary><p>
        History page {offset / 100 + 1}; up to 100 messages plus the latest
        assistant reply. Turn status shows up to 100 recent turns. Messages are
        restored from the server; this tab stores only conversation/submission
        IDs.
      </p></details>
      <section className="chat-transcript" role="log" aria-label="Saved chat messages" aria-live="polite">
        {messages.length ? (
          messages.map((m) => (
            <article key={m.id} className={`chat-message chat-message-${m.role}`}>
              <h3>
                {m.role === "user" ? "You" : m.role === "assistant" ? "Agent" : m.role} <span>{m.status}</span>
              </h3>
              {m.role === "assistant" ? (
                <ChatMarkdown text={m.content || "No saved text yet."} />
              ) : (
                <p className="chat-text">{m.content || "No saved text yet."}</p>
              )}
            </article>
          ))
        ) : (
          <div className="chat-empty"><span aria-hidden="true">✦</span><h3>How can I help?</h3><p>Ask about your models, explore evidence, or plan your next step.</p><small>Choose a suggestion below to get started.</small></div>
        )}
      </section>
      {provisional?.text && (
        <section className="chat-message chat-message-assistant chat-streaming" aria-label="Provisional reply">
          <h3>Unsaved streaming preview — may be incomplete</h3>
          <p>This preview is kept only while this chat remains open. Saved replies replace it when available.</p>
          <ChatMarkdown text={provisional.text} />
        </section>
      )}
      {turns[0] && (
        <p>
          Response status: {turns[0].status}
          {turns[0].error ? " · " + turns[0].error : ""}
        </p>
      )}
      {current && (
        <div className="lifecycle-card">
          <p>
            The agent is working. You can return to this conversation later.
          </p>
          <button
            disabled={busy}
            onClick={() =>
              void task(async (signal) => {
                await observe(current, signal);
              })
            }
          >
            Resume stream
          </button>
          <label>
            <input
              type="checkbox"
              disabled={busy}
              checked={cancelConfirm}
              onChange={(e) => setCancelConfirm(e.target.checked)}
            />
            Request server cancellation of this turn
          </label>
          <button
            disabled={busy || !cancelConfirm}
            onClick={() =>
              void task(async (signal) => {
                setCancelConfirm(false);
                await api.chat.cancel(conversation, current.id, signal);
                await snapshot(conversation, signal);
                if (!signal.aborted)
                  setInfo(
                    "Cancellation requested. Refresh saved chat for the actual outcome; completion may win the race.",
                  );
              })
            }
          >
            Cancel server turn
          </button>
        </div>
      )}
      {events.length > 0 && (
        <section aria-label="Agent activity">
          <h3>Agent activity</h3>
          {activity.tools.length > 0 && (
            <details className="help-details">
              <summary>Platform tool activity · {activity.toolCount} updates</summary>
              <ul>{activity.tools.map((tool) => <li key={tool.text}>{tool.text}{tool.count > 1 ? ` (${tool.count} updates)` : ""}</li>)}</ul>
            </details>
          )}
          {activity.notices.map((e) => (
            <article key={e.seq}>
              <h4>
                {e.kind === "confirmation_required"
                  ? "Confirmation required"
                  : e.kind === "review_required"
                    ? "Review required"
                    : "Agent update"}
              </h4>
              <p>{e.text}</p>
              {e.proposal_id && <LiveProposal key={e.proposal_id} id={e.proposal_id} modelId={modelId??undefined} conversationId={conversation} onRequest={prepareRequest}/>}
            </article>
          ))}
        </section>
      )}
      {paused && (
        <p role="status">
          This conversation is paused for a decision. Chat text and suggested
          answers cannot approve an action.{" "}
          {paused.status === "awaiting_review"
            ? "Open review cards here or use Review queue, then refresh saved chat. Automatic review-to-chat continuation is not assumed."
            : "Inspect the exact action below; no decision has been sent by this view."}
        </p>
      )}
      {actions.map((a) =>
        a.status === "pending" ? (
          <ActionCard
            key={a.id + ":" + generation}
            action={a}
            disabled={
              busy ||
              !loaded ||
              !turns.some(
                (t) =>
                  t.id === a.turn_id && t.status === "awaiting_confirmation",
              )
            }
            modelId={modelId}
            onDecision={(outcome, rationale) =>
              void task(async (signal) => {
                setLoaded(false);
                await api.chat.decide(
                  conversation,
                  a,
                  user.role,
                  modelId,
                  outcome,
                  rationale,
                  crypto.randomUUID(),
                  signal,
                );
                await snapshot(conversation, signal);
                if (!signal.aborted)
                  setInfo(
                    "Decision recorded. Inspect the saved run/continuation; this is not model approval.",
                  );
              })
            }
          />
        ) : (
          <div key={a.id}>
            {a.run_id ? <OperationStatusCard key={a.run_id} runId={a.run_id} modelId={a.model_id} onRequest={prepareRequest} /> : <p>Action decided. No operation reference was returned.</p>}
          </div>
        ),
      )}
        {!question ? (
          <fieldset className="chat-choices" disabled={busy || blocked}>
            <legend>Suggested starting points</legend>
            {[
              [
                "Explain the workflow",
                "Explain the model risk workflow and where I should start.",
              ],
              [
                "Check missing evidence",
                "Help me identify missing evidence for the selected model.",
              ],
              [
                "Plan validation",
                "Help me choose validation checks for the selected model.",
              ],
            ].map(([label, prompt], i) => (
              <button
                type="button"
                key={label}
                disabled={i > 0 && !model}
                onClick={() => setText(prompt)}
              >
                {label}
              </button>
            ))}
          </fieldset>
        ) : (
          <fieldset className="chat-choices" disabled={busy || blocked}>
            <legend>{question.question}</legend>
            {question.options.map((o) => (
              <button type="button" key={o.id} onClick={() => setText(o.label)}>
                {o.label}
              </button>
            ))}
            <p>
              Choices fill the message only; they never authorize an action.
            </p>
          </fieldset>
        )}

      {uncertain && (
        <p role="status">
          Submission outcome is uncertain. Use Refresh saved chat to look up the
          saved turn. No automatic POST retry.
        </p>
      )}
      {info && <p role="status">{info}</p>}
      {error && (
        <p role="alert">
          {error} No automatic write retry; refresh saved state before another
          action.
        </p>
      )}
      <button onClick={() => setShowProposals(v => !v)}>{showProposals ? "Hide agent proposals" : "Browse saved agent proposals"}</button>
      {showProposals && <LiveProposals key={"proposals:" + (modelId ?? "general")} modelId={modelId??undefined} onRequest={prepareRequest}/>}
      <ChatReviews key={"reviews:" + (modelId ?? "general")} modelId={modelId} />
      <details className="chat-session-details"><summary>About chat and approvals</summary><p>
        HTTPS + SSE. Review cards use the same authenticated decisions as Review queue.
        Changing models restores a separate conversation context.
      </p></details>
      </div>
      <form
        className="form chat-composer-form"
        onSubmit={(e) => {
          e.preventDefault();
          if (blocked || busy || !text.trim()) return;
          void task(async (signal) => {
            let id = conversationRef.current;
            if (!id) {
              id = await api.createConversation(signal);
              if (signal.aborted) return;
              remember(id);
            }
            const clientId = crypto.randomUUID();
            markUncertain(clientId);
            setLoaded(false);
            setQuestion(null);
            const turn = await api.chat.submit(
              id,
              text.trim(),
              modelId,
              clientId,
              signal,
            );
            if (signal.aborted) return;
            markUncertain(null);
            setTurns([turn]);
            setText("");
            await observe(turn, signal);
          });
        }}
      >
        <div className="chat-composer"><label htmlFor="agent-message">Message the agent</label>
        <textarea
          id="agent-message"
          value={text}
          disabled={busy}
          onChange={(e) => setText(e.target.value)}
          maxLength={10000}
          placeholder={model ? "Ask about this model…" : "Ask a question or describe what you need…"}
          rows={2}
          onKeyDown={(e) => {
            if (e.key === "Enter" && (e.ctrlKey || e.metaKey) && !e.nativeEvent.isComposing) {
              e.preventDefault();
              if (!busy && !blocked && text.trim()) e.currentTarget.form?.requestSubmit();
            }
          }}
        />
        <div className="chat-compose-actions"><span>Ctrl / ⌘ + Enter to send</span><button
          className="button primary"
          disabled={busy || blocked || !text.trim()}
        >
          {busy ? "Waiting…" : "Send"}
        </button></div></div>
        {busy && (
          <button
            type="button"
            onClick={() => {
              controller.current?.abort();
              setLoaded(false);
            }}
          >
            Stop observation
          </button>
        )}
      </form>
    </aside>
  );
}
function ActionCard({
  action,
  disabled,
  modelId,
  onDecision,
}: {
  action: ChatAction;
  disabled: boolean;
  modelId: string | null;
  onDecision: (outcome: "approved" | "rejected", rationale: string) => void;
}) {
  const { user } = useAuth();
  const [choice, setChoice] = useState(""),
    [rationale, setRationale] = useState(""),
    [confirmed, setConfirmed] = useState(false);
  return (
    <section
      className="lifecycle-card"
      aria-label={"Confirm action " + action.id}
    >
      <h3>Confirm proposed {action.operation}</h3>
      <p>
        Model {action.model_id} · version {action.model_version}
      </p>
      <p>Action version: {action.action_version}</p>
      {action.parameters && (
        <p>
          Declared simulation: {action.parameters.window_label}; feature{" "}
          {action.parameters.shift.feature}; factor{" "}
          {action.parameters.shift.factor}. This is not observed production
          drift.
        </p>
      )}
      <p>
        Approval starts this exact backend operation and may incur
        execution/provider costs. It does not approve a model or validation
        report.
      </p>
      <label>
        Action decision
        <select
          aria-label="Action decision"
          disabled={disabled}
          value={choice}
          onChange={(e) => {
            setChoice(e.target.value);
            setConfirmed(false);
          }}
        >
          <option value="">Choose decision</option>
          <option
            value="approved"
            disabled={!canDecideAction(action, user.role, modelId, "approved")}
          >
            Approve operation
          </option>
          <option
            value="rejected"
            disabled={!canDecideAction(action, user.role, modelId, "rejected")}
          >
            Reject operation
          </option>
        </select>
      </label>
      <label>
        Action rationale
        <textarea
          aria-label="Action rationale"
          disabled={disabled}
          value={rationale}
          maxLength={4000}
          onChange={(e) => {
            setRationale(e.target.value);
            setConfirmed(false);
          }}
        />
      </label>
      <label>
        <input
          type="checkbox"
          disabled={disabled}
          checked={confirmed}
          onChange={(e) => setConfirmed(e.target.checked)}
        />
        I confirm the exact action, model version, parameters and decision shown
        above
      </label>
      <button
        disabled={
          disabled ||
          !confirmed ||
          !rationale.trim() ||
          !canDecideAction(action, user.role, modelId, choice)
        }
        onClick={() => {
          setConfirmed(false);
          onDecision(choice as "approved" | "rejected", rationale);
        }}
      >
        Submit action decision
      </button>
    </section>
  );
}
