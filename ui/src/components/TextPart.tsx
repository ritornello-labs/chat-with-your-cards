import { useEffect, useRef, useState } from "react";
import type { TextMessagePartProps } from "@assistant-ui/react";
import { renderMarkdown } from "../markdown";
import { processPendingMermaidBlocks } from "../mermaid";

/**
 * Renderer for {type:"text"} parts (registered as MessagePrimitive.Parts'
 * `components.Text`). Streamed assistant text is rendered as sanitized
 * markdown - see ../markdown.ts for the security posture (model output is
 * untrusted: marked -> DOMPurify -> dangerouslySetInnerHTML) and the
 * streaming-safety guarantee (fed partial markdown on every delta).
 *
 * A <div> (not the old <p>) because markdown output contains block elements
 * (<p>, <ul>, <pre>) that are invalid nested inside a <p>. The
 * data-testid="assistant-message" hook lives on the message row in
 * Thread.tsx, not here, so the probe's selector is unaffected.
 *
 * MERMAID: renderMarkdown/marked run synchronously and never await anything
 * (see mermaid.ts's header), so a closed ```mermaid fence first paints as a
 * `[data-mermaid-pending]` placeholder that looks like a plain code block.
 * The effect below - the correct place for this side effect, not the render
 * path itself - scans the just-committed DOM for those placeholders after
 * every render, kicks off the actual (cached, deduped) mermaid render, and
 * bumps `renderTick` once any of them settle so this component re-renders
 * and renderMarkdown() picks the now-cached SVG (or the permanent fallback
 * to plain code, on failure) up on its next call. Runs on every commit with
 * no dependency array - cheap and idempotent (mermaid.ts's own cache/
 * in-flight bookkeeping is what actually prevents duplicate work), so it
 * converges once there are no more pending elements for this message.
 */
export function TextPart({ text, status }: TextMessagePartProps) {
  const running = status.type === "running";
  const containerRef = useRef<HTMLDivElement>(null);
  const [, setRenderTick] = useState(0);

  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;
    processPendingMermaidBlocks(container, () => setRenderTick((n) => n + 1));
  });

  return (
    <div
      ref={containerRef}
      className={"cwyc-text cwyc-markdown" + (running ? " cwyc-streaming" : "")}
      dangerouslySetInnerHTML={{ __html: renderMarkdown(text) }}
    />
  );
}
