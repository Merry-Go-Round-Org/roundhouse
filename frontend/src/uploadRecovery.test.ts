import { expect, it, vi } from "vitest";
import { createModelClient } from "./api";
const row = { id: "u1", status: "queued", checks: [], files: [], model_id: null, error: null };
function client(value: unknown) {
  const fetcher = vi.fn<typeof fetch>(async () => new Response(JSON.stringify(value), { status: 200 }));
  return { fetcher, api: createModelClient("/api/v1", fetcher) };
}
it("reads actor-scoped pages using GET and the shared cookie client", async () => {
  const { api, fetcher } = client([row]);
  expect(await api.listUploads(20, 20)).toEqual([row]);
  expect(fetcher.mock.calls[0]?.[1]?.credentials).toBe("include");
  expect(fetcher.mock.calls[0]?.[1]?.method ?? "GET").toBe("GET");
  expect(fetcher.mock.calls[0]?.[0]).toBe("/api/v1/models/uploads?offset=20&limit=20");
});
it("rejects malformed, duplicate and oversized pages", async () => {
  for (const value of [{}, [row, row], [{ ...row, status: "completed" }], Array.from({length: 21}, (_, i) => ({...row, id: String(i)}))])
    await expect(client(value).api.listUploads()).rejects.toThrow();
});
it("rejects invalid pagination before requesting and another upload's detail", async () => {
  const { api, fetcher } = client(row);
  await expect(api.listUploads(-1)).rejects.toThrow();
  expect(fetcher).not.toHaveBeenCalled();
  await expect(api.getUpload("other")).rejects.toThrow("another upload");
});
