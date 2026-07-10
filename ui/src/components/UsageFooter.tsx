import type { UsageSnapshot } from "../store";

/** Small footer indicator for the `usage` ChatEvent (cost + token totals). */
export function UsageFooter({ usage }: { usage: UsageSnapshot | null }) {
  if (!usage) return null;
  const parts: string[] = [];
  if (usage.costUsd !== null && usage.costUsd !== undefined) {
    parts.push("$" + usage.costUsd.toFixed(usage.costUsd < 1 ? 3 : 2));
  }
  const tokens = (usage.inputTokens ?? 0) + (usage.outputTokens ?? 0);
  if (tokens) {
    parts.push(tokens >= 1000 ? Math.round(tokens / 1000) + "k tokens" : tokens + " tokens");
  }
  if (!parts.length) return null;
  return <div className="cwyc-usage-chip">{parts.join(" · ")}</div>;
}
