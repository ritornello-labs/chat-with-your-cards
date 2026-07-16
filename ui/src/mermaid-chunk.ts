/**
 * Entry for the separately-built mermaid chunk (vite.mermaid.config.ts ->
 * chat_with_your_cards/web/next/mermaid.bundle.js, an ES module fetched at
 * runtime by mermaid.ts's loadMermaid() the first time a closed ```mermaid
 * fence needs rendering).
 *
 * Why this exists: the main bundle is a single IIFE (vite.config.ts) and
 * Rollup cannot code-split IIFE output, so a plain `import("mermaid")` in
 * mermaid.ts would inline mermaid's ~3.6MB into bundle.js - every dock load
 * would pay its parse cost, and every routine bundle rebuild would commit a
 * 4.6MB blob. This file changes ONLY when the mermaid dependency itself is
 * upgraded, so the committed mermaid.bundle.js stays byte-stable across
 * normal UI work.
 */
export { default } from "mermaid";
