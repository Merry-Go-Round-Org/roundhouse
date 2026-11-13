import { useEffect, useRef, useState } from "react";
import { MessageCircle, Minus, Maximize2, Minimize2 } from "lucide-react";
import { AgentPanel } from "./AgentPanel";
import type { ModelRecord } from "./api";

/** Visibility never owns the turn lifetime: minimizing keeps the client mounted. */
export function FloatingAgent({ model, deletedModelIds = [] }: { model: ModelRecord | null; deletedModelIds?: string[] }) {
  const [open, setOpen] = useState(false);
  const [expanded, setExpanded] = useState(false);
  const [context, setContext] = useState<ModelRecord | null>(null);
  const [started, setStarted] = useState(false);
  const [removedContext, setRemovedContext] = useState(false);
  useEffect(() => {
    if (context && deletedModelIds.includes(context.id)) { setContext(null); setRemovedContext(true); }
  }, [context, deletedModelIds]);
  const launcher = useRef<HTMLButtonElement>(null);
  const minimize = useRef<HTMLButtonElement>(null);
  // Initial deep links acquire their model before chat is opened. Navigation
  // after opening never silently redirects a conversation to another model.
  useEffect(() => {
    if (!started && model && !deletedModelIds.includes(model.id)) setContext(model);
  }, [model, started, deletedModelIds]);
  function close() {
    setOpen(false);
    launcher.current?.focus();
  }
  useEffect(() => {
    if (open) minimize.current?.focus();
  }, [open]);
  return (
    <div className="agent-dock">
      <section
        id="floating-agent-window"
        className={`agent-window${expanded ? " expanded" : ""}`}
        hidden={!open}
        aria-label="Agent workspace"
        onKeyDown={(event) => {
          if (event.key === "Escape" && !event.defaultPrevented) {
            event.stopPropagation();
            close();
          }
        }}
      >
        <header className="agent-window-bar">
          <strong>
            <MessageCircle size={18} aria-hidden="true" /> Agent chat
          </strong>
          <div>
            <button
              type="button"
              aria-label={expanded ? "Restore chat size" : "Expand chat"}
              onClick={() => setExpanded((value) => !value)}
            >
              {expanded ? <Minimize2 size={18} /> : <Maximize2 size={18} />}
            </button>
            <button
              ref={minimize}
              type="button"
              aria-label="Minimize chat"
              onClick={close}
            >
              <Minus size={20} />
            </button>
          </div>
        </header>
        <div className="agent-context">
          {removedContext && <p role="status">The previous model was deleted. Chat has returned to general guidance.</p>}
          <span>Context: {context?.name ?? "General guidance"}</span>
          {model && model.id !== context?.id && (
            <button type="button" onClick={() => setContext(model)}>
              Chat about {model.name}
            </button>
          )}
          {context && (
            <button type="button" onClick={() => setContext(null)}>
              General guidance
            </button>
          )}
        </div>
        {started && (
          <AgentPanel key={context?.id ?? "general"} model={context} />
        )}
      </section>
      <button
        ref={launcher}
        type="button"
        className="agent-launcher"
        aria-expanded={open}
        aria-controls="floating-agent-window"
        onClick={() => {
          if (open) close();
          else {
            setStarted(true);
            setOpen(true);
          }
        }}
      >
        <MessageCircle size={20} aria-hidden="true" />{" "}
        {open ? "Minimize agent chat" : "Open agent chat"}
      </button>
    </div>
  );
}
