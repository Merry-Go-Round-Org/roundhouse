import { afterEach, expect, it, vi } from "vitest";
import { createModelClient } from "./api";
afterEach(() => vi.unstubAllGlobals());
const file = (name: string, text = "fixture") => new File([text], name);
const complete = { id: "uci_credit", status: "completed", checks: [{ name: "Split", status: "passed", blocking: true, reason: "Verified" }] };
function transport(response: unknown = complete) {
  const xhr = { upload: {}, open: vi.fn(), send: vi.fn(), abort: vi.fn(), onload: () => {}, status: 201, responseText: JSON.stringify(response), withCredentials: false, timeout: 0 };
  vi.stubGlobal("XMLHttpRequest", function () { return xhr; });
  return xhr;
}
it.each([false, true])("sends a dataset with explicit split=%s without an empty placeholder", async (explicit) => {
  const xhr = transport();
  const split = explicit ? file("split.csv", "id,partition\n1,train") : null;
  const pending = createModelClient().uploadDataset(file("source.csv"), file("dataset.json"), split, vi.fn());
  const body = xhr.send.mock.calls[0][0] as FormData;
  expect([...body.keys()]).toEqual(explicit ? ["source", "descriptor", "split"] : ["source", "descriptor"]);
  if (split) expect(await (body.get("split") as File).text()).toBe(await split.text());
  expect(xhr.withCredentials).toBe(true);
  xhr.onload();
  await expect(pending).resolves.toMatchObject({ id: "uci_credit" });
  expect(xhr.send).toHaveBeenCalledTimes(1);
});
it("rejects empty required files and explicitly selected empty splits before transport", async () => {
  const xhr = transport();
  for (const [source, descriptor, split] of [
    [file("source.csv", ""), file("dataset.json"), null],
    [file("source.csv"), file("dataset.json", ""), null],
    [file("source.csv"), file("dataset.json"), file("split.csv", "")],
  ] as const) await expect(createModelClient().uploadDataset(source, descriptor, split, vi.fn())).rejects.toThrow();
  expect(xhr.send).not.toHaveBeenCalled();
});
it.each([
  { ...complete, status: "queued" },
  { ...complete, checks: [{ ...complete.checks[0], status: "failed" }] },
])("never treats incomplete or rejected splitting as completed", async (response) => {
  const xhr = transport(response);
  const pending = createModelClient().uploadDataset(file("source.csv"), file("dataset.json"), null, vi.fn());
  xhr.onload();
  await expect(pending).rejects.toThrow("Check the catalogue before retrying");
  expect(xhr.send).toHaveBeenCalledTimes(1);
});
