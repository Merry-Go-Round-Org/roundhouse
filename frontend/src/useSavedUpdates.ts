import { useEffect, useRef, useState, type RefObject } from "react";

/** Sequential GET observation. Callers retain drafts; this hook never submits actions. */
export function useSavedUpdates<T>({ scope, enabled, element, read, apply, failed, interval = 10000 }: {
  scope: string; enabled: boolean; element: RefObject<HTMLElement | null>;
  read: (signal: AbortSignal) => Promise<T>; apply: (value: T) => void;
  failed?: () => void; interval?: number;
}) {
  const callbacks = useRef({ read, apply, failed });
  callbacks.current = { read, apply, failed };
  const [notice, setNotice] = useState("");
  useEffect(() => {
    setNotice("");
    if (!enabled) return;
    const c = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    let reads = 0;
    async function tick() {
      if (c.signal.aborted) return;
      if (document.visibilityState !== "visible" || element.current?.closest("[hidden]")) {
        timer = setTimeout(() => void tick(), interval); return;
      }
      try {
        const value = await callbacks.current.read(c.signal);
        if (c.signal.aborted) return;
        callbacks.current.apply(value);
        if (++reads >= 100) {
          setNotice("Automatic updates paused after 100 checks. Use Refresh or Reload to continue.");
        } else timer = setTimeout(() => void tick(), interval);
      } catch (e) {
        if (!c.signal.aborted) {
          setNotice(`Automatic updates stopped: ${(e as Error).message}. Displayed records may be outdated. Use Refresh or Reload to reconnect.`);
          callbacks.current.failed?.();
        }
      }
    }
    timer = setTimeout(() => void tick(), interval);
    return () => { c.abort(); clearTimeout(timer); };
  }, [scope, enabled, element, interval]);
  return notice;
}
