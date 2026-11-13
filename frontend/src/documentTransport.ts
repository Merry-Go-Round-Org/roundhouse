export const MAX_DOCUMENT_BYTES = 2 * 1024 * 1024;
export async function readMarkdownResponse(
  response: Response,
): Promise<string> {
  const type = response.headers
    .get("content-type")
    ?.split(";")[0]
    .trim()
    .toLowerCase();
  if (
    !type ||
    !["text/markdown", "text/plain", "application/octet-stream"].includes(type)
  )
    throw new Error(
      "This artifact is not a supported Markdown/text document. Use its download instead.",
    );
  const declared = Number(response.headers.get("content-length"));
  if (declared > MAX_DOCUMENT_BYTES)
    throw new Error(
      "Document exceeds the 2 MiB preview limit. Use its download instead.",
    );
  if (!response.body) throw new Error("The document response has no body.");
  const reader = response.body.getReader(),
    decoder = new TextDecoder("utf-8", { fatal: true });
  let size = 0,
    text = "";
  try {
    while (true) {
      const part = await reader.read();
      if (part.done) break;
      size += part.value.byteLength;
      if (size > MAX_DOCUMENT_BYTES)
        throw new Error(
          "Document exceeds the 2 MiB preview limit. Use its download instead.",
        );
      text += decoder.decode(part.value, { stream: true });
    }
    text += decoder.decode();
  } finally {
    await reader.cancel();
    reader.releaseLock();
  }
  if (!text.trim() || text.includes("\u0000"))
    throw new Error("Document is empty or is not UTF-8 text.");
  return text;
}
export function documentLink(url: string): string {
  // No remote images, file paths, credentials, relative API routes or active schemes.
  try {
    const parsed = new URL(url);
    return parsed.protocol === "https:" && !parsed.username && !parsed.password
      ? parsed.href
      : "";
  } catch {
    return "";
  }
}
