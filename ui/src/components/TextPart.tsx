import type { TextMessagePartProps } from "@assistant-ui/react";
import { renderMarkdown } from "../markdown";

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
 */
export function TextPart({ text, status }: TextMessagePartProps) {
  const running = status.type === "running";
  return (
    <div
      className={"cwyc-text cwyc-markdown" + (running ? " cwyc-streaming" : "")}
      dangerouslySetInnerHTML={{ __html: renderMarkdown(text) }}
    />
  );
}
