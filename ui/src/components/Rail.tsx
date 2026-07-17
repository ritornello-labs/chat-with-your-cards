import { useChatState } from "../ChatRuntimeProvider";
import type { ChatStore } from "../store";

/**
 * The collapsed dock: a slim, always-visible rail that IS the add-on's
 * presence when the chat is put away. One generous click target (the whole
 * rail) expands it. Identity carriers: the ember (faint at rest, breathing
 * while a reply is streaming, lit while a finished reply waits unread) and
 * the vertical wordmark; the chevron at the bottom points away from the dock
 * edge, i.e. the direction the panel will grow. Rendered as an overlay layer
 * inside .cwyc-app; visibility is driven by the host's dock_state pushes
 * (see styles.css "dock shell" section).
 */
export function Rail({ store }: { store: ChatStore }) {
  const { isRunning, hasUnread, ui } = useChatState(store);
  const side = ui.dock?.side ?? "right";
  const emberState = isRunning
    ? " cwyc-rail-ember-live"
    : hasUnread
      ? " cwyc-rail-ember-unread"
      : "";
  return (
    <button
      type="button"
      className="cwyc-rail"
      data-testid="rail"
      title="Open chat (Ctrl+J)"
      aria-label="Open chat"
      onClick={() => store.setDockExpanded(true)}
    >
      <span className={"cwyc-rail-ember" + emberState} aria-hidden="true" />
      <span className="cwyc-rail-word" aria-hidden="true">
        Chat with your cards
      </span>
      <svg
        className="cwyc-rail-chevron"
        viewBox="0 0 12 12"
        width="11"
        height="11"
        aria-hidden="true"
      >
        {side === "right" ? (
          <path
            d="M7.5 2.5 4 6l3.5 3.5"
            stroke="currentColor"
            strokeWidth="1.5"
            strokeLinecap="round"
            fill="none"
          />
        ) : (
          <path
            d="M4.5 2.5 8 6l-3.5 3.5"
            stroke="currentColor"
            strokeWidth="1.5"
            strokeLinecap="round"
            fill="none"
          />
        )}
      </svg>
    </button>
  );
}
