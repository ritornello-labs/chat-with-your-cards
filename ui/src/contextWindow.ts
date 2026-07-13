/**
 * Context-window sizes in tokens, hardcoded because the stream-json
 * protocol carries no window size at all - only cumulative per-turn usage
 * counts (backends/base.py's UsageUpdate docstring; DESIGN.md section 9).
 *
 * This is a TypeScript port of the same table in
 * chat_with_your_cards/backends/claude_cli.py's context_window_for() -
 * update both together when a new model generation ships. Keyed by our own
 * model aliases ("" | "opus" | "sonnet" | "fable" | "haiku", see
 * ComposerControls.tsx's MODELS) plus loose substring matches so a full
 * model id still resolves sensibly.
 *
 * 1,000,000: Opus 4.6/4.7/4.8, Sonnet 4.6/5, Fable 5, Mythos*.
 * 200,000: Sonnet <=4.5, Haiku <=4.5, and anything unrecognized (safe default).
 */

const CONTEXT_WINDOW_1M = 1_000_000;
const CONTEXT_WINDOW_200K = 200_000;

const KNOWN_ALIASES: Readonly<Record<string, number>> = {
  "": CONTEXT_WINDOW_200K, // unpinned "CLI default" - unknown, stay safe
  opus: CONTEXT_WINDOW_1M,
  sonnet: CONTEXT_WINDOW_1M,
  fable: CONTEXT_WINDOW_1M,
  haiku: CONTEXT_WINDOW_200K,
};

// Full-model-id substrings that mean "an older, 200k-window Sonnet" even
// though the family name "sonnet" alone maps to the current (1M) generation.
const OLD_SONNET_MARKERS = ["sonnet-4-5", "sonnet-3", "3-5-sonnet", "3-7-sonnet"];

export function contextWindowFor(model: string): number {
  const key = (model ?? "").trim().toLowerCase();
  if (key in KNOWN_ALIASES) return KNOWN_ALIASES[key];
  if (key.includes("opus") || key.includes("fable") || key.includes("mythos")) {
    return CONTEXT_WINDOW_1M;
  }
  if (key.includes("sonnet")) {
    if (OLD_SONNET_MARKERS.some((marker) => key.includes(marker))) return CONTEXT_WINDOW_200K;
    return CONTEXT_WINDOW_1M;
  }
  if (key.includes("haiku")) return CONTEXT_WINDOW_200K;
  return CONTEXT_WINDOW_200K;
}
