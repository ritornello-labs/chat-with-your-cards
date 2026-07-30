import { useEffect } from "react";
import { postCommand } from "../bridge";
import type { ChatStore } from "../store";

/**
 * Document-level keyboard review (task #21).
 *
 * (a) Cmd/Ctrl+Enter accepts the active proposal, Cmd+Backspace rejects it,
 *     Cmd+Up/Down cycles which one is active. All present in app.js and listed
 *     in DESIGN.md as shipped; the React port had no document-level key
 *     handler at all.
 *
 * (b) Escape returns focus to the reviewer. This one is the subtle half: the
 *     handler is CAPTURE-phase on purpose, because AnkiWebView installs its
 *     own bubble-phase Escape that calls pycmd("close"). Bubble-phase here
 *     loses the race and Anki closes instead of focus moving. After the
 *     migration this survived only inside VimComposer, so a DEFAULT (non-vim)
 *     user pressing Escape got Anki's close behaviour.
 *
 * The card, not this hook, performs accept/reject: an edit proposal's values
 * live in its own component state, so a global handler cannot know what
 * "accept" means. It dispatches `cwyc:proposal-action` and the matching card
 * runs the same code path its button does.
 */

export const PROPOSAL_ACTION_EVENT = "cwyc:proposal-action";

export interface ProposalActionDetail {
  id: string;
  action: "accept" | "reject";
}

/** Text entry where the OS/browser already gives these chords a meaning. */
function inTextEntry(target: EventTarget | null): boolean {
  const el = target as HTMLElement | null;
  if (!el || !el.closest) return false;
  return !!el.closest("input, textarea, [contenteditable='true']");
}

/** Editors that own Escape themselves (vim: insert/visual -> normal mode). */
function inVimEditor(target: EventTarget | null): boolean {
  const el = target as HTMLElement | null;
  return !!el?.closest?.(".cwyc-vim-editor, .cwyc-vim-field");
}

/** A popover/dropdown is open and Escape belongs to it, not to Anki. */
function popoverOpen(): boolean {
  return !!document.querySelector(".cwyc-panel, .cwyc-combo-list");
}

export function useKeyboardReview(store: ChatStore): void {
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.defaultPrevented) return;

      if (event.key === "Escape") {
        // Everything below is about who OWNS Escape right now; only when
        // nobody else does should it leave the dock.
        if (inVimEditor(event.target) || popoverOpen()) return;
        event.preventDefault();
        event.stopPropagation(); // beat AnkiWebView's bubble-phase pycmd("close")
        // The set-aside tray is a view the user stepped INTO; Escape steps
        // back out before it means anything to the wider dock (task #33).
        if (store.getSnapshot().ui.pane === "aside") {
          store.closeSetAside();
          return;
        }
        if (store.getSnapshot().isRunning) store.cancel();
        else postCommand({ type: "focus_reviewer" });
        return;
      }

      const chord = event.metaKey || event.ctrlKey;
      if (!chord || event.altKey) return;

      const pending = store.pendingProposalIds();
      if (!pending.length) return;
      const active = store.getSnapshot().ui.activeProposalId;
      const current = active && pending.includes(active) ? active : pending[pending.length - 1];

      const dispatchAction = (action: "accept" | "reject") => {
        event.preventDefault();
        event.stopPropagation();
        store.setActiveProposal(current);
        document.dispatchEvent(
          new CustomEvent<ProposalActionDetail>(PROPOSAL_ACTION_EVENT, {
            detail: { id: current, action },
          })
        );
      };

      if (event.key === "Enter") {
        // Deliberately allowed from inside a field: "finish editing and
        // submit" is what Cmd+Enter means everywhere, and editing a field
        // then accepting is the normal flow.
        dispatchAction("accept");
        return;
      }
      // The rest would fight the OS inside a text field (Cmd+Backspace
      // deletes to line start, Cmd+Up/Down jump to the ends), so they stay
      // out of the way there.
      if (inTextEntry(event.target)) return;

      if (event.key === "Backspace") {
        dispatchAction("reject");
      } else if (event.key === "ArrowUp" || event.key === "ArrowDown") {
        event.preventDefault();
        const step = event.key === "ArrowDown" ? 1 : -1;
        const index = pending.indexOf(current);
        const next = pending[(index + step + pending.length) % pending.length];
        store.setActiveProposal(next);
        document
          .querySelector(`[data-proposal-card-id="${next}"]`)
          ?.scrollIntoView({ block: "nearest" });
      }
    };

    document.addEventListener("keydown", onKeyDown, true); // capture
    return () => document.removeEventListener("keydown", onKeyDown, true);
  }, [store]);
}
