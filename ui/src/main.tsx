/**
 * Production entry point. Built by `npm run build` into
 * chat_with_your_cards/web/next/bundle.js - a classic, self-contained IIFE
 * script (see vite.config.ts) loaded by dock.py via AnkiWebView.stdHtml():
 * plain <script>/<link> tags, no module system, no CDN/network calls at
 * runtime.
 *
 * Bootstrap order: install the window.chatUI global before
 * anything else, kick off the ready handshake, then mount React. Real mode
 * only - window.pycmd is expected to already be wired by AnkiWebView. (Dev
 * mode instead loads dev-main.tsx, which installs a fake window.pycmd first.)
 */
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App";
import { ChatStore } from "./store";
import { installChatUI, startReadyHandshake } from "./bridge";
import "./styles.css";

const store = new ChatStore();

const handle = installChatUI({
  dispatch: store.dispatch,
  focusComposer: () => {
    // Plain textarea, or the CodeMirror content node when vim mode is on.
    document
      .querySelector<HTMLElement>(".cwyc-composer-input, .cwyc-vim-editor .cm-content")
      ?.focus();
  },
});
startReadyHandshake(handle.isAcked);

function mount(): void {
  const container = document.getElementById("cwyc-root") ?? (() => {
    const el = document.createElement("div");
    el.id = "cwyc-root";
    document.body.appendChild(el);
    return el;
  })();

  createRoot(container).render(
    <StrictMode>
      <App store={store} />
    </StrictMode>
  );
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", mount);
} else {
  mount();
}
