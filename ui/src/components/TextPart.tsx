import type { TextMessagePartProps } from "@assistant-ui/react";

/**
 * Plain-text renderer for {type:"text"} parts (registered as
 * MessagePrimitive.Parts' `components.Text`). Markdown rendering (marked.js
 * in app.js) is out of scope for this scaffold - see ui/README.md.
 */
export function TextPart({ text, status }: TextMessagePartProps) {
  const running = status.type === "running";
  return (
    <p className={"cwyc-text" + (running ? " cwyc-streaming" : "")}>{text}</p>
  );
}
