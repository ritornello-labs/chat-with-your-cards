#!/usr/bin/env node
/**
 * Codegen: produce a self-contained KaTeX stylesheet with fonts inlined as
 * base64 data URIs, instead of importing katex/dist/katex.min.css directly.
 *
 * WHY: this add-on's UI bundle is loaded by the Python side via
 * AnkiWebView.stdHtml(css=[...], js=[...]), which registers routes through
 * `mw.addonManager.setWebExports(__name__, r"web/.*\.(css|js)")`
 * (chat_with_your_cards/__init__.py). That regex only serves .css and .js -
 * any separate font file Vite would normally emit under web/next/assets/
 * (katex.min.css's stock @font-face rules point at relative fonts/*.woff2
 * files) is NOT covered by that route and would 404 in the dock's webview.
 * A math renderer with visibly broken glyphs is worse than no renderer.
 *
 * FIX: inline every font as a data: URI directly in the CSS text, at
 * generation time, from the actual bytes in node_modules/katex/dist/fonts/.
 * The output file has zero relative url() references, so Vite's normal CSS
 * asset pipeline (assetsInlineLimit etc., which we deliberately do not touch
 * here - vite.config.ts is out of scope for this change) never has anything
 * to resolve: the generated CSS is emitted byte-for-byte into bundle.css.
 *
 * Only the woff2 variant is embedded (dropping the woff/ttf fallbacks KaTeX
 * ships for older browsers): the dock only ever runs inside Qt's
 * QtWebEngine (a current Chromium), which has supported woff2 for years, so
 * the fallback formats would be pure dead weight in every chat session.
 *
 * Run via `npm run build` (wired as a "prebuild" step in package.json) or
 * directly: `node src/build/generate-katex-inline-css.mjs`. Output is
 * committed (src/katex-inline.css) so a plain `vite build` also works
 * without this step, and diffs are reviewable; re-run after bumping the
 * pinned `katex` dependency version.
 */
import { readFileSync, writeFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";

const here = path.dirname(fileURLToPath(import.meta.url));
const uiRoot = path.resolve(here, "..");
const katexDist = path.join(uiRoot, "node_modules", "katex", "dist");
const outFile = path.join(uiRoot, "src", "katex-inline.css");

const srcCss = readFileSync(path.join(katexDist, "katex.min.css"), "utf8");

// Matches one @font-face src list, e.g.:
//   src:url(fonts/KaTeX_AMS-Regular.woff2) format("woff2"),url(fonts/KaTeX_AMS-Regular.woff) format("woff"),url(fonts/KaTeX_AMS-Regular.ttf) format("truetype")
// Captures the base font file name (without extension) from the woff2 entry.
const SRC_LIST_RE =
  /src:url\(fonts\/([^)]+)\.woff2\)\s*format\("woff2"\)(?:,url\(fonts\/[^)]+\.woff\)\s*format\("woff"\))?(?:,url\(fonts\/[^)]+\.ttf\)\s*format\("truetype"\))?/g;

let fontsInlined = 0;
let totalBase64Bytes = 0;

const outCss = srcCss.replace(SRC_LIST_RE, (whole, baseName) => {
  const fontPath = path.join(katexDist, "fonts", `${baseName}.woff2`);
  const bytes = readFileSync(fontPath);
  const base64 = bytes.toString("base64");
  fontsInlined += 1;
  totalBase64Bytes += base64.length;
  return `src:url(data:font/woff2;base64,${base64}) format("woff2")`;
});

// Sanity check: every @font-face in the source must have been rewritten -
// if KaTeX changes its CSS structure in a future version bump, fail loudly
// here instead of silently shipping un-inlined (404-prone) font urls.
const remainingFontUrls = [...outCss.matchAll(/url\(fonts\//g)];
if (remainingFontUrls.length > 0) {
  console.error(
    `generate-katex-inline-css: ${remainingFontUrls.length} font url(...) reference(s) ` +
      "were not inlined - katex's CSS structure may have changed. Aborting " +
      "so a broken (404-prone) stylesheet is never committed.",
  );
  process.exit(1);
}
const fontFaceCount = (srcCss.match(/@font-face/g) || []).length;
if (fontsInlined !== fontFaceCount) {
  console.error(
    `generate-katex-inline-css: expected to inline ${fontFaceCount} @font-face rules, ` +
      `only inlined ${fontsInlined}. Aborting.`,
  );
  process.exit(1);
}

const header = `/* GENERATED FILE - do not hand-edit.
 * Produced by src/build/generate-katex-inline-css.mjs from
 * node_modules/katex/dist/katex.min.css + .../fonts/*.woff2 (katex ${
   JSON.parse(readFileSync(path.join(katexDist, "..", "package.json"), "utf8")).version
 }).
 * All @font-face src urls are base64 data: URIs (woff2 only) - see that
 * script's header comment for why. Re-run \`npm run build\` after bumping
 * the katex dependency to regenerate.
 */
`;

writeFileSync(outFile, header + outCss);

const kb = (n) => (n / 1024).toFixed(1);
console.log(
  `generate-katex-inline-css: inlined ${fontsInlined} fonts ` +
    `(${kb(totalBase64Bytes)} KiB base64) into ${path.relative(uiRoot, outFile)}`,
);
