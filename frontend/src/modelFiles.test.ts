import { expect, it, vi } from "vitest";
import { sourcePath, readModelFiles, modelFileRoute } from "./modelFiles";
import { createModelClient } from "./api";
const file = {path:"documentation/notes.md",sha256:"a".repeat(64)};
it("maps exact package citations without traversal or active schemes", () => {
  expect(sourcePath("/inputs/model/documentation/notes.md#L4")).toBe(file.path);
  expect(sourcePath("./manifest.json")).toBe("manifest.json");
  for (const path of ["../notes.md","/etc/secret.txt","https://host/notes.md","javascript:hello.py","%2e%2e/notes.md","a\\b.py","a//b.py","a/./b.py","manifest.json?secret=1"])
    expect(sourcePath(path)).toBeNull();
});
it("validates file evidence and never follows a supplied download URL", () => {
  expect(readModelFiles([{...file,download_url:"https://untrusted.example"}])).toEqual([file]);
  for (const value of [{},[file,file],[{...file,sha256:"bad"}],[{...file,path:"../x.md"}]])
    expect(() => readModelFiles(value)).toThrow();
  expect(modelFileRoute("m1",file.path)).toBe("/models/m1/files/documentation/notes.md");
});
it("rechecks source hash before fetching through the authenticated model route", async () => {
  const transport = vi.fn<typeof fetch>(async url => String(url).endsWith("/files") ?
    new Response(JSON.stringify([file])) : new Response("Retained notes",{headers:{"content-type":"text/plain"}}));
  const api = createModelClient("/api/v1",transport);
  expect(await api.getModelFile("m1",file)).toBe("Retained notes");
  expect(transport.mock.calls[1][0]).toBe("/api/v1/models/m1/files/documentation/notes.md");
  expect(transport.mock.calls[1][1]?.credentials).toBe("include");
  transport.mockClear();
  await expect(api.getModelFile("m1",{...file,sha256:"b".repeat(64)})).rejects.toThrow("changed");
  expect(transport).toHaveBeenCalledTimes(1);
});
