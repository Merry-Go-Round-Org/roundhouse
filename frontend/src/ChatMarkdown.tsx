import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { documentLink } from "./documentTransport";

/** Agent output is display content, never executable HTML or an approval control. */
export function ChatMarkdown({ text }: { text: string }) {
  return (
    <div className="chat-text chat-markdown">
      <Markdown
        remarkPlugins={[remarkGfm]}
        skipHtml
        urlTransform={(url) => documentLink(url) || ""}
        components={{
          a: ({ href, children }) =>
            href ? (
              <a href={href} target="_blank" rel="noopener noreferrer">
                {children}
              </a>
            ) : (
              <span>{children}</span>
            ),
          img: ({ alt }) => <span>[Image omitted{alt ? ": " + alt : ""}]</span>,
          table: ({ children }) => (
            <div
              className="chat-markdown-table"
              role="region"
              aria-label="Reply table"
              tabIndex={0}
            >
              <table>{children}</table>
            </div>
          ),
        }}
      >
        {text}
      </Markdown>
    </div>
  );
}
