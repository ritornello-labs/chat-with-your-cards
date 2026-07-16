import { defineConfig } from "vite";

// Second build pass (see package.json "build"): emits mermaid as a
// standalone ES module chunk next to the main IIFE bundle -
// chat_with_your_cards/web/next/mermaid.bundle.js - fetched at runtime via
// dynamic import() by ui/src/mermaid.ts. Rationale in src/mermaid-chunk.ts.
//
// MUST run AFTER the main `vite build`: that pass has emptyOutDir: true and
// would wipe this file; this pass sets emptyOutDir: false to compose.
//
// ES format is required (the runtime loads it with import(), which the
// media-served /_addons/... URL supports in QtWebEngine/Chromium); the main
// bundle stays a classic-script IIFE, and a classic script may still call
// import() - only `import.meta` is off-limits there, which is why
// mermaid.ts resolves this file's URL from document.currentScript instead.
export default defineConfig({
  base: "./",
  define: {
    "process.env.NODE_ENV": JSON.stringify("production"),
  },
  build: {
    outDir: "../chat_with_your_cards/web/next",
    emptyOutDir: false,
    sourcemap: false,
    lib: {
      entry: "src/mermaid-chunk.ts",
      formats: ["es"],
      fileName: () => "mermaid.bundle.js",
    },
    rollupOptions: {
      output: {
        // mermaid lazily imports each diagram type; without this the ES
        // build splinters into ~50 hash-named sibling chunks (committed-file
        // churn on every mermaid upgrade). One flat file instead.
        inlineDynamicImports: true,
      },
    },
  },
});
