import React from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App";
import { AuthShell } from "./AuthShell";
import "./style.css";
const ProposalPreview = React.lazy(() => import("./ProposalPreview"));
createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    {window.location.pathname === "/demo/proposals" ? (
      <React.Suspense fallback={<p>Loading proposal preview…</p>}>
        <ProposalPreview />
      </React.Suspense>
    ) : window.location.pathname === "/demo" ? (
      <App />
    ) : (
      <AuthShell />
    )}
  </React.StrictMode>,
);
