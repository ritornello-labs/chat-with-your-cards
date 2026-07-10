import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Production build target: chat_with_your_cards/web/next/{bundle.js,bundle.css}.
//
// Emitted as a single self-contained IIFE (classic <script> tag, no
// type="module", no code-splitting, no CDN/network fetches at runtime) so a
// future Python loader can point AnkiWebView.stdHtml() at this directory the
// same way dock.py does today for web/ (index.html body + css=[...] +
// js=[...]). React/ReactDOM/@assistant-ui are bundled in, not externalized.
export default defineConfig({
  plugins: [react()],
  base: "./",
  // Vite's usual automatic `process.env.NODE_ENV` replacement does not
  // reach library-mode/IIFE builds the way it does the default app build:
  // without this, React's own bundled dev-mode branches
  // (process.env.NODE_ENV !== "production") throw "process is not defined"
  // at runtime in a plain browser (there is no Node `process` global to
  // fall back on). Confirmed empirically against the built bundle.
  define: {
    "process.env.NODE_ENV": JSON.stringify("production"),
  },
  build: {
    outDir: "../chat_with_your_cards/web/next",
    emptyOutDir: true,
    cssCodeSplit: false,
    sourcemap: false,
    lib: {
      entry: "src/main.tsx",
      name: "CwycUI",
      formats: ["iife"],
      fileName: () => "bundle.js",
    },
    rollupOptions: {
      output: {
        assetFileNames: (info) =>
          info.name && info.name.endsWith(".css") ? "bundle.css" : "assets/[name][extname]",
      },
    },
  },
});
