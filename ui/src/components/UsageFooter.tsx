import { contextWindowFor } from "../contextWindow";
import type { UsageSnapshot } from "../store";

/** k/M token formatting: one decimal where useful, always rounded. */
function formatTokens(n: number): string {
  if (n >= 1_000_000) {
    const millions = n / 1_000_000;
    return (millions >= 10 ? Math.round(millions) : Math.round(millions * 10) / 10) + "M";
  }
  if (n >= 1_000) {
    const thousands = n / 1_000;
    return (thousands >= 10 ? Math.round(thousands) : Math.round(thousands * 10) / 10) + "k";
  }
  return String(Math.round(n));
}

/** Small footer indicator for the `usage` ChatEvent (cost + token totals +
 * a context-window bar). The window size comes from the CLI's own per-turn
 * report when available (usage.contextWindow), falling back to the hardcoded
 * per-model table (contextWindow.ts) for scripted/older backends. Recomputes
 * whenever the usage snapshot or the current model changes. */
export function UsageFooter({ usage, model }: { usage: UsageSnapshot | null; model: string }) {
  if (!usage) return null;
  const parts: string[] = [];
  if (usage.costUsd !== null && usage.costUsd !== undefined) {
    parts.push("$" + usage.costUsd.toFixed(usage.costUsd < 1 ? 3 : 2));
  }
  const tokens = (usage.inputTokens ?? 0) + (usage.outputTokens ?? 0);
  if (tokens) {
    parts.push(tokens >= 1000 ? Math.round(tokens / 1000) + "k tokens" : tokens + " tokens");
  }

  // Context used ~= the size of the last turn's request context: input plus
  // whatever it read from/wrote to the prompt cache (base.py's UsageUpdate
  // docstring). There is no cumulative summing across turns here - each
  // turn's own input_tokens already reflects the full, growing conversation
  // sent as that call's input.
  const contextUsed =
    (usage.inputTokens ?? 0) + (usage.cacheReadTokens ?? 0) + (usage.cacheCreationTokens ?? 0);
  // Prefer the CLI's real reported window; fall back to the per-model table.
  const window =
    usage.contextWindow && usage.contextWindow > 0 ? usage.contextWindow : contextWindowFor(model);
  const hasContext = contextUsed > 0;
  const pct = hasContext ? Math.min(100, (contextUsed / window) * 100) : 0;
  const warn = pct > 80;

  if (!parts.length && !hasContext) return null;

  return (
    <div className="cwyc-usage-chip">
      {parts.length ? <span className="cwyc-usage-text">{parts.join(" · ")}</span> : null}
      {hasContext ? (
        <span className="cwyc-usage-context" title={`${contextUsed.toLocaleString()} / ${window.toLocaleString()} tokens of context used`}>
          <span className="cwyc-usage-bar">
            <span
              className={"cwyc-usage-bar-fill" + (warn ? " cwyc-usage-bar-fill-warn" : "")}
              style={{ width: `${pct}%` }}
            />
          </span>
          <span className={"cwyc-usage-context-label" + (warn ? " cwyc-usage-context-label-warn" : "")}>
            {formatTokens(contextUsed)} / {formatTokens(window)} context
          </span>
        </span>
      ) : null}
    </div>
  );
}
