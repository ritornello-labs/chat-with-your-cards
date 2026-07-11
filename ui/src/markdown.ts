/**
 * Markdown rendering for assistant text parts.
 *
 * SECURITY: the strings passed here are model output, and the model reads
 * untrusted card/collection content (a shared deck can embed text aimed at
 * the assistant). Treat every rendered string as untrusted HTML: marked
 * turns markdown into HTML, then DOMPurify strips anything script-like
 * (event handlers, <script>, javascript: URLs, ...) before it reaches the
 * DOM via dangerouslySetInnerHTML. This is the classic UI's marked.js
 * posture (app.js's renderMarkdown) plus the sanitize step it lacked.
 *
 * STREAMING SAFETY: renderMarkdown runs on every text_delta, so it is fed
 * partial/incomplete markdown mid-stream (an unclosed ``` fence, a dangling
 * `[link`, half a table). marked tolerates partial input by design, but a
 * try/catch guards anyway - on any failure the raw text is escaped and shown
 * verbatim rather than throwing and blanking the turn. Nothing here mutates
 * layout-breaking state, so a mid-stream render is always safe.
 */
import { marked } from "marked";
import DOMPurify from "dompurify";

// gfm + breaks mirrors the chat convention (single newline -> <br>), matching
// how Claude Code / ChatGPT render streamed assistant text. Synchronous
// (async stays off) so parse() returns a string, never a Promise.
marked.setOptions({ gfm: true, breaks: true });

// Force links to open outside the dock webview and never leak referrer/opener
// (defense-in-depth on top of DOMPurify): a link in rendered agent output
// should not be able to navigate the chat away or reach window.opener.
DOMPurify.addHook("afterSanitizeAttributes", (node) => {
  if (node.tagName === "A") {
    node.setAttribute("target", "_blank");
    node.setAttribute("rel", "noopener noreferrer");
  }
});

function escapeHtml(text: string): string {
  return text
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

/**
 * Parse `text` as markdown and return sanitized HTML safe for
 * dangerouslySetInnerHTML. On any parser error, falls back to the escaped
 * raw text so a malformed/partial stream can never throw or inject markup.
 */
export function renderMarkdown(text: string): string {
  try {
    const html = marked.parse(text, { async: false }) as string;
    return DOMPurify.sanitize(html);
  } catch {
    return escapeHtml(text);
  }
}
