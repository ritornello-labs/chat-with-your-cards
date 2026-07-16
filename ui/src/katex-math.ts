/**
 * Inline/display LaTeX math for assistant markdown ($...$ / $$...$$), hand-
 * rolled as a marked v18 tokenizer+renderer extension rather than pulling in
 * marked-katex-extension (see markdown.ts's header for the overall
 * marked -> DOMPurify security posture this plugs into).
 *
 * WHY HAND-ROLLED INSTEAD OF marked-katex-extension: that package advertises
 * `marked: ">=4 <19"` / `katex: ">=0.16 <0.18"` (checked via `npm view
 * marked-katex-extension peerDependencies` on 2026-07-16), which is
 * nominally compatible with the versions pinned here (marked 18.0.5, katex
 * 0.17.0). But its bundled delimiter heuristics aren't documented precisely
 * enough to be certain it rejects "R$ 50" / "$5 and $10" the way this add-on
 * needs (model output routinely quotes prices). A ~40-line extension with an
 * explicit, testable heuristic is safer and one fewer third-party dependency
 * sitting in an untrusted-input pipeline.
 *
 * DELIMITER RULES (mirrors the standard KaTeX auto-render heuristic):
 *  - display: $$...$$; requires a real closing "$$" later in the current
 *    text. On no closing "$$" yet (mid-stream), the tokenizer returns
 *    undefined and marked's built-in tokenizers render the "$$" as literal
 *    text meanwhile - see markdown.ts's STREAMING SAFETY note.
 *  - inline: $...$; the character right after the opening $ and right
 *    before the closing $ must both be non-whitespace, and the content may
 *    not itself contain another literal $ or a newline. This is exactly what
 *    rejects "R$ 50" (space right after that $) and "$5 and $10" (space
 *    right before the second $, so it never pairs with the first) while
 *    still matching "$x^2$".
 *
 * SECURITY: katex.renderToString runs with throwOnError:false (per KaTeX's
 * docs this makes it render invalid LaTeX as styled error text instead of
 * throwing - types/katex.d.ts) and the default trust:false, which is never
 * overridden here: trust:false is KaTeX's own gate on \includegraphics,
 * \href, \htmlClass and similar commands that could otherwise turn
 * attacker-controlled LaTeX into arbitrary links/images/HTML classes. The
 * LaTeX source is untrusted model output, so this must stay false.
 *
 * KaTeX's default output ("htmlAndMathml") emits two parallel trees: a
 * visual one (.katex-html, plain <span>/<svg> with class/style attributes)
 * and an accessibility one (.katex-mathml, a <math><semantics>
 * <annotation>...) for screen readers/copy-paste. DOMPurify's *default*
 * allowlist already covers every tag/attribute the visual tree uses (span,
 * svg, path, class, style - all in its default HTML+SVG sets), so nothing
 * needs to be added there. The accessibility tree is a different story:
 * DOMPurify deliberately EXCLUDES `math`, `semantics`, `annotation`, and
 * `annotation-xml` from its default allowlist (see
 * dompurify/dist/purify.cjs.js's `mathMlDisallowed` array, and `math` is
 * also in `DEFAULT_FORBID_CONTENTS` alongside mi/mn/mo/ms/mtext) - these
 * were historically a mutation-XSS vector via MathML/foreign-content
 * namespace confusion, and DOMPurify's authors chose not to allow them by
 * default. This module does NOT add them back: the visible rendering does
 * not depend on that subtree (KaTeX's own CSS visually hides
 * .katex-mathml), so DOMPurify quietly emptying it costs only
 * screen-reader/copy-paste fidelity for math, which this chat dock does not
 * currently target, in exchange for not reopening a class of bug DOMPurify
 * closed on purpose. See markdown.ts for the (unmodified) DOMPurify call.
 */
import katex from "katex";
import type { MarkedExtension, TokenizerAndRendererExtension, Tokens } from "marked";

function escapeHtml(text: string): string {
  return text
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function renderKatex(source: string, displayMode: boolean): string {
  try {
    return katex.renderToString(source, {
      throwOnError: false,
      displayMode,
      // "ignore" silences console.warn spam for merely-non-standard-but-
      // supported LaTeX (KaTeX's default "warn" strict mode) - irrelevant
      // noise for a chat log, not a safety control.
      strict: "ignore",
      output: "htmlAndMathml",
      // trust intentionally left at its default (false) - see header.
    });
  } catch {
    // Defensive only: throwOnError:false means katex itself should not
    // throw for parse errors (it renders a styled error span instead - see
    // types/katex.d.ts), but a math renderer must never blank/throw the
    // whole message either way, so fall back to escaped raw source.
    return `<span class="cwyc-math-error">${escapeHtml(source)}</span>`;
  }
}

interface MathToken extends Tokens.Generic {
  type: "cwycMathDisplay" | "cwycMathInline";
  raw: string;
  text: string;
}

const displayExtension: TokenizerAndRendererExtension = {
  name: "cwycMathDisplay",
  level: "inline",
  start(src) {
    const i = src.indexOf("$$");
    return i === -1 ? undefined : i;
  },
  tokenizer(src): MathToken | undefined {
    if (src[0] !== "$" || src[1] !== "$") return undefined;
    const close = src.indexOf("$$", 2);
    if (close === -1) return undefined; // no closing "$$" yet: mid-stream, leave as text
    const inner = src.slice(2, close).trim();
    if (inner.length === 0) return undefined;
    return { type: "cwycMathDisplay", raw: src.slice(0, close + 2), text: inner };
  },
  renderer(token) {
    return renderKatex((token as MathToken).text, true);
  },
};

const inlineExtension: TokenizerAndRendererExtension = {
  name: "cwycMathInline",
  level: "inline",
  start(src) {
    const i = src.indexOf("$");
    return i === -1 ? undefined : i;
  },
  tokenizer(src): MathToken | undefined {
    if (src[0] !== "$" || src[1] === "$") return undefined; // "$$" belongs to display
    let end = 1;
    while (end < src.length && src[end] !== "$" && src[end] !== "\n") end++;
    if (end >= src.length || src[end] !== "$") return undefined; // no closing $: mid-stream or multi-line
    const inner = src.slice(1, end);
    if (inner.length === 0) return undefined;
    // Currency-safe heuristic (see header): reject "R$ 50" / "$5 and $10".
    if (/^\s/.test(inner) || /\s$/.test(inner)) return undefined;
    return { type: "cwycMathInline", raw: src.slice(0, end + 1), text: inner };
  },
  renderer(token) {
    return renderKatex((token as MathToken).text, false);
  },
};

/** Registered once via `marked.use(katexMarkedExtension)` in markdown.ts. */
export const katexMarkedExtension: MarkedExtension = {
  extensions: [displayExtension, inlineExtension],
};
