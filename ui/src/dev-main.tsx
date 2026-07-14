/**
 * `npm run dev` entry point: the full Thread UI running in a plain browser
 * against the scripted replayer, no Anki and no Python involved. This is
 * where design iteration happens (see ui/README.md).
 *
 * Installs the fake window.pycmd BEFORE importing/running the real
 * bridge.ts code path, then reuses main.tsx's exact bootstrap (installChatUI
 * + startReadyHandshake + mount) so dev and prod exercise identical wiring.
 */
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App";
import { ChatStore } from "./store";
import { installChatUI, startReadyHandshake } from "./bridge";
import { installDevReplayer } from "./dev/replayer";
import "./styles.css";
import "./dev/dev-harness.css";

installDevReplayer();

// Standalone marker: scopes styles.css's prefers-color-scheme block so the
// OS dark theme only applies OUTSIDE Anki (inside Anki, night-mode classes
// are the sole authority - see styles.css header).
document.documentElement.classList.add("cwyc-standalone");

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

function toggleNightMode(): void {
  document.body.classList.toggle("night-mode");
}

function mount(): void {
  const toolbar = document.getElementById("dev-toolbar");
  toolbar?.querySelector("#dev-toggle-night")?.addEventListener("click", toggleNightMode);

  const container = document.getElementById("cwyc-root");
  if (!container) throw new Error("dev-main.tsx: #cwyc-root not found in ui/index.html");

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
