import { useEffect, useRef, useState } from "react";
import type { ReasoningMessagePartProps } from "@assistant-ui/react";

/**
 * assistant-ui's Reasoning primitive: registered as
 * MessagePrimitive.Parts' `components.Reasoning` (src/App.tsx). Renders
 * {type:"reasoning"} parts, which today only the dev replayer emits (via the
 * thinking_delta stub - see events.ts and dev/replayer.ts) since no real
 * backend produces them yet. Collapsed by default once complete, expanded
 * automatically while still streaming so the "thinking" is visible live.
 */
export function ReasoningBlock({ text, status }: ReasoningMessagePartProps) {
  const running = status.type === "running";
  const [open, setOpen] = useState(running);
  const wasRunning = useRef(running);

  useEffect(() => {
    // Auto-collapse the instant streaming finishes; a manual re-open after
    // that still works normally (this effect only fires on the transition).
    if (wasRunning.current && !running) {
      setOpen(false);
    }
    wasRunning.current = running;
  }, [running]);

  if (!text.trim() && !running) return null;

  return (
    <details
      className={"cwyc-reasoning" + (running ? " cwyc-reasoning-running" : "")}
      open={open}
      onToggle={(event) => setOpen(event.currentTarget.open)}
    >
      <summary>{running ? "Thinking…" : "Thought for a bit"}</summary>
      <div className="cwyc-reasoning-text">{text}</div>
    </details>
  );
}
