import { useEffect, useRef } from "react";
import { useChatState } from "../ChatRuntimeProvider";
import type { ChatStore } from "../store";

/**
 * The reviewing-side affordances for deferral (task #32 follow-up, user
 * 2026-07-28): a "Set aside" button while a review card is on screen, and a
 * transient Undo chip whenever a card was just deferred - by this button, the
 * chord, the menu, the agent, or defer-on-send.
 *
 * The use case the button serves: you ask the agent about the card in front
 * of you, and while it thinks you want to keep reviewing OTHER cards - so the
 * one under discussion steps aside and comes back later in the session. With
 * `defer_on_send` enabled the button's job happens automatically on every
 * send; the button then reads as the mode indicator.
 */

/** How long the Undo chip lingers. Long enough to notice and regret, short
 *  enough not to become furniture. */
const UNDO_VISIBLE_MS = 12_000;

export function SetAsideChip({ store }: { store: ChatStore }) {
  const { ui } = useChatState(store);
  const settings = ui.settings;
  // Only when it can do something: a review card on screen, and not hidden
  // in settings. When defer-on-send is on the chip stays visible as the
  // indicator that sends will defer.
  if (!settings?.deferButton || !ui.reviewing) return null;
  const auto = settings.deferOnSend;
  return (
    <button
      type="button"
      className={"cwyc-chip cwyc-chip-pin" + (auto ? " cwyc-chip-on" : "")}
      data-testid="defer-chip"
      title={
        auto
          ? "Auto: sending a message sets the current card aside (Settings › Reviewing)"
          : `Set the current card aside - it comes back later in this session (${settings.deferShortcut})`
      }
      onClick={() => store.deferCurrentCard()}
    >
      <svg viewBox="0 0 14 14" width="11" height="11" aria-hidden="true">
        {/* a card stepping back: rectangle + backward arrow */}
        <path
          d="M2 3.5h8v7H2z M11 5l2 2-2 2"
          fill="none"
          stroke="currentColor"
          strokeWidth="1.4"
          strokeLinecap="round"
          strokeLinejoin="round"
        />
      </svg>
      {auto ? "Set aside · auto" : "Set aside"}
    </button>
  );
}

export function DeferredUndoChip({ store }: { store: ChatStore }) {
  const { ui } = useChatState(store);
  const deferred = ui.deferred;
  const timer = useRef<number | null>(null);

  // Restart the auto-hide on every new defer (seq changes), so rapid defers
  // do not inherit a nearly-expired timer.
  useEffect(() => {
    if (!deferred) return;
    timer.current = window.setTimeout(() => store.dismissDeferredNotice(), UNDO_VISIBLE_MS);
    return () => {
      if (timer.current !== null) window.clearTimeout(timer.current);
    };
  }, [deferred?.seq, deferred, store]);

  if (!deferred) return null;
  return (
    <div className="cwyc-deferred-undo" data-testid="deferred-undo" role="status">
      {/* The label doubles as the door to the tray (task #33): the moment a
          card was just set aside is exactly when "where did it go?" arises. */}
      <button
        type="button"
        className="cwyc-deferred-undo-text"
        data-testid="deferred-undo-view"
        title="See every card set aside today"
        onClick={() => {
          store.openSetAside();
          store.dismissDeferredNotice();
        }}
      >
        Card set aside
      </button>
      <button
        type="button"
        className="cwyc-chip"
        data-testid="deferred-undo-button"
        onClick={() => store.undoDefer(deferred.cardId)}
      >
        Undo
      </button>
      <button
        type="button"
        className="cwyc-deferred-undo-dismiss"
        aria-label="Dismiss"
        onClick={() => store.dismissDeferredNotice()}
      >
        ×
      </button>
    </div>
  );
}
