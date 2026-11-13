import {
  useEffect,
  useRef,
  useState,
  type FormEvent,
  type ReactNode,
} from "react";
import {
  ArrowDownToLine,
  ArrowRight,
  Bell,
  BookOpen,
  Box,
  Check,
  ChevronRight,
  CircleHelp,
  FileText,
  GitBranch,
  Layers3,
  LayoutDashboard,
  Plus,
  Search,
  ShieldCheck,
  SlidersHorizontal,
  X,
  Activity,
  AlertTriangle,
  CircleDot,
} from "lucide-react";
import { ModelDetailTabs } from "./ModelDetailTabs";
import { ModelEvidenceSection } from "./ModelEvidenceSection";
import { ModelRecordCard } from "./ModelRecordCard";
import { demoModelFacts } from "./modelDetailAdapters";
import { InventoryTable } from "./InventoryTable";
import { demoInventoryRow } from "./inventoryAdapters";
import { demoModels } from "./demo";
import {
  canAccept,
  draftDocument,
  reviewModel,
  type Model,
  type Status,
} from "./domain";

type View = "Inventory" | "Review queue" | "Monitoring";
type Tab = "Overview" | "Documentation" | "Rebuild & parity" | "Validation";
const demoDetailTabs: readonly Tab[] = ["Overview", "Documentation", "Rebuild & parity", "Validation"];
const statusLabel: Record<Status, string> = {
  green: "Pass",
  amber: "Needs review",
  red: "Action required",
  not_assessable: "Not assessable",
};
function Badge({
  status,
  children,
}: {
  status?: Status;
  children?: ReactNode;
}) {
  return (
    <span className={`badge ${status ?? "neutral"}`}>
      <span className="badge-dot" />
      {children ?? (status && statusLabel[status])}
    </span>
  );
}
function download(name: string, value: unknown) {
  const url = URL.createObjectURL(
    new Blob([JSON.stringify(value, null, 2)], { type: "application/json" }),
  );
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
function Modal({
  title,
  close,
  children,
}: {
  title: string;
  close: () => void;
  children: ReactNode;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    ref.current?.showModal();
  }, []);
  return (
    <dialog
      ref={ref}
      onCancel={(e) => {
        e.preventDefault();
        close();
      }}
      aria-labelledby="modal-title"
    >
      <div className="modal-head">
        <h2 id="modal-title">{title}</h2>
        <button
          className="icon-button"
          aria-label="Close dialog"
          onClick={close}
        >
          <X size={20} />
        </button>
      </div>
      {children}
    </dialog>
  );
}

export function App() {
  const [models, setModels] = useState<Model[]>(() =>
    structuredClone(demoModels),
  );
  const [view, setView] = useState<View>("Inventory");
  const [selected, setSelected] = useState<string | null>(null);
  const [tab, setTab] = useState<Tab>("Overview");
  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState("All models");
  const [importing, setImporting] = useState(false);
  const [reviewing, setReviewing] = useState(false);
  const [help, setHelp] = useState(false);
  const [toast, setToast] = useState("");
  const current = models.find((m) => m.id === selected);
  const notify = (message: string) => setToast(message);
  useEffect(() => {
    if (!toast) return;
    const timer = setTimeout(() => setToast(""), 6000);
    return () => clearTimeout(timer);
  }, [toast]);
  const update = (model: Model) =>
    setModels((ms) => ms.map((m) => (m.id === model.id ? model : m)));
  const open = (model: Model, nextTab: Tab = "Overview") => {
    setSelected(model.id);
    setTab(nextTab);
  };
  const pending = models.filter((m) => m.stage === "Validation" && !m.review);
  const alerts = models.flatMap((m) =>
    m.metrics
      .filter((t) => t.status === "red")
      .map((t) => ({ model: m, metric: t })),
  );
  const filtered = models.filter(
    (m) =>
      (filter === "All models" || m.type === filter) &&
      `${m.name} ${m.id} ${m.owner ?? ""} ${m.dataset}`
        .toLowerCase()
        .includes(query.toLowerCase()),
  );
  const heading = current
    ? current.name
    : view === "Inventory"
      ? "Model inventory"
      : view === "Review queue"
        ? "Human review queue"
        : "Monitoring";
  return (
    <div className="app-shell">
      <a className="skip" href="#main">
        Skip to content
      </a>
      <aside className="sidebar">
        <div className="brand">
          <div className="brand-icon">
            <Layers3 size={24} />
          </div>
          <div>
            roundhouse<span>MODEL RISK WORKBENCH</span>
          </div>
        </div>
        <div className="workspace">
          <span className="workspace-mark">M</span>
          <div>
            Merry Go Round<small>Challenge 04 workspace</small>
          </div>
          <ChevronRight size={15} />
        </div>
        <p className="nav-label">WORKSPACE</p>
        <nav aria-label="Main navigation">
          {(
            [
              { name: "Inventory", icon: LayoutDashboard },
              { name: "Review queue", icon: ShieldCheck },
              { name: "Monitoring", icon: Activity },
            ] as const
          ).map(({ name, icon: Icon }) => (
            <button
              key={name}
              className={view === name ? "active" : ""}
              onClick={() => {
                setView(name);
                setSelected(null);
              }}
            >
              <Icon size={18} />
              {name}
              {name === "Review queue" && (
                <span className="nav-count">{pending.length}</span>
              )}
              {name === "Monitoring" && alerts.length > 0 && (
                <span className="alert-dot" />
              )}
            </button>
          ))}
        </nav>
        <div className="sidebar-note">
          <GitBranch size={19} />
          <strong>Evidence before approval.</strong>
          <p>Follow every model from legacy code to an independent review.</p>
        </div>
        <div className="sidebar-bottom">
          <button onClick={() => setHelp(true)}>
            <CircleHelp size={18} />
            Workspace guide
          </button>
          <div className="profile">
            <span>YN</span>
            <div>
              Yuki<small>Frontend workspace</small>
            </div>
            <span className="online" />
          </div>
        </div>
      </aside>
      <div className="main-shell">
        <header className="topbar">
          <div>
            Merry Go Round <ChevronRight size={14} />
            <span>{current ? "Model detail" : view}</span>
          </div>
          <div>
            <span className="demo-pill">
              <span />
              Demo workspace
            </span>
            <button
              aria-label="Open monitoring alerts"
              className="icon-button"
              onClick={() => {
                setView("Monitoring");
                setSelected(null);
              }}
            >
              <Bell size={19} />
            </button>
            <span className="avatar">YN</span>
          </div>
        </header>
        <main id="main">
          <div className="page-heading">
            <div>
              {current ? (
                <button className="back" onClick={() => setSelected(null)}>
                  ← Back to {view.toLowerCase()}
                </button>
              ) : (
                <p className="eyebrow">MODEL GOVERNANCE / CHALLENGE 04</p>
              )}
              <h1>{heading}</h1>
              <p>
                {current
                  ? `${current.id} · ${current.version} · ${current.dataset === "UCI" ? "UCI Credit Default" : "ULB Credit Card Fraud"}`
                  : view === "Inventory"
                    ? "One place to document, rebuild and challenge your models."
                    : view === "Review queue"
                      ? "Independent challenge. Explicit decisions. A traceable record."
                      : "Inspect drift signals and decide what needs another look."}
              </p>
            </div>
            <div className="heading-actions">
              <a className="button secondary" href="/live">
                Open connected workspace
              </a>
              <button
                className="button secondary"
                onClick={() =>
                  download("roundhouse-demo-inventory.json", {
                    demo: true,
                    exportedAt: new Date().toISOString(),
                    models,
                  })
                }
              >
                <ArrowDownToLine size={16} />
                Export inventory
              </button>
              {!current && view === "Inventory" && (
                <button
                  className="button primary"
                  onClick={() => setImporting(true)}
                >
                  <Plus size={17} />
                  Register model
                </button>
              )}
            </div>
          </div>
          <div className="demo-banner">
            <CircleDot size={16} />
            <span>
              <strong>Prototype preview.</strong> Model records and metrics are
              illustrative. Actions stay in this session; no backend jobs or
              approvals are executed.
            </span>
          </div>
          {current ? (
            <>
              <ModelDetailTabs
                tabs={demoDetailTabs}
                value={tab}
                onChange={setTab}
                panelClassName="detail-grid"
              >
                <div className="detail-main">
                  {tab === "Overview" && (
                    <>
                      <ModelRecordCard
                        mode="demo"
                        title="Model record"
                        subtitle="Purpose, ownership and operating boundaries"
                        icon={<Box size={18} />}
                        facts={demoModelFacts(current)}
                        purpose={current.purpose}
                      />
                      <section className="panel">
                        <PanelTitle
                          icon={<GitBranch size={18} />}
                          title="Lifecycle"
                          subtitle="Each transition needs supporting evidence"
                        />
                        <div className="lifecycle">
                          {(
                            [
                              "Inventory",
                              "Documentation",
                              "Rebuild",
                              "Validation",
                            ] as const
                          ).map((s, i) => (
                            <button
                              key={s}
                              onClick={() =>
                                setTab(
                                  (
                                    [
                                      "Overview",
                                      "Documentation",
                                      "Rebuild & parity",
                                      "Validation",
                                    ] as Tab[]
                                  )[i],
                                )
                              }
                            >
                              <span
                                className={
                                  current.stage === s ? "step current" : "step"
                                }
                              >
                                {i + 1}
                              </span>
                              <strong>{s}</strong>
                              <small>
                                {current.stage === s
                                  ? "Current stage"
                                  : "Inspect evidence"}
                              </small>
                            </button>
                          ))}
                        </div>
                      </section>
                    </>
                  )}
                  {tab === "Documentation" && (
                    <ModelEvidenceSection
                      mode="demo"
                      title="Model documentation"
                      subtitle="Nine sections, with missing evidence kept visible"
                      icon={<FileText size={18} />}
                    >
                        <p>
                          Prepare a reviewable template from the inventory.
                          Source extraction and AI drafting will be supplied by
                          the backend.
                        </p>
                        <button
                          className="button primary"
                          onClick={() => {
                            update({
                              ...current,
                              document: draftDocument(current),
                            });
                            notify(
                              "Demo document template prepared. Human review is still required.",
                            );
                          }}
                        >
                          <FileText size={16} />
                          {current.document
                            ? "Refresh demo template"
                            : "Prepare demo template"}
                        </button>
                        {current.document ? (
                          <>
                            <pre className="document-preview">
                              {current.document}
                            </pre>
                            <button
                              className="button secondary"
                              onClick={() =>
                                download(`${current.id}-demo-document.json`, {
                                  demo: true,
                                  modelId: current.id,
                                  document: current.document,
                                })
                              }
                            >
                              <ArrowDownToLine size={16} />
                              Export document
                            </button>
                          </>
                        ) : (
                          <div className="empty compact">
                            <BookOpen size={32} />
                            <h3>No document attached</h3>
                            <p>
                              Start with a template. Unknown methodology and
                              test evidence remain explicit.
                            </p>
                          </div>
                        )}
                    </ModelEvidenceSection>
                  )}
                  {tab === "Rebuild & parity" && (
                    <ModelEvidenceSection
                      mode="demo"
                      title="Implementation parity"
                      subtitle="Equivalent outputs do not establish model validity"
                      icon={<GitBranch size={18} />}
                    >
                        <div className="migration">
                          <span>
                            {current.language}
                            <small>Legacy implementation</small>
                          </span>
                          <ArrowRight size={24} />
                          <span>
                            Python<small>Rebuilt implementation</small>
                          </span>
                        </div>
                        <Badge status={current.parity.status} />
                        <dl className="record-grid">
                          <div>
                            <dt>Rows compared</dt>
                            <dd>
                              {current.parity.rows?.toLocaleString() ??
                                "Not available"}
                            </dd>
                          </div>
                          <div>
                            <dt>Maximum absolute difference</dt>
                            <dd>{current.parity.maxDifference}</dd>
                          </div>
                          <div>
                            <dt>Tolerance</dt>
                            <dd>{current.parity.tolerance}</dd>
                          </div>
                          <div>
                            <dt>Evidence provenance</dt>
                            <dd>Demo fixture only</dd>
                          </div>
                        </dl>
                        <p>{current.parity.reason}</p>
                        <div className="notice">
                          Rebuild execution is awaiting the agent/backend
                          integration. No source code is executed in this
                          browser.
                        </div>
                    </ModelEvidenceSection>
                  )}
                  {tab === "Validation" && (
                    <ModelEvidenceSection
                      mode="demo"
                      title="Validation report"
                      subtitle="Separate performance, stability and fairness evidence"
                      icon={<ShieldCheck size={18} />}
                    >
                        <div className="report-toolbar">
                          <Badge
                            status={
                              current.metrics.some((m) => m.status === "red")
                                ? "red"
                                : current.metrics.length
                                  ? "amber"
                                  : "not_assessable"
                            }
                          />
                          <button
                            className="button secondary small"
                            onClick={() =>
                              download(`${current.id}-demo-validation.json`, {
                                demo: true,
                                modelId: current.id,
                                metrics: current.metrics,
                                limitations: current.limitations,
                                review: current.review,
                              })
                            }
                          >
                            <ArrowDownToLine size={15} />
                            Export report
                          </button>
                        </div>
                        {current.metrics.length ? (
                          current.metrics.map((m) => (
                            <article className="metric-result" key={m.name}>
                              <div>
                                <h3>{m.name}</h3>
                                <Badge status={m.status} />
                              </div>
                              <strong className="metric-value">
                                {m.value}
                              </strong>
                              <p>{m.reason}</p>
                              <small>
                                {m.required
                                  ? "Required assessment"
                                  : "Supporting diagnostic"}{" "}
                                · Demo evidence
                              </small>
                            </article>
                          ))
                        ) : (
                          <div className="empty">
                            <ShieldCheck size={32} />
                            <h3>No validation evidence</h3>
                            <p>
                              Metrics will appear when a backend validation run
                              produces evidence.
                            </p>
                          </div>
                        )}
                    </ModelEvidenceSection>
                  )}
                </div>
                <aside className="detail-side">
                  <section className="panel">
                    <PanelTitle title="Review status" />
                    <div className="section-content">
                      <Badge
                        status={
                          current.review?.decision === "Accepted"
                            ? "green"
                            : "amber"
                        }
                      >
                        {current.review?.decision ?? "Awaiting human review"}
                      </Badge>
                      <p>
                        Model checks and human approval are separate decisions.
                      </p>
                      <button
                        className="button primary full"
                        onClick={() => setReviewing(true)}
                      >
                        Record demo review
                        <ArrowRight size={16} />
                      </button>
                      {current.review && (
                        <div className="review-record">
                          <strong>{current.review.reviewer}</strong>
                          <p>{current.review.note}</p>
                          <small>
                            {new Date(current.review.at).toLocaleString()}
                          </small>
                        </div>
                      )}
                    </div>
                  </section>
                  <section className="panel limitations">
                    <PanelTitle
                      icon={<AlertTriangle size={17} />}
                      title="Known limitations"
                    />
                    <ul>
                      {current.limitations.map((l) => (
                        <li key={l}>{l}</li>
                      ))}
                    </ul>
                  </section>
                  <p className="side-footnote">
                    No compliance or deployment-readiness claim is made by this
                    prototype.
                  </p>
                </aside>
              </ModelDetailTabs>
            </>
          ) : view === "Inventory" ? (
            <>
              <section className="stats" aria-label="Inventory summary">
                <Stat
                  label="Models in inventory"
                  value={models.length.toString()}
                  note="Credit & fraud models"
                  icon={<Box size={20} />}
                />
                <Stat
                  label="Awaiting review"
                  value={pending.length.toString().padStart(2, "0")}
                  note="Human decisions required"
                  icon={<ShieldCheck size={20} />}
                />
                <Stat
                  label="Documentation"
                  value={`${models.filter((m) => m.document).length} / ${models.length}`}
                  note="Demo templates prepared"
                  icon={<FileText size={20} />}
                />
                <Stat
                  label="Open drift signals"
                  value={alerts.length.toString().padStart(2, "0")}
                  note="Illustrative monitoring alert"
                  icon={<Activity size={20} />}
                />
              </section>
              <div className="inventory-layout">
                <section className="panel inventory-panel">
                  <PanelTitle
                    title="Your model portfolio"
                    subtitle="Track the evidence behind every decision."
                  />
                  <div className="table-tools">
                    <label className="search">
                      <Search size={17} />
                      <input
                        aria-label="Search models"
                        placeholder="Search name, ID, owner or dataset…"
                        value={query}
                        onChange={(e) => setQuery(e.target.value)}
                      />
                    </label>
                    <label className="filter">
                      <SlidersHorizontal size={15} />
                      <select
                        aria-label="Filter model type"
                        value={filter}
                        onChange={(e) => setFilter(e.target.value)}
                      >
                        <option>All models</option>
                        <option>Credit</option>
                        <option>Fraud</option>
                      </select>
                    </label>
                  </div>
                  <InventoryTable
                    rows={filtered.map(demoInventoryRow)}
                    statusHeading="Workflow stage (demo)"
                    onOpen={(modelId) => {
                      const model = models.find((m) => m.id === modelId);
                      if (model) open(model);
                    }}
                  />
                  {!filtered.length && (
                    <div className="empty">
                      <Search size={28} />
                      <h3>No matching models</h3>
                      <p>Try another search or change the model type.</p>
                      <button
                        className="button secondary"
                        onClick={() => {
                          setQuery("");
                          setFilter("All models");
                        }}
                      >
                        Clear filters
                      </button>
                    </div>
                  )}
                  <div className="table-footer">
                    Showing {filtered.length} of {models.length} models
                    <span>Session-only demo records</span>
                  </div>
                </section>
                <aside className="portfolio-side">
                  <section className="next-review">
                    <div className="mini-heading">
                      <span className="light-icon">
                        <ShieldCheck size={20} />
                      </span>
                      <span>YOUR NEXT STEP</span>
                    </div>
                    <h2>
                      Put evidence
                      <br />
                      under review.
                    </h2>
                    <p>
                      {pending.length} models are waiting for independent
                      challenge. Inspect the findings before recording a
                      decision.
                    </p>
                    <button
                      onClick={() => {
                        setView("Review queue");
                        setSelected(null);
                      }}
                    >
                      Open review queue
                      <ArrowRight size={17} />
                    </button>
                    <div className="review-stack">
                      <span>R</span>
                      <span>Py</span>
                      <small>Credit + fraud</small>
                    </div>
                  </section>
                  <section className="panel dataset-card">
                    <h3>Research in context</h3>
                    <div>
                      <span className="dataset-mark">01</span>
                      <p>
                        <strong>UCI Credit Default</strong>
                        <small>Snapshot cohort · next-month default</small>
                      </p>
                    </div>
                    <div>
                      <span className="dataset-mark">02</span>
                      <p>
                        <strong>ULB Credit Card Fraud</strong>
                        <small>Two-day window · fraud outcomes</small>
                      </p>
                    </div>
                    <p className="dataset-note">
                      Dataset selection does not establish model validity. Keep
                      limitations visible.
                    </p>
                  </section>
                </aside>
              </div>
              <section className="workflow-strip">
                <div>
                  <span className="eyebrow">A TRACEABLE MODEL LIFECYCLE</span>
                  <h3>From legacy code to reviewed evidence</h3>
                </div>
                {["Register", "Document", "Rebuild", "Validate"].map((s, i) => (
                  <div key={s}>
                    <span>{String(i + 1).padStart(2, "0")}</span>
                    <strong>{s}</strong>
                    {i < 3 && <ChevronRight size={16} />}
                  </div>
                ))}
              </section>
            </>
          ) : view === "Review queue" ? (
            <section className="panel">
              <PanelTitle
                title="Reviewable models"
                subtitle="Record an opinion with a reviewer and supporting rationale."
              />
              {models
                .filter((m) => m.stage === "Validation")
                .map((m) => (
                  <div className="queue-item" key={m.id}>
                    <span className="model-icon">
                      {m.language === "R" ? "R" : "Py"}
                    </span>
                    <div>
                      <h3>{m.name}</h3>
                      <p>
                        {m.id} ·{" "}
                        {
                          m.metrics.filter(
                            (x) => x.required && x.status !== "green",
                          ).length
                        }{" "}
                        required checks unresolved
                      </p>
                    </div>
                    <Badge status="amber">
                      {m.review?.decision ?? "Awaiting review"}
                    </Badge>
                    <button
                      className="button secondary"
                      onClick={() => open(m, "Validation")}
                    >
                      Inspect report
                      <ArrowRight size={16} />
                    </button>
                  </div>
                ))}
            </section>
          ) : (
            <section className="panel">
              <PanelTitle
                title="Drift review events"
                subtitle="Demo alerts; monitoring will run in the Python backend."
              />
              {alerts.length ? (
                alerts.map(({ model, metric }) => (
                  <div
                    className="monitor-event"
                    key={`${model.id}-${metric.name}`}
                  >
                    <span className="event-icon">
                      <AlertTriangle size={22} />
                    </span>
                    <div>
                      <Badge status="red" />
                      <h2>{model.name}</h2>
                      <p>
                        {metric.name}: <strong>{metric.value}</strong> ·{" "}
                        {metric.reason}
                      </p>
                      <p className="muted">
                        A drift alert triggers investigation, not automatic
                        retraining or approval.
                      </p>
                      <button
                        className="button secondary"
                        onClick={() => open(model, "Validation")}
                      >
                        Inspect model evidence
                        <ArrowRight size={16} />
                      </button>
                    </div>
                  </div>
                ))
              ) : (
                <div className="empty">
                  <Check size={30} />
                  <h3>No drift signals</h3>
                  <p>No red drift checks appear in the current demo records.</p>
                </div>
              )}
            </section>
          )}
          <footer className="page-footer">
            <span>Roundhouse / Merry Go Round</span>
            <span>
              Public & synthetic data only <span className="footer-dot">·</span>{" "}
              Challenge 04
            </span>
          </footer>
        </main>
      </div>
      {toast && (
        <div role="status" className="toast">
          <Check size={18} />
          {toast}
          <button
            onClick={() => setToast("")}
            aria-label="Dismiss notification"
          >
            <X size={16} />
          </button>
        </div>
      )}
      {importing && (
        <ImportModal
          close={() => setImporting(false)}
          save={(model) => {
            setModels((ms) => [...ms, model]);
            setImporting(false);
            open(model);
            notify(
              "Demo inventory record registered. No file was uploaded or executed.",
            );
          }}
        />
      )}
      {reviewing && current && (
        <ReviewModal
          model={current}
          close={() => setReviewing(false)}
          save={(m) => {
            update(m);
            setReviewing(false);
            notify("Demo review recorded for this session.");
          }}
        />
      )}
      {help && (
        <Modal title="Workspace guide" close={() => setHelp(false)}>
          <div className="section-content">
            <p>
              This frontend demonstrates Challenge 04 with illustrative model
              records. It is not connected to a backend.
            </p>
            <ol className="help-list">
              <li>Register a model and inspect its purpose and limitations.</li>
              <li>Prepare a demo documentation template.</li>
              <li>
                Inspect illustrative migration parity separately from
                validation.
              </li>
              <li>Review unresolved findings and record a demo decision.</li>
            </ol>
            <p>
              Reloading resets the workspace. Export records to keep a copy.
              Review names are not authenticated; the backend must enforce
              identity, independence and approval rules.
            </p>
            <button className="button primary" onClick={() => setHelp(false)}>
              Got it
            </button>
          </div>
        </Modal>
      )}
    </div>
  );
}
function PanelTitle({
  title,
  subtitle,
  icon,
}: {
  title: string;
  subtitle?: string;
  icon?: ReactNode;
}) {
  return (
    <div className="panel-title">
      <div>
        {icon}
        <h2>{title}</h2>
      </div>
      {subtitle && <p>{subtitle}</p>}
    </div>
  );
}
function Stat({
  label,
  value,
  note,
  icon,
}: {
  label: string;
  value: string;
  note: string;
  icon: ReactNode;
}) {
  return (
    <article className="stat">
      <div>
        <span>{label}</span>
        {icon}
      </div>
      <strong>{value}</strong>
      <p>{note}</p>
    </article>
  );
}
function ImportModal({
  close,
  save,
}: {
  close: () => void;
  save: (model: Model) => void;
}) {
  const [error, setError] = useState("");
  function submit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const d = new FormData(e.currentTarget);
    const name = String(d.get("name") ?? "").trim();
    if (!name) {
      setError("Enter a model name.");
      return;
    }
    const dataset = d.get("dataset") === "ULB" ? "ULB" : "UCI";
    save({
      id: `DEMO-${crypto.randomUUID().slice(0, 8)}`,
      name,
      owner: String(d.get("owner") ?? "").trim() || null,
      type: dataset === "UCI" ? "Credit" : "Fraud",
      language: d.get("language") === "R" ? "R" : "Python",
      version: "v0.1",
      dataset,
      stage: "Inventory",
      tier: "Medium",
      purpose:
        String(d.get("purpose") ?? "").trim() ||
        "Intended use not yet documented.",
      limitations: [
        dataset === "UCI"
          ? "No separately labelled calendar cohorts."
          : "Protected-group fairness cannot be assessed from ULB fields.",
        "Source, data checksum and methodology await evidence.",
      ],
      document: null,
      metrics: [],
      review: null,
      parity: {
        status: "not_assessable",
        rows: null,
        maxDifference: "—",
        tolerance: "Not agreed",
        reason: "No rebuild evidence attached.",
      },
    });
  }
  return (
    <Modal title="Register a model" close={close}>
      <form onSubmit={submit} className="form">
        <p>
          Create a demo inventory record. Source-file import will be connected
          to the backend later.
        </p>
        <label>
          Model name
          <input
            name="name"
            required
            maxLength={100}
            autoFocus
            placeholder="e.g. Credit default challenger"
          />
        </label>
        <div className="form-row">
          <label>
            Dataset
            <select name="dataset" aria-label="Dataset">
              <option value="UCI">UCI · Credit default</option>
              <option value="ULB">ULB · Transaction fraud</option>
            </select>
          </label>
          <label>
            Source language
            <select name="language" aria-label="Source language">
              <option>R</option>
              <option>Python</option>
            </select>
          </label>
        </div>
        <label>
          Model owner <small>(optional)</small>
          <input
            name="owner"
            maxLength={100}
            placeholder="Leave blank if unknown"
          />
        </label>
        <label>
          Intended use
          <textarea
            name="purpose"
            maxLength={1000}
            rows={3}
            placeholder="What decision does the model support?"
          />
        </label>
        {error && (
          <p role="alert" className="form-error">
            {error}
          </p>
        )}
        <div className="form-actions">
          <button className="button secondary" type="button" onClick={close}>
            Cancel
          </button>
          <button className="button primary" type="submit">
            Register demo model
          </button>
        </div>
      </form>
    </Modal>
  );
}
function ReviewModal({
  model,
  close,
  save,
}: {
  model: Model;
  close: () => void;
  save: (m: Model) => void;
}) {
  const [error, setError] = useState("");
  function submit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const d = new FormData(e.currentTarget);
    try {
      save(
        reviewModel(
          model,
          String(d.get("reviewer") ?? ""),
          d.get("decision") === "Accepted" ? "Accepted" : "Changes requested",
          String(d.get("note") ?? ""),
        ),
      );
    } catch (err) {
      setError((err as Error).message);
    }
  }
  return (
    <Modal title="Record a demo review" close={close}>
      <form className="form" onSubmit={submit}>
        <p>
          {model.id} · {model.name}
        </p>
        {!canAccept(model) && (
          <div className="notice">
            Acceptance is unavailable while required checks or parity are
            unresolved. Record the changes needed; a missing assessment is not a
            pass.
          </div>
        )}
        <label>
          Reviewer name
          <input
            name="reviewer"
            required
            maxLength={100}
            autoFocus
            placeholder="Independent reviewer"
          />
        </label>
        <label>
          Decision
          <select name="decision" aria-label="Decision">
            <option>Changes requested</option>
            <option disabled={!canAccept(model)}>Accepted</option>
          </select>
        </label>
        <label>
          Reason and next actions
          <textarea
            name="note"
            required
            maxLength={2000}
            rows={4}
            placeholder="Describe the finding, action and evidence needed."
          />
        </label>
        <small>
          Demo only: identity and organisational independence must be enforced
          by the backend.
        </small>
        {error && (
          <p role="alert" className="form-error">
            {error}
          </p>
        )}
        <div className="form-actions">
          <button type="button" className="button secondary" onClick={close}>
            Cancel
          </button>
          <button className="button primary" type="submit">
            Save demo review
          </button>
        </div>
      </form>
    </Modal>
  );
}
