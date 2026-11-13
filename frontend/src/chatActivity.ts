import type { PublicChatEvent } from "./durableChat";

/** Compact repeated display text, without treating distinct tool events as duplicate actions. */
export function compactChatActivity(events: PublicChatEvent[]) {
  const seen = new Set<number>();
  const proposals = new Set<string>();
  const tools = new Map<string, number>();
  const notices: PublicChatEvent[] = [];
  let toolCount = 0;
  for (const event of events) {
    if (seen.has(event.seq)) continue;
    seen.add(event.seq);
    if (event.kind === "tool_call") {
      const text = event.text || "Platform tool activity";
      tools.set(text, (tools.get(text) ?? 0) + 1);
      toolCount++;
    } else {
      if (event.proposal_id) {
        if (proposals.has(event.proposal_id)) continue;
        proposals.add(event.proposal_id);
      }
      notices.push(event);
    }
  }
  return { toolCount, tools: [...tools].map(([text, count]) => ({ text, count })), notices };
}
