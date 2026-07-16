import { useMemo } from "react";
import type { ChatStore } from "../store";

/**
 * Sandboxed inline widget (render_widget tool) + the enable-offer chip.
 *
 * SECURITY - read before touching. The widget HTML is agent output, and the
 * agent reads untrusted card content, so treat the payload as HOSTILE even
 * though the user opted in. The confinement, in order of importance:
 *
 * 1. `sandbox="allow-scripts"` WITHOUT `allow-same-origin`: the iframe gets
 *    an opaque origin - window.parent's DOM, the store, and (crucially) the
 *    dock's pycmd bridge are unreachable; SecurityError on any attempt.
 *    Nested iframes inherit the sandbox (flags can only tighten).
 * 2. A <meta> CSP FIRST in <head>: `default-src 'none'` kills every network
 *    fetch (exfiltration), allowing only inline script/style and data: URIs.
 *    Later CSPs (agent-injected metas) can only intersect, never loosen.
 * 3. No postMessage listener anywhere in this app: display-only by design.
 *    If interactivity is ever added, messages must render as VISIBLE user
 *    messages, never as silent commands.
 *
 * Do not "optimize" the srcdoc assembly through any path that touches the
 * live document, and never render `data.html` outside this iframe.
 */

/** Trusted prelude; the agent's HTML goes into <body> verbatim. */
function buildWidgetSrcdoc(html: string, dark: boolean): string {
  const csp =
    "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; " +
    "img-src data:; font-src data:; media-src data:";
  // Minimal base styling so bare widget HTML inherits the dock's feel; the
  // widget can override with its own inline styles.
  const base =
    "html{color-scheme:" + (dark ? "dark" : "light") + ";}" +
    "body{margin:8px;font:13px/1.45 -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;" +
    (dark ? "color:#e8e6e3;background:transparent;" : "color:#1f2937;background:transparent;") +
    "}";
  return (
    "<!doctype html><html><head>" +
    `<meta http-equiv="Content-Security-Policy" content="${csp}">` +
    `<style>${base}</style>` +
    "</head><body>" +
    html +
    "</body></html>"
  );
}

/** Same dark signal styles.css keys on: Anki's night-mode marker (root or
 *  legacy body classes), falling back to the OS scheme when standalone. */
function isDark(): boolean {
  if (typeof document === "undefined") return false;
  const root = document.documentElement;
  if (
    root.classList.contains("night-mode") ||
    document.body.classList.contains("night-mode") ||
    document.body.classList.contains("nightMode")
  ) {
    return true;
  }
  return typeof matchMedia !== "undefined" && matchMedia("(prefers-color-scheme: dark)").matches;
}

export function WidgetCard({ data }: { data: { html: string; title: string } }) {
  const dark = isDark();
  const srcdoc = useMemo(() => buildWidgetSrcdoc(data.html, dark), [data.html, dark]);
  return (
    <figure className="cwyc-widget" data-testid="inline-widget">
      {data.title ? <figcaption className="cwyc-widget-title">{data.title}</figcaption> : null}
      <div className="cwyc-widget-frame-box">
        <iframe
          className="cwyc-widget-frame"
          sandbox="allow-scripts"
          srcDoc={srcdoc}
          title={data.title || "widget"}
        />
      </div>
    </figure>
  );
}

export function WidgetOfferChip({
  data,
  store,
}: {
  data: { id: string; title: string; resolved: boolean };
  store: ChatStore;
}) {
  if (data.resolved) {
    return (
      <div className="cwyc-widget-offer cwyc-widget-offer-resolved" data-testid="widget-offer">
        Widget rendering choice recorded.
      </div>
    );
  }
  return (
    <div className="cwyc-widget-offer" data-testid="widget-offer">
      <div className="cwyc-widget-offer-text">
        The assistant wants to display an interactive widget
        {data.title ? <> (&ldquo;{data.title}&rdquo;)</> : null}. Widgets run in a strict
        sandbox: no internet access, no access to your collection or this app - display only.
      </div>
      <div className="cwyc-widget-offer-actions">
        <button
          type="button"
          className="cwyc-chip cwyc-chip-primary"
          data-testid="widget-offer-enable"
          onClick={() => store.resolveWidgetOffer(data.id, true)}
        >
          Enable widgets
        </button>
        <button
          type="button"
          className="cwyc-chip"
          data-testid="widget-offer-dismiss"
          onClick={() => store.resolveWidgetOffer(data.id, false)}
        >
          Not now
        </button>
      </div>
    </div>
  );
}
