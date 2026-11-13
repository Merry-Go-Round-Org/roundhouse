import { expect, it, vi } from "vitest";
import { createModelClient } from "./api";
const dataset = {
  id: "uci_credit",
  name: "Credit",
  filename: "credit.xls",
  target_description: "Default next month",
  limitations: ["Historical population"],
};
it("downloads Markdown by report ID with authentication and rejects wrong formats or denied access", async () => {
  const fetcher = vi.fn<typeof fetch>()
    .mockResolvedValueOnce(new Response("# Validation\nHuman decision: accepted", { headers: { "Content-Type": "text/markdown; charset=utf-8" } }))
    .mockResolvedValueOnce(Response.json({ checks: [] }))
    .mockResolvedValueOnce(Response.json({ detail: "Access denied" }, { status: 403 }));
  const api = createModelClient("/api/v1", fetcher);
  const signal = new AbortController().signal;
  const blob = await api.downloadReportMarkdown("report/1", signal);
  expect(await blob.text()).toContain("Human decision: accepted");
  expect(blob.type).toContain("text/markdown");
  expect(fetcher.mock.calls[0][0]).toBe("/api/v1/reports/report%2F1/markdown");
  expect(fetcher.mock.calls[0][1]).toMatchObject({ credentials: "include", signal });
  await expect(api.downloadReportMarkdown("report/1")).rejects.toThrow("not Markdown");
  await expect(api.downloadReportMarkdown("report/1")).rejects.toMatchObject({ status: 403 });
  expect(fetcher).toHaveBeenCalledTimes(3);
});
it("loads dataset descriptions and rejects duplicate or unsupported IDs", async () => {
  const fetcher = vi
    .fn<typeof fetch>()
    .mockResolvedValueOnce(Response.json([dataset]))
    .mockResolvedValueOnce(Response.json([dataset, dataset]))
    .mockResolvedValueOnce(Response.json([{ ...dataset, id: "unknown" }]));
  const api = createModelClient("/api/v1", fetcher);
  expect(await api.listDatasets()).toEqual([dataset]);
  await expect(api.listDatasets()).rejects.toThrow("catalogue");
  await expect(api.listDatasets()).rejects.toThrow("catalogue");
});
it("rejects document records for a different model and preserves unavailable reports", async () => {
  const fetcher = vi
    .fn<typeof fetch>()
    .mockResolvedValueOnce(
      Response.json([
        {
          id: "d",
          model_id: "other",
          run_id: "r",
          kind: "documentation",
          artifact_id: "a",
          metrics: [],
        },
      ]),
    )
    .mockResolvedValueOnce(
      Response.json({ detail: "Reports not implemented" }, { status: 501 }),
    );
  const api = createModelClient("/api/v1", fetcher);
  await expect(api.listReports("m")).rejects.toThrow("mismatch");
  await expect(api.listReports("m")).rejects.toMatchObject({ status: 501 });
});
it("reads existing document references without inventing approval or version metadata", async () => {
  const report = {
    id: "d",
    model_id: "m",
    run_id: "r",
    kind: "documentation",
    artifact_id: "a/b",
    metrics: [],
  };
  const fetcher = vi
    .fn<typeof fetch>()
    .mockResolvedValueOnce(Response.json([report]))
    .mockResolvedValueOnce(new Response("draft"));
  const api = createModelClient("/api/v1", fetcher);
  expect(await api.listReports("m")).toEqual([report]);
  expect(await (await api.downloadArtifact("a/b")).text()).toBe("draft");
  expect(fetcher.mock.calls[1][0]).toBe("/api/v1/artifacts/a%2Fb/download");
});
