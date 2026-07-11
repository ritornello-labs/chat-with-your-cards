import { useEffect, useRef, useState } from "react";
import type { ReasoningMessagePartProps } from "@assistant-ui/react";
import { THINKING_SENTINEL } from "../store";

/**
 * assistant-ui's Reasoning primitive: registered as
 * MessagePrimitive.Parts' `components.Reasoning` (src/App.tsx). Renders
 * {type:"reasoning"} parts, which store.ts now produces from real
 * thinking_delta events (landed 2026-07-11) as well as the dev replayer's
 * scripted ones.
 *
 * `estimatedTokens` is not part of assistant-ui's ReasoningMessagePart type,
 * but store.ts's part objects carry it as an extra field, and
 * MessageParts.js spreads the whole part object into this component's props
 * (`jsx(Reasoning, {...part})`) - confirmed by reading @assistant-ui/core's
 * shipped source. TypeScript accepts the wider prop type below because it
 * only ADDS an optional field to ReasoningMessagePartProps; any value of the
 * narrower type remains assignable to it.
 *
 * `text` is redacted upstream at every reasoning effort level observed from
 * the real CLI today, so this renders three states:
 *  - running, no real text yet (text === THINKING_SENTINEL): a rotating
 *    "Thinking…" phrase + a live "~N tokens" suffix once estimatedTokens is
 *    known - the *only* signal, since text stays empty throughout.
 *  - done, no real text ever arrived: a static one-line "Thought for ~N
 *    tokens" (or nothing at all if no token estimate was ever reported).
 *  - real text present (running or done): the original collapsible
 *    <details> block, future-proofed for whenever an account/effort level
 *    stops redacting thinking text.
 */
interface ReasoningBlockProps extends ReasoningMessagePartProps {
  readonly estimatedTokens?: number | null;
}

const THINKING_PHRASES = [
  "Thinking…",
  "Reasoning it through…",
  "Weighing the options…",
  "Lining up the facts…",
  "Turning the card over…",
];
const ROTATE_MS = 2500;

export function ReasoningBlock({ text, status, estimatedTokens }: ReasoningBlockProps) {
  const running = status.type === "running";
  const hasText = text !== THINKING_SENTINEL && text.trim().length > 0;
  const [open, setOpen] = useState(running);
  const wasRunning = useRef(running);
  const [phraseIndex, setPhraseIndex] = useState(0);

  useEffect(() => {
    // Auto-collapse the instant streaming finishes; a manual re-open after
    // that still works normally (this effect only fires on the transition).
    if (wasRunning.current && !running) {
      setOpen(false);
    }
    wasRunning.current = running;
  }, [running]);

  useEffect(() => {
    // Only the no-text indicator rotates; a real-text block shows the text
    // itself, not a phrase, so nothing to rotate there.
    if (!running || hasText) return;
    setPhraseIndex(0);
    const id = window.setInterval(() => {
      setPhraseIndex((i) => (i + 1) % THINKING_PHRASES.length);
    }, ROTATE_MS);
    return () => window.clearInterval(id);
  }, [running, hasText]);

  if (hasText) {
    return (
      <details
        className={"cwyc-reasoning" + (running ? " cwyc-reasoning-running" : "")}
        open={open}
        onToggle={(event) => setOpen(event.currentTarget.open)}
      >
        <summary>
          {running
            ? "Thinking…"
            : estimatedTokens
              ? `Thought for ~${estimatedTokens} tokens`
              : "Thought for a bit"}
        </summary>
        <div className="cwyc-reasoning-text">{text}</div>
      </details>
    );
  }

  if (running) {
    return (
      <div className="cwyc-reasoning cwyc-reasoning-indicator cwyc-reasoning-running">
        <span className="cwyc-reasoning-phrase">{THINKING_PHRASES[phraseIndex]}</span>
        {!!estimatedTokens && (
          <span className="cwyc-reasoning-tokens"> ~{estimatedTokens} tokens</span>
        )}
      </div>
    );
  }

  // Done, and no real thinking text ever arrived: collapse to a static
  // one-liner, or render nothing if no token estimate was ever reported.
  if (!estimatedTokens) return null;
  return (
    <div className="cwyc-reasoning cwyc-reasoning-indicator cwyc-reasoning-done">
      Thought for ~{estimatedTokens} tokens
    </div>
  );
}
