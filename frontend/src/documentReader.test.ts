import { it, expect, vi } from "vitest";
import {
  documentLink,
  readMarkdownResponse,
  MAX_DOCUMENT_BYTES,
} from "./documentTransport";
import { createModelClient } from "./api";
it("permits only explicit HTTPS citations without embedded credentials", () => {
  expect(documentLink("https://example.org/evidence")).toBe(
    "https://example.org/evidence",
  );
  for (const value of [
    "javascript:alert(1)",
    "data:text/html,test",
    "file:///model",
    "../manifest.json",
    "/api/v1/action",
    "http://example.org",
    "//example.org",
    "https://user:password@example.org",
  ])
    expect(documentLink(value)).toBe("");
});
it("rejects binary, HTML, empty and oversized previews", async () => {
  for (const [body, type] of [
    ["%PDF", "application/pdf"],
    ["<script>x</script>", "text/html"],
    ["", "text/plain"],
    ["a\u0000b", "text/plain"],
  ])
    await expect(
      readMarkdownResponse(
        new Response(body, { headers: { "content-type": type } }),
      ),
    ).rejects.toThrow();
  await expect(
    readMarkdownResponse(
      new Response("x", {
        headers: {
          "content-type": "text/plain",
          "content-length": String(MAX_DOCUMENT_BYTES + 1),
        },
      }),
    ),
  ).rejects.toThrow("preview limit");
  await expect(
    readMarkdownResponse(
      new Response(new Uint8Array(MAX_DOCUMENT_BYTES + 1).fill(65), {
        headers: { "content-type": "text/plain" },
      }),
    ),
  ).rejects.toThrow("preview limit");
});
it("uses the authorized artifact route and retains original Markdown", async () => {
  const source = "# Model\n\n## Purpose\nAn agent draft.";
  const fetcher = vi
    .fn<typeof fetch>()
    .mockResolvedValueOnce(
      new Response(source, {
        headers: { "content-type": "text/markdown; charset=utf-8" },
      }),
    )
    .mockResolvedValueOnce(new Response(null, { status: 403 }));
  const client = createModelClient("/api/v1", fetcher);
  expect(await client.getDocumentText("a/b")).toBe(source);
  expect(fetcher.mock.calls[0][0]).toBe("/api/v1/artifacts/a%2Fb/download");
  await expect(client.getDocumentText("a/b")).rejects.toMatchObject({
    status: 403,
  });
});
