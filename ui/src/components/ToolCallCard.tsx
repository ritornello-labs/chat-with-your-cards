import type { ToolCallMessagePartProps } from "@assistant-ui/react";
import type { ToolCallResult } from "../store";

/**
 * Generic collapsible tool-call card, registered as MessagePrimitive.Parts'
 * `tools.Fallback` (src/App.tsx) so it renders every tool name uniformly.
 * assistant-ui derives `status` for us (see store.ts's header comment):
 * running while unresolved and the message is running, complete the instant
 * `result` is set - we only need to read it, not compute it.
 *
 * app.js additionally maps raw MCP tool names to friendly labels and hides
 * internal ones (search_notes -> "Searched your cards", ToolSearch hidden
 * entirely). Not ported here: left for the later restyle pass (see
 * ui/README.md).
 */
export function ToolCallCard(props: ToolCallMessagePartProps) {
  const { toolName, argsText, result, isError, status } = props;
  const running = status.type === "running";
  const typedResult = result as ToolCallResult | undefined;
  const finished = typedResult !== undefined;

  return (
    <div
      className={
        "cwyc-tool-chip" +
        (running ? " cwyc-tool-running" : finished ? (isError ? " cwyc-tool-failed" : " cwyc-tool-ok") : "")
      }
      data-testid="tool-chip"
    >
      <details>
        <summary>
          <CaretIcon />
          <span className="cwyc-tool-name">{toolName}</span>
          {running ? <span className="cwyc-ember cwyc-tool-result" aria-label="running" /> : null}
          {finished ? (
            <span className="cwyc-tool-result">{isError ? "✗ failed" : "✓ ok"}</span>
          ) : null}
        </summary>
        {argsText ? (
          <div className="cwyc-tool-detail-block">
            <div className="cwyc-tool-detail-label">Input</div>
            <pre className="cwyc-tool-detail-body">{argsText}</pre>
          </div>
        ) : null}
        {finished && typedResult?.summary ? (
          <div className="cwyc-tool-detail-block">
            <div className="cwyc-tool-detail-label">{isError ? "Error" : "Result"}</div>
            <pre className="cwyc-tool-detail-body">{typedResult.summary}</pre>
          </div>
        ) : null}
      </details>
    </div>
  );
}

function CaretIcon() {
  return (
    <svg
      className="cwyc-tool-caret"
      viewBox="0 0 8 8"
      width="8"
      height="8"
      aria-hidden="true"
    >
      <path d="M2.5 1l3 3-3 3" stroke="currentColor" strokeWidth="1.4" fill="none" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}
