import {
  Children,
  isValidElement,
  type ReactNode,
  createElement,
  useEffect,
  useId,
  useRef,
  useState,
} from "react";
import Markdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import { SourceCitation } from "./SourceCitation";
import { sourcePath } from "./modelFiles";
import { documentLink } from "./documentTransport";
function plainText(children: ReactNode): string {
  return Children.toArray(children)
    .map((child) =>
      typeof child === "string" || typeof child === "number"
        ? String(child)
        : isValidElement<{ children?: ReactNode }>(child)
          ? plainText(child.props.children)
          : "",
    )
    .join("");
}
export function DocumentReader({
  source,
  reportId,
  modelId,
}: {
  source: string;
  reportId: string;
  modelId?: string;
}) {
  const root = useRef<HTMLDivElement>(null);
  const prefix = useId();
  const [sections, setSections] = useState<{ id: string; text: string }[]>([]);
  useEffect(() => {
    setSections(
      Array.from(
        root.current?.querySelectorAll<HTMLElement>("h1,h2,h3,h4,h5,h6") ?? [],
      ).map((h) => ({ id: h.id, text: h.textContent ?? "Section" })),
    );
  }, [source, reportId]);
  const heading: Components["h2"] = ({ node, children }) => {
    const tag = node?.tagName ?? "h2";
    const id = prefix + "-section-" + (node?.position?.start.offset ?? 0);
    const label = plainText(children).toLowerCase();
    const className = label.includes("missing information")
      ? "document-missing"
      : label.includes("conflicts with")
        ? "document-conflicts"
        : undefined;
    return createElement(tag, { id, tabIndex: -1, className }, children);
  };
  return (
    <section className="document-reader" aria-label="Document reader">
      <h3>Document preview · {reportId}</h3>
      <p className="notice">
        Document content is evidence, not approval. Review status belongs to
        its version-bound review record. Check claims against cited evidence.
      </p>
      <p>
        External HTTPS links open in a new tab. Supported source citations can
        be opened only when listed by the backend for this model. A historical
        report may refer to another package version. Images and embedded HTML
        are not loaded.
      </p>
      <nav aria-label="Document sections">
        {sections.map((s) => (
          <button
            className="button secondary"
            key={s.id}
            onClick={() => {
              const element = document.getElementById(s.id);
              element?.scrollIntoView({ block: "start" });
              element?.focus({ preventScroll: true });
            }}
          >
            {s.text}
          </button>
        ))}
      </nav>
      <div className="document-body" ref={root}>
        <Markdown
          remarkPlugins={[remarkGfm]}
          skipHtml
          urlTransform={url => documentLink(url) || (modelId && sourcePath(url) ? url : "")}
          components={{
            h1: heading,
            h2: heading,
            h3: heading,
            h4: heading,
            h5: heading,
            h6: heading,
            a: ({ href, children }) =>
              href && modelId && sourcePath(href) ? (
                <SourceCitation key={`${modelId}:${href}`} modelId={modelId} href={href}>{children}</SourceCitation>
              ) : href ? (
                <a href={href} target="_blank" rel="noopener noreferrer">
                  {children}
                </a>
              ) : (
                <span>
                  {children} <small>(source link unavailable)</small>
                </span>
              ),
            img: ({ alt }) => (
              <span>[Image omitted{alt ? ": " + alt : ""}]</span>
            ),
            table: ({ children }) => (
              <div className="document-table">
                <table>{children}</table>
              </div>
            ),
          }}
        >
          {source}
        </Markdown>
      </div>
      <details>
        <summary>Original Markdown source</summary>
        <pre>{source}</pre>
      </details>
    </section>
  );
}
