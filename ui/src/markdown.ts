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
 * DOMPurify is called here with NO custom ALLOWED_TAGS/ALLOWED_ATTR/ADD_TAGS
 * - the default profile - and that is deliberate, not an oversight: both the
 * KaTeX math extension (katex-math.ts) and the mermaid diagram renderer
 * (mermaid.ts) were designed so their output needs no allowlist changes at
 * all. See katex-math.ts's header for exactly which tags KaTeX emits and
 * why DOMPurify's default handling of them (in particular, quietly
 * dropping the MathML accessibility subtree) is the right outcome here, not
 * a gap to patch over.
 *
 * STREAMING SAFETY: renderMarkdown runs on every text_delta, so it is fed
 * partial/incomplete markdown mid-stream (an unclosed ``` fence, a dangling
 * `[link`, half a table, an unclosed `$`/`$$`/```mermaid). marked tolerates
 * partial input by design, but a try/catch guards anyway - on any failure
 * the raw text is escaped and shown verbatim rather than throwing and
 * blanking the turn. Nothing here mutates layout-breaking state, so a
 * mid-stream render is always safe. katex-math.ts and mermaid.ts each carry
 * their own streaming-safety notes for their specific delimiters/fences.
 */
import { marked } from "marked";
import type { Tokens } from "marked";
import DOMPurify from "dompurify";
import { katexMarkedExtension } from "./katex-math";
import { renderMermaidCode } from "./mermaid";
import "./katex-inline.css";
import "./markdown-extras.css";

// gfm + breaks mirrors the chat convention (single newline -> <br>), matching
// how Claude Code / ChatGPT render streamed assistant text. Synchronous
// (async stays off) so parse() returns a string, never a Promise.
marked.setOptions({ gfm: true, breaks: true });

// $/$$ math (katex-math.ts) - registered before the mermaid `code` renderer
// override below since marked.use() merges are independent (math is inline
// tokenizer/renderer extensions, mermaid is a block-level renderer
// override; order between the two calls does not matter).
marked.use(katexMarkedExtension);

// ```mermaid fences: everything else falls through to marked's default code
// renderer (returning `false` - see mermaid.ts's renderMermaidCode header).
marked.use({
  renderer: {
    code(token: Tokens.Code) {
      return renderMermaidCode(token);
    },
  },
});

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
