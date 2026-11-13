export interface ChatQuestion {
  id: string;
  question: string;
  options: { id: string; label: string }[];
}

// Proposed frontend extension; backend adoption is still pending.
export function parseQuestion(value: unknown): ChatQuestion {
  const validText = (v: unknown, max: number): v is string =>
    typeof v === "string" && v.trim().length > 0 && v.length <= max;
  if (!value || typeof value !== "object")
    throw new Error("Invalid chat choices.");
  const q = value as Partial<ChatQuestion>;
  if (
    !validText(q.id, 100) ||
    !validText(q.question, 1000) ||
    !Array.isArray(q.options) ||
    q.options.length < 2 ||
    q.options.length > 6 ||
    q.options.some(
      (o) => !o || !validText(o.id, 100) || !validText(o.label, 500),
    ) ||
    new Set(q.options.map((o) => o.id)).size !== q.options.length
  )
    throw new Error("Invalid chat choices.");
  return {
    id: q.id,
    question: q.question,
    options: q.options.map(({ id, label }) => ({ id, label })),
  };
}
