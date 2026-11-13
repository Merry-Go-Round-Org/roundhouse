import { useEffect, useRef, useState } from "react";

export const workspacePages = ["Workspaces", "Inventory", "Datasets", "Review queue", "Monitoring", "Dashboard", "Accounts", "Archive intake", "Agent proposals"] as const;
export const modelTabs = ["Overview", "Documentation", "Rebuild & parity", "Validation", "Monitoring", "History"] as const;
export type WorkspaceRoute = {
  page: typeof workspacePages[number];
  tab: typeof modelTabs[number];
  id: string;
  datasetId: string | null;
};
export function readWorkspaceRoute(search: string, admin: boolean): WorkspaceRoute {
  const params = new URLSearchParams(search);
  const page = workspacePages.find(p => p === params.get("page")) ?? "Inventory";
  return {
    page: page === "Accounts" && !admin ? "Inventory" : page,
    tab: modelTabs.find(t => t === params.get("tab")) ?? "Overview",
    id: params.get("model") ?? "",
    datasetId: params.get("dataset") || null,
  };
}
export function useWorkspaceNavigation(admin: boolean) {
  const [route, setRoute] = useState(() => readWorkspaceRoute(location.search, admin));
  const current = useRef(route);
  useEffect(() => {
    const restore = () => {
      const saved = readWorkspaceRoute(location.search, admin);
      current.current = saved;
      setRoute(saved);
    };
    window.addEventListener("popstate", restore);
    return () => window.removeEventListener("popstate", restore);
  }, [admin]);
  function navigate(patch: Partial<WorkspaceRoute>) {
    const next = { ...current.current, ...patch };
    if (next.page === "Accounts" && !admin) next.page = "Inventory";
    const url = new URL(location.href);
    for (const [key, value] of Object.entries({
      page: next.page === "Inventory" ? null : next.page,
      tab: next.tab === "Overview" ? null : next.tab,
      model: next.id || null,
      dataset: next.datasetId,
    })) {
      if (value) url.searchParams.set(key, value);
      else url.searchParams.delete(key);
    }
    if (url.href !== location.href) history.pushState(null, "", url);
    current.current = next;
    setRoute(next);
  }
  return { ...route, navigate };
}
