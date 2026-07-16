/**
 * Mermaid diagram rendering for ```mermaid fences in assistant markdown.
 *
 * LAZY LOADING - CODE IS LAZY, THE CURRENT BUNDLE IS NOT (read before
 * touching vite.config.ts): `import("mermaid")` below is a genuine dynamic
 * import - mermaid's own module-level init/side effects do not run until
 * loadMermaid() is first called, i.e. until a message actually contains a
 * closed ```mermaid fence (see isFenceClosed()/renderMermaidCode() below).
 *
 * BUT under the current vite.config.ts, that dynamic import does NOT become
 * a separately-fetched network chunk the way it would in a normal (ESM,
 * code-splitting) Vite app build. vite.config.ts builds this UI as a single
 * self-contained IIFE by deliberate design (its own header comment: "no
 * code-splitting, no CDN/network fetches at runtime", one fixed bundle.js
 * path that dock.py points AnkiWebView.stdHtml() at). Rollup cannot emit a
 * second, separately-loadable chunk for an IIFE/classic-script output - it
 * has no runtime module loader to fetch one - so `import("mermaid")` here
 * gets statically inlined into bundle.js at build time, same as a regular
 * import would. Measured: bundle.js grew from 952,527 bytes (committed
 * baseline, `git show HEAD:chat_with_your_cards/web/next/bundle.js | wc -c`)
 * to ~4.59 MB after adding this file, almost entirely mermaid + its layout
 * engine (d3/dagre-ish internals) - i.e. every dock load now pays that
 * download/parse/compile cost whether or not the conversation ever uses a
 * mermaid fence.
 *
 * This was NOT fixed here because doing so for real requires either (a)
 * editing vite.config.ts to change the build's module format/loading
 * convention - out of this change's file-ownership scope, and a decision
 * that affects dock.py's loading contract, not something to make
 * unilaterally - or (b) standing up a second, separately-built ES module
 * chunk plus a runtime-resolved absolute-URL `import()` (e.g. via
 * `document.currentScript.src`, since a classic script has no
 * `import.meta.url`) - a real architecture addition whose runtime behavior
 * (base-URL resolution, the addon's web-export route serving an
 * unregistered-but-regex-matched sibling file) could not be verified against
 * an actual Anki/QtWebEngine session from here. Flagged for the
 * orchestrator/vite.config.ts owner as a follow-up rather than guessed at.
 * The feature itself is fully correct and safe either way - this only
 * affects bundle size/parse cost, not correctness or security.
 *
 * STREAMING SAFETY: renderMarkdown (markdown.ts) runs on every streamed text
 * delta, so a ```mermaid fence is fed to marked in a partial state on every
 * delta until its closing ``` arrives. marked itself implicitly closes an
 * unterminated fence at end-of-input (CommonMark behavior - verified
 * empirically against marked 18.0.5), so `token.raw`/`token.text` for a
 * still-open fence look exactly like a normal, closed code block except
 * that `token.raw` has no trailing closing-fence line. isFenceClosed()
 * below is exactly that check: mermaid is only ever asked to parse a fence
 * once its raw text ends in a real closing ``` line. Until then,
 * renderMermaidCode returns `false` so marked's default <pre><code>
 * renderer handles it - a still-streaming ```mermaid block reads as a
 * completely ordinary code block, never a half-rendered diagram.
 *
 * ASYNC INSIDE A SYNC PIPELINE: mermaid.render() is async but
 * renderMarkdown()/marked.parse() run synchronously (see markdown.ts). The
 * `code` renderer here therefore never awaits anything - it only ever reads
 * the module-level `cache` (populated) or leaves a `[data-mermaid-pending]`
 * placeholder (not yet attempted) which mirrors the plain fallback code
 * block visually. All actual async work is kicked off from
 * TextPart.tsx's post-commit effect via processPendingMermaidBlocks, which
 * is the React-sanctioned place for a side effect - never from inside the
 * render path itself (doing it there would kick off a `mermaid.render()`
 * call as a side effect of merely calling renderMarkdown, misbehaving under
 * StrictMode's double-invoked renders and generally violating "render must
 * be pure"). Once a render settles (success or failure), the effect forces
 * one extra re-render of the owning TextPart so the next renderMarkdown()
 * pass picks the cached result up.
 *
 * FAILURE HANDLING: a bad/unsupported diagram is cached as `{status:
 * "error"}` (once, permanently, for that content) so renderMermaidCode
 * falls back to the plain fenced code block forever after - it must never
 * throw or blank the message (see task requirements), and re-attempting a
 * known-bad diagram on every keystroke of unrelated later text would be
 * wasted work.
 */
import DOMPurify from "dompurify";
import type { Tokens } from "marked";

export type MermaidCacheEntry = { status: "ok"; svg: string } | { status: "error" };

const cache = new Map<string, MermaidCacheEntry>();
const inFlight = new Map<string, Promise<MermaidCacheEntry>>();

let mermaidLoad: Promise<typeof import("mermaid")> | null = null;
let configured = false;
let renderSeq = 0;

function loadMermaid(): Promise<typeof import("mermaid")> {
  if (!mermaidLoad) mermaidLoad = import("mermaid");
  return mermaidLoad;
}

/** Pure cache lookup - never triggers a render. Safe to call from the
 * synchronous marked renderer (renderMermaidCode below). */
export function getCachedMermaid(source: string): MermaidCacheEntry | undefined {
  return cache.get(source);
}

/**
 * Render `source` (a closed ```mermaid fence's raw text) to a sanitized SVG
 * string and cache it, keyed by the exact source text (this doubles as the
 * "only re-render when the content actually changed" cache the task asks
 * for: identical diagram text - anywhere, in any message - renders through
 * mermaid at most once per session). Concurrent calls for the same source
 * share one in-flight promise instead of racing duplicate renders. Never
 * rejects: failures resolve to `{status:"error"}`.
 */
export function renderMermaidAndCache(source: string): Promise<MermaidCacheEntry> {
  const cached = cache.get(source);
  if (cached) return Promise.resolve(cached);
  const existing = inFlight.get(source);
  if (existing) return existing;

  const task = (async (): Promise<MermaidCacheEntry> => {
    try {
      const { default: mermaid } = await loadMermaid();
      if (!configured) {
        mermaid.initialize({
          startOnLoad: false,
          // Untrusted diagram source (model output reading untrusted card
          // content, same threat model as everywhere else in this file):
          // strict mode disables script-ish constructs (click handlers)
          // mermaid would otherwise allow.
          securityLevel: "strict",
          theme: "neutral",
          // Strict mode does NOT switch labels to SVG text in mermaid 11 -
          // it still emits HTML labels inside <foreignObject>, which the
          // DOMPurify SVG-profile pass below rightly strips, leaving
          // diagrams with empty nodes (found in preview 2026-07-16). Force
          // plain <text> labels so the sanitized SVG keeps its words.
          htmlLabels: false,
          flowchart: { htmlLabels: false },
        });
        configured = true;
      }
      const id = `cwyc-mermaid-${++renderSeq}`;
      // mermaid.render() needs a real, laid-out (connected, not display:none)
      // element to measure text/bounding boxes against - verified empirically
      // that omitting the container param lets mermaid create and attach its
      // OWN scratch element directly under document.body, and on a parse
      // failure that scratch element is left behind uncleaned (mermaid's
      // internal cleanup runs after the point where it throws), landing a
      // second, unsanitized copy of its "Syntax error in text" SVG straight
      // in the live DOM outside this file's control entirely - bypassing the
      // DOMPurify pass below. Supplying our own off-screen container and
      // always removing it in `finally`, regardless of success or throw,
      // closes that leak: the only SVG that ever reaches the page is the
      // sanitized `svg` string returned below, inserted by renderMermaidCode.
      const scratch = document.createElement("div");
      scratch.setAttribute("aria-hidden", "true");
      scratch.style.position = "absolute";
      scratch.style.left = "-9999px";
      scratch.style.top = "0";
      scratch.style.width = "0";
      scratch.style.height = "0";
      scratch.style.overflow = "hidden";
      document.body.appendChild(scratch);
      let svg: string;
      try {
        ({ svg } = await mermaid.render(id, source, scratch));
      } finally {
        scratch.remove();
      }
      // Defense in depth: securityLevel "strict" already constrains what
      // mermaid itself will emit, but the diagram text is untrusted input
      // to mermaid's parser/renderer, so re-sanitize the SVG it hands back
      // through DOMPurify's SVG profile before it ever reaches the DOM -
      // the same "don't trust one layer alone" posture as marked ->
      // DOMPurify for the surrounding markdown. svgFilters stays off:
      // diagrams don't need <feGaussianBlur> etc., no reason to allow it.
      const safeSvg = DOMPurify.sanitize(svg, {
        USE_PROFILES: { svg: true, svgFilters: false },
      });
      const entry: MermaidCacheEntry = { status: "ok", svg: safeSvg };
      cache.set(source, entry);
      return entry;
    } catch {
      const entry: MermaidCacheEntry = { status: "error" };
      cache.set(source, entry);
      return entry;
    } finally {
      inFlight.delete(source);
    }
  })();

  inFlight.set(source, task);
  return task;
}

/**
 * True if `raw` (marked's Tokens.Code.raw for a fenced code block) ends in
 * an actual closing fence line, as opposed to marked's CommonMark-mandated
 * implicit close at end-of-input for an unterminated fence - which is
 * exactly what a still-streaming ```mermaid block looks like until its
 * closing ``` arrives. See this file's header for why that distinction
 * matters.
 */
export function isFenceClosed(raw: string): boolean {
  const lines = raw.split("\n");
  if (lines.length < 2) return false; // just the opening fence line so far
  return /^\s*`{3,}\s*$/.test(lines[lines.length - 1]);
}

// UTF-8-safe base64 (btoa/atob are Latin1-only): mermaid source can contain
// non-Latin1 text (e.g. non-English diagram labels), so plain btoa would
// throw on it. Used only to round-trip the fence's raw text through a DOM
// data-attribute between the sync render pass and the async effect.
function toBase64Utf8(text: string): string {
  return btoa(String.fromCharCode(...new TextEncoder().encode(text)));
}

function fromBase64Utf8(b64: string): string {
  return new TextDecoder().decode(Uint8Array.from(atob(b64), (c) => c.charCodeAt(0)));
}

function escapeHtml(text: string): string {
  return text
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

const MERMAID_LANG = "mermaid";
const WRAPPER_CLASS = "cwyc-mermaid";
const PENDING_ATTR = "data-mermaid-pending";
const SRC_ATTR = "data-mermaid-src";

/**
 * marked `code` renderer override (registered in markdown.ts). Returning
 * `false` falls back to marked's own default <pre><code> renderer verbatim
 * (marked.use() semantics: a renderer override returning `false` defers to
 * the built-in one) - used for every non-mermaid fence, and deliberately
 * also for a mermaid fence that is not closed yet or has already been
 * marked as unrenderable, so those cases read as a completely ordinary code
 * block with no special chrome.
 */
export function renderMermaidCode(token: Tokens.Code): string | false {
  if ((token.lang ?? "").trim().split(/\s+/)[0]?.toLowerCase() !== MERMAID_LANG) return false;
  if (!isFenceClosed(token.raw)) return false;

  const cached = getCachedMermaid(token.text);
  if (cached?.status === "ok") {
    return `<div class="${WRAPPER_CLASS}" data-mermaid-rendered="true">${cached.svg}</div>`;
  }
  if (cached?.status === "error") return false; // permanently fall back to the plain code block

  // Not yet attempted: emit the same markup the default code renderer
  // would (a plain escaped code block), wrapped with tracking data so
  // TextPart's effect can find it, kick off the async render, and trigger
  // a re-render once it settles (see this file's header).
  const escaped = escapeHtml(token.text);
  return (
    `<div class="${WRAPPER_CLASS}" ${PENDING_ATTR}="true" ${SRC_ATTR}="${toBase64Utf8(token.text)}">` +
    `<pre><code class="language-mermaid">${escaped}</code></pre></div>`
  );
}

/**
 * Called from TextPart's post-commit effect. Scans `root` for mermaid
 * placeholders left by renderMermaidCode, kicks off (deduped, cached)
 * rendering for each, and calls `onSettled` once any of them resolve so the
 * caller can force one more render pass - which will pick the result up
 * from the cache via renderMermaidCode on the next renderMarkdown() call.
 * Safe to call on every commit: entries already cached or in flight are
 * skipped, so this converges (no further pending elements) after the first
 * successful pass over a given diagram's content.
 */
export function processPendingMermaidBlocks(root: ParentNode, onSettled: () => void): void {
  const pending = root.querySelectorAll<HTMLElement>(`[${PENDING_ATTR}="true"]`);
  pending.forEach((el) => {
    const b64 = el.getAttribute(SRC_ATTR);
    if (!b64) return;
    let source: string;
    try {
      source = fromBase64Utf8(b64);
    } catch {
      return; // malformed attribute - leave the plain fallback showing
    }
    if (getCachedMermaid(source)) return; // resolved since this DOM snapshot was taken
    void renderMermaidAndCache(source).then(onSettled);
  });
}
