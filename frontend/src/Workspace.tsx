import { useWorkspaceNavigation, modelTabs as tabs, type WorkspaceRoute } from "./workspaceNavigation";
import { IntakePage } from "./IntakePage";
import { LiveProposals, ProposalSection } from "./LiveProposals";
import { FloatingAgent } from "./FloatingAgent";
import { Activity, BarChart3, ChevronRight, CircleDot, Database, FileText, GitBranch, Layers3, LayoutDashboard, ShieldCheck } from "lucide-react";
import { AccountAdmin } from "./AccountAdmin";
import { useAuth } from "./AuthContext";
import { canUpload } from "./auth";
import { RunHistory } from "./RunHistory";
import { ReviewQueue } from "./ReviewQueue";
import { useEffect, useState } from "react";
import { createModelClient, type ModelRecord, type DatasetRead } from "./api";
import { RunPanel } from "./AgentPanel";
import { ModelFindings } from "./ModelFindings";
import { ModelDocuments } from "./ModelDocuments";
import { UploadHistory } from "./UploadHistory";
import { UploadDialog } from "./UploadDialog";
import { ModelSelection } from "./ModelSelection";
import { liveInventoryRow } from "./inventoryAdapters";
import { ModelDetailTabs } from "./ModelDetailTabs";
import { ModelEvidenceSection } from "./ModelEvidenceSection";
import { InventoryOverview } from "./InventoryOverview";
import { DatasetSelection } from "./DatasetSelection";
import { DeletionDialog } from "./DeletionDialog";
import type { DeletionSelection } from "./deletion";
import { MonitoringPage } from "./MonitoringPage";
import { ModelMonitoring } from "./ModelMonitoring";
import { DashboardPage } from "./DashboardPage";
import { ParityPanel } from "./ParityPanel";
import { derivedFrom } from "./parity";
const api = createModelClient();
type Tab = WorkspaceRoute["tab"];
// Opt-in for upload acceptance testing; Backend routes exist, but full E2E is not signed off.
const uploadsEnabled =
  (import.meta as ImportMeta & { env: Record<string, string> }).env
    .VITE_ENABLE_PROPOSED_UPLOADS === "true";
export function Workspace() {
  const { user, signOut, signingOut } = useAuth();
  const uploader = canUpload(user.role);
  const canDelete = user.role === "admin" || user.role === "developer";
  const [deletion, setDeletion] = useState<DeletionSelection | null>(null);
  const [deletedModelIds, setDeletedModelIds] = useState<string[]>([]);
  const { page, tab, id, datasetId, navigate } = useWorkspaceNavigation(user.role === "admin");
  const setPage = (page: WorkspaceRoute["page"]) => navigate({ page });
  const setTab = (tab: Tab) => navigate({ tab });
  const setDatasetId = (datasetId: string | null) => navigate({ datasetId });
  const [selected, setSelected] = useState<ModelRecord | null>(null);
  const [models, setModels] = useState<ModelRecord[]>([]),
    [datasets, setDatasets] = useState<DatasetRead[]>([]),
    [error, setError] = useState(""),
    [detailError, setDetailError] = useState("");
  const [loading, setLoading] = useState(true),
    [revision, setRevision] = useState(0),
    [offset, setOffset] = useState(0),
    [query, setQuery] = useState("");
  const [languageFilter, setLanguageFilter] = useState("");
  const [lifecycleFilter, setLifecycleFilter] = useState("");
  const [datasetFilter, setDatasetFilter] = useState("");
  const [unassignedOnly, setUnassignedOnly] = useState(false);
  function clearFilters() {
    setQuery("");
    setLanguageFilter("");
    setLifecycleFilter("");
    setDatasetFilter("");
    setUnassignedOnly(false);
  }
  function openDataset(value: DatasetRead["id"] | null) {
    if (value === null) return; // Synthetic survival models have no dataset.
    navigate({ datasetId: value, page: "Datasets" });
  }
  function findDatasetModels(value: DatasetRead["id"]) {
    clearFilters();
    setDatasetFilter(value);
    setOffset(0);
    select("");
  }
  const [showUploads, setShowUploads] = useState(false);
  const [upload, setUpload] = useState<"model" | "dataset" | null>(null);
  function select(value: string, nextTab: Tab = "Overview") {
    if (value !== id) setSelected(null);
    navigate({ id: value, tab: nextTab, page: "Inventory" });
  }
  useEffect(() => {
    const c = new AbortController();
    setLoading(true);
    setError("");
    if (page !== "Inventory" && page !== "Datasets") {
      setLoading(false);
      return () => c.abort();
    }
    const task =
      page === "Datasets"
        ? api.listDatasets(c.signal).then((v) => {
            if (!c.signal.aborted) setDatasets(v);
          })
        : api.list(offset, 20, c.signal).then((v) => {
            if (!c.signal.aborted) setModels(v);
          });
    task
      .catch((e) => {
        if (!c.signal.aborted) setError(e.message);
      })
      .finally(() => {
        if (!c.signal.aborted) setLoading(false);
      });
    return () => c.abort();
  }, [page, offset, revision]);
  useEffect(() => {
    const c = new AbortController();
    setSelected(null);
    setDetailError("");
    if (id)
      api
        .get(id, c.signal)
        .then((m) => {
          if (!c.signal.aborted) setSelected(m);
        })
        .catch((e) => {
          if (!c.signal.aborted) setDetailError(e.message);
        });
    return () => c.abort();
  }, [id, revision]);
  const visible = models.filter(
    (m) =>
      `${m.id} ${m.name} ${m.language} ${liveInventoryRow(m).owner ?? ""}`
        .toLowerCase()
        .includes(query.trim().toLowerCase()) &&
      (!languageFilter || m.language === languageFilter) &&
      (!lifecycleFilter ||
        (m.inventory?.lifecycle_stage ?? m.lifecycle_status) === lifecycleFilter) &&
      (!datasetFilter || m.dataset_id === datasetFilter) &&
      (!unassignedOnly || liveInventoryRow(m).owner === null),
  );
  return (
    <div className="app-shell live-shell">
      <a className="skip" href="#main">Skip to content</a>
      <aside className="sidebar live-sidebar">
        <div className="brand">
          <div className="brand-icon"><Layers3 size={24} /></div>
          <div>roundhouse<span>MODEL RISK WORKBENCH</span></div>
        </div>
        <button type="button" className="workspace workspace-return" aria-label="Open workspace overview" onClick={() => setPage("Workspaces")}>
          <span className="workspace-mark">M</span>
          <div>Merry Go Round<small>Challenge 04 workspace</small></div>
          <ChevronRight size={15} aria-hidden="true" />
        </button>
        <p className="nav-label">WORKSPACE</p>
        <nav aria-label="Main navigation">
          {([
            { name: "Inventory", icon: LayoutDashboard },
            { name: "Datasets", icon: Database },
            { name: "Archive intake", icon: Layers3 },
            { name: "Agent proposals", icon: GitBranch },
            { name: "Review queue", icon: ShieldCheck },
            { name: "Monitoring", icon: Activity },
            { name: "Dashboard", icon: BarChart3 },
            { name: "Accounts", icon: ShieldCheck },
          ] as const).filter(item => item.name !== "Accounts" || user.role === "admin").map(({ name, icon: Icon }) => (
            <button
              key={name}
              className={page === name ? "active" : ""}
              aria-current={page === name ? "page" : undefined}
              onClick={() => name === "Inventory" ? select("") : setPage(name)}
            >
              <Icon size={18} />
              {name}
            </button>
          ))}
        </nav>
        <div className="sidebar-note">
          <GitBranch size={19} />
          <strong>Evidence before approval.</strong>
          <p>Follow each model from legacy code to independent review.</p>
        </div>
        <div className="sidebar-bottom">
          <div className="workspace-identity">
            <span>Signed in as <strong>{user.username ?? user.id}</strong></span>
            <span>Role: <strong>{user.role}</strong></span>
            <button type="button" className="live-sign-out"
              disabled={signingOut} onClick={() => void signOut()}>
              {signingOut ? "Signing out…" : "Sign out"}
            </button>
          </div>
        </div>
      </aside>
      <div className="main-shell live-main-shell">
        <header className="topbar">
          <div>
            <button type="button" className="breadcrumb-home" onClick={() => setPage("Workspaces")}>Merry Go Round <ChevronRight size={14} aria-hidden="true" /></button>
            <span>{id && page === "Inventory" ? "Model detail" : page}</span>
          </div>
          <div>
            <span className="demo-pill live-pill"><CircleDot size={13} /> Live workspace</span>
            <span className="avatar" aria-label="Signed-in account">
              {(user.username ?? user.id).slice(0, 2).toUpperCase()}
            </span>
          </div>
        </header>
        <div className="live-content">
      <main id="main" className="workspace-main">
        <header className="page-heading">
          <div>
            <p className="eyebrow">MODEL WORKSPACE</p>
            <h1>{page === "Inventory" && selected ? selected.name : page}</h1>
          </div>
          <button
            className="button secondary"
            onClick={() => setRevision((v) => v + 1)}
          >
            Refresh
          </button>
        </header>
        {page === "Workspaces" && <section className="workspace-overview" aria-label="Available workspaces">
          <p>Choose a workspace to continue.</p>
          <article className="native-card">
            <h2>Merry Go Round</h2>
            <p>Challenge 04 · Model risk workbench</p>
            <button className="button primary" onClick={() => setPage("Inventory")}>Open Merry Go Round workspace</button>
          </article>
        </section>}
        {page === "Inventory" && (
          <>
            <div className="form-actions">
              <button className="button primary" onClick={() => setPage("Archive intake")}>Upload archive</button>
              <button className="button primary" disabled={!uploader} title={!uploader ? "A developer or administrator account is required to upload." : undefined} onClick={() => setUpload("model")}>
                  Upload model
                </button>
              <button className="button secondary" aria-expanded={showUploads}
                onClick={() => setShowUploads(v => !v)}>
                {showUploads ? "Hide saved uploads" : "View saved uploads"}
              </button>
              {id && (
                <button className="button secondary" onClick={() => select("")}>
                  Back to inventory
                </button>
              )}
            </div>
            {!uploader && <p className="page-description">Model uploads require a developer or administrator account.</p>}
            {id && <label className="model-switcher">Switch model
              <select aria-label="Switch model" value={id} disabled={loading} onChange={e => select(e.target.value)}>
                {!models.some(m => m.id === id) && <option value={id}>{selected?.name ?? "Current model"}</option>}
                {models.map(m => <option key={m.id} value={m.id}>{m.name} · {m.version}</option>)}
              </select><small>Models on this inventory page. Choose Back to inventory to browse more.</small>
            </label>}
            {showUploads && <UploadHistory key={`${user.id}:${revision}`} openModel={(value) => {
              setShowUploads(false); select(value);
            }} />}
            {detailError && <p role="alert">{detailError}</p>}
            {!id ? (
              <>
                <label className="inventory-search">
                  Search this inventory page
                  <input
                    value={query}
                    onChange={(e) => setQuery(e.target.value)}
                    placeholder="Name, ID, language or owner"
                  />
                </label>
                <fieldset className="inventory-filters">
                  <legend>Filter this inventory page</legend>
                  <label>
                    Language
                    <select
                      aria-label="Language"
                      value={languageFilter}
                      onChange={(e) => setLanguageFilter(e.target.value)}
                    >
                      <option value="">All languages</option>
                      <option>R</option>
                      <option>Python</option>
                    </select>
                  </label>
                  <label>
                    Lifecycle
                    <select
                      aria-label="Lifecycle"
                      value={lifecycleFilter}
                      onChange={(e) => setLifecycleFilter(e.target.value)}
                    >
                      <option value="">All lifecycle states</option>
                      {["development", "active", "retired", "uploaded", "documented", "rebuilt", "approved", "monitored", "revalidation_due"].map((v) => (
                        <option key={v}>{v}</option>
                      ))}
                    </select>
                  </label>
                  <label>
                    Dataset
                    <select
                      aria-label="Dataset"
                      value={datasetFilter}
                      onChange={(e) => setDatasetFilter(e.target.value)}
                    >
                      <option value="">All datasets</option>
                      <option value="uci_credit">UCI credit</option>
                      <option value="ulb_fraud">ULB fraud</option>
                    </select>
                  </label>
                  <label className="filter-checkbox">
                    <input
                      type="checkbox"
                      checked={unassignedOnly}
                      onChange={(e) => setUnassignedOnly(e.target.checked)}
                    />
                    Owner not supplied
                  </label>
                  <button
                    type="button"
                    className="button secondary"
                    onClick={clearFilters}
                  >
                    Clear filters
                  </button>
                </fieldset>
                <p>
                  Search and filters apply only to this fetched page (up to 20
                  models). Use Previous/Next to inspect other pages; these are
                  not portfolio totals.
                </p>
                {loading ? (
                  <p role="status">Loading inventory…</p>
                ) : error ? (
                  <p role="alert">{error}</p>
                ) : (
                  <section className="panel live-inventory-panel" aria-label="Model portfolio">
                    <div className="panel-title">
                      <h2>Model portfolio</h2>
                      <p>Models available on this page. Select a model to inspect its evidence.</p>
                    </div>
                    <ModelSelection
                      key={JSON.stringify([user.id, offset, revision, query, languageFilter, lifecycleFilter, datasetFilter, unassignedOnly, visible.map(m => [m.id, m.version])])}
                      rows={visible.map(liveInventoryRow)}
                      canDelete={canDelete}
                      requestDeletion={setDeletion}
                      onOpen={select}
                    />
                    <div className="table-footer">
                      <span role="status">{visible.length} of {models.length} fetched models match.</span>
                      <span>Saved records · page {offset / 20 + 1}</span>
                    </div>
                    {visible.length === 0 && (
                      <div className="empty">
                        <h3>{models.length ? "No models match your filters." : offset ? "No models on this page." : "No models available yet."}</h3>
                        <p>{models.length ? "Try a different search or clear the filters." : offset ? "Use Previous models to return to earlier records." : uploader ? "Upload a model to start documentation, rebuilding and review." : "Ask a model developer to upload a model, then refresh this page."}</p>
                        {models.length > 0 && <button type="button" className="button secondary" onClick={clearFilters}>Reset inventory filters</button>}
                        {!models.length && offset === 0 && uploader && <button type="button" className="button primary" onClick={() => setUpload("model")}>Upload your first model</button>}
                      </div>
                    )}
                  </section>
                )}
                <div className="form-actions">
                  <button
                    disabled={loading || offset === 0}
                    onClick={() => setOffset((v) => Math.max(0, v - 20))}
                  >
                    Previous models
                  </button>
                  <span>Page {offset / 20 + 1}</span>
                  <button
                    disabled={loading || !!error || models.length < 20}
                    onClick={() => setOffset((v) => v + 20)}
                  >
                    Next models
                  </button>
                </div>
              </>
            ) : selected ? (
              <>
                <ModelDetailTabs
                  tabs={tabs}
                  value={tab}
                  onChange={setTab}
                  label="Model detail"
                  panelClassName="live-detail-panel"
                >
                  {tab === "Overview" && (
                    <InventoryOverview
                      model={selected}
                      openDataset={() => openDataset(selected.dataset_id)}
                    />
                  )}
                  {tab === "Documentation" && (
                    <ModelEvidenceSection
                      mode="live"
                      title="Model documentation"
                      subtitle="Generated documents, source evidence and review progress"
                      icon={<FileText size={18} />}
                    >
                      <ModelDocuments key={selected.id} modelId={selected.id} />
                      <RunPanel
                        key={selected.id}
                        model={selected}
                        operations={["document"]}
                      />
                      <p>Open Review queue when the document is ready for review.</p>
                    </ModelEvidenceSection>
                  )}
                  {tab === "Rebuild & parity" && (
                    <ModelEvidenceSection
                      mode="live"
                      title="Implementation parity"
                      subtitle="Official parity is one platform check, run by the agent and by people"
                      icon={<GitBranch size={18} />}
                    >
                      <RunPanel
                        key={selected.id}
                        model={selected}
                        operations={["rebuild"]}
                      />
                      <ParityPanel
                        key={selected.id + "-parity"}
                        modelId={selected.id}
                        candidate={
                          derivedFrom(selected.inventory?.manifest)
                            ? { id: selected.id, version: selected.version }
                            : null
                        }
                      />
                    </ModelEvidenceSection>
                  )}
                  {tab === "Validation" && (
                    <ModelEvidenceSection
                      mode="live"
                      title="Validation report"
                      subtitle="Evidence is separate from human review and approval"
                      icon={<ShieldCheck size={18} />}
                    >
                      <RunPanel
                        key={selected.id + "-validation"}
                        model={selected}
                        operations={["validate"]}
                      />
                      <ModelDocuments
                        key={selected.id}
                        modelId={selected.id}
                        kind="validation"
                      />
                      <ModelFindings key={selected.id + ":findings"} modelId={selected.id} />
                      <ProposalSection key={selected.id+":proposals"} modelId={selected.id}/>
                      <p>
                        Read saved validation evidence here and use Review
                        queue for Gate 3 sign-off. PASS, FAIL, REVIEW,
                        NOT_ASSESSABLE and NOT_APPLICABLE remain distinct;
                        a completed run is not approval.
                      </p>
                    </ModelEvidenceSection>
                  )}
                  {tab === "History" && <RunHistory key={selected.id + ":" + revision} modelId={selected.id} />}
                  {tab === "Monitoring" && (
                    <> <ModelMonitoring key={selected.id} model={selected} refresh={revision} /><ProposalSection key={selected.id+":proposals"} modelId={selected.id}/></>
                  )}
                </ModelDetailTabs>
              </>
            ) : (
              !detailError && <p role="status">Loading model…</p>
            )}
          </>
        )}
        {page === "Datasets" && (
          <>
              <button className="button primary" disabled={!uploader} onClick={() => setUpload("dataset")}>
                Upload dataset
              </button>
            {!uploader && <p className="page-description">Dataset uploads require a developer or administrator account.</p>}
<p className="page-description">Upload datasets and inspect their versions and checks.</p>
            {loading ? (
              <p role="status">Loading datasets…</p>
            ) : error ? (
              <p role="alert">{error}</p>
            ) : (
              <DatasetSelection
                key={`${user.id}:${revision}`}
                datasets={datasets}
                canDelete={canDelete}
                requestDeletion={setDeletion}
                selectedId={datasetId}
                select={setDatasetId}
                findModels={findDatasetModels}
              />
            )}
          </>
        )}
        {page === "Review queue" && (
          <ReviewQueue key={revision} openModel={select} openIntake={() => setPage("Archive intake")} />
        )}
        {page === "Monitoring" && (
          <MonitoringPage refresh={revision}
            openModel={(modelId) => select(modelId, "Monitoring")} />
        )}
        {page === "Archive intake" && <IntakePage key={`${user.id}:${revision}`} openModel={select}/>}
        {page === "Agent proposals" && <LiveProposals key={revision}/>}
        {page === "Accounts" && user.role === "admin" && <AccountAdmin key={`${user.id}:${revision}`} />}
        {page === "Dashboard" && (
          <DashboardPage refresh={revision}
            openMonitoring={() => setPage("Monitoring")} />
        )}
      </main>
      <FloatingAgent model={selected} deletedModelIds={deletedModelIds} />
      <DeletionDialog key={user.id} actorId={user.id} allowed={canDelete} selection={deletion}
        close={() => setDeletion(null)} refresh={() => setRevision(v => v + 1)}
        completed={request => {
          if (request.kind === "models") {
            setDeletedModelIds(current => [...new Set([...current, ...request.ids])]);
            setModels(current => current.filter(m => !request.ids.includes(m.id)));
            if (request.ids.includes(id)) { setSelected(null); select(""); }
            // Return to a known page instead of leaving an empty last page selected.
            setOffset(0);
          } else {
            setDatasets(current => current.filter(d => !d.version_id || !request.ids.includes(d.version_id)));
            setDatasetId(null);
          }
          setRevision(v => v + 1);
        }} />
        </div>
      {upload && uploader && (
        <UploadDialog
          kind={upload}
          enabled={uploadsEnabled}
          close={() => setUpload(null)}
          complete={(value) => {
            const kind = upload;
            setUpload(null);
            setRevision((v) => v + 1);
            if (kind === "model") select(value);
            else {
              navigate({ datasetId: null, page: "Datasets" });
            }
          }}
        />
      )}
      </div>
    </div>
  );
}
