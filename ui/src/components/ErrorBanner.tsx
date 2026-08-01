import type { DataMessagePartProps } from "@assistant-ui/react";

/** Renders {type:"data", name:"error"} parts (the `error` ChatEvent). */
export function ErrorBanner({ data }: DataMessagePartProps<{ message: string }>) {
  return (
    <div className="cwyc-error-banner" role="alert" data-testid="error-banner">
      <strong>Error:</strong> {data.message}
    </div>
  );
}

/**
 * Tool calls the agent's permission layer refused this turn (#24c).
 *
 * Deliberately its own banner rather than an error: nothing failed - the mode
 * the user chose did exactly what it promised. The wording points at the fix
 * (a more permissive tier) instead of implying a bug.
 */
export function DenialBanner({
  data,
}: DataMessagePartProps<{ denials: { tool: string; detail: string }[] }>) {
  const denials: { tool: string; detail: string }[] = data.denials ?? [];
  if (!denials.length) return null;
  return (
    <div className="cwyc-denial-banner" role="status" data-testid="denial-banner">
      <div className="cwyc-denial-head">
        {denials.length === 1 ? "A tool call was blocked" : `${denials.length} tool calls were blocked`}{" "}
        by the current permission mode.
      </div>
      <ul className="cwyc-denial-list">
        {denials.map((denial, i) => (
          <li key={i}>
            <code>{denial.tool}</code>
            {denial.detail ? <span className="cwyc-denial-detail">{denial.detail}</span> : null}
          </li>
        ))}
      </ul>
    </div>
  );
}
