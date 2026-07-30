import { useState } from "react";
import { useChatState } from "../ChatRuntimeProvider";
import type { ChatStore, SetAsideEntry } from "../store";

/**
 * The set-aside tray (task #33): a full-pane view the header tray button
 * flips to, listing today's set-aside cards as chips - the "important
 * primitive" view of what the tracked-bury deferral engine is holding.
 *
 * Each chip: deck (leaf-first, like the pickers) + a clamped front snippet.
 * Clicking the chip body expands the front/back text preview in place (no
 * modal - the dock is narrow, and keeping the list visible preserves the
 * "which of these did I want?" comparison the tray exists for). The
 * always-visible action on the right is per-card "Review next" (unbury +
 * pin, same verb as the transient Undo chip); the header offers "Bring all
 * back" (plain unbury, no pin - "all of them next" is not a thing).
 *
 * Content is TEXT, not a template render: deferral.py's card_summary strips
 * the rendered HTML and collapses media to [image]/[audio] markers, because
 * the dock webview must not run arbitrary note-type CSS/JS and a snippet is
 * what a "which card was that?" glance needs.
 */

function splitDeck(deck: string): { path: string; leaf: string } {
  const parts = deck.split("::").filter(Boolean);
  if (parts.length <= 1) return { path: "", leaf: deck };
  return { path: parts.slice(0, -1).join(" › "), leaf: parts[parts.length - 1] };
}

function AsideCard({ entry, store }: { entry: SetAsideEntry; store: ChatStore }) {
  const [expanded, setExpanded] = useState(false);
  const { path, leaf } = splitDeck(entry.deck);
  return (
    <li className={"cwyc-aside-card" + (expanded ? " cwyc-aside-expanded" : "")}>
      <div className="cwyc-aside-row">
        <button
          type="button"
          className="cwyc-aside-summary"
          aria-expanded={expanded}
          data-testid={`aside-card-${entry.cardId}`}
          title={expanded ? "Collapse the preview" : "Preview this card's front and back"}
          onClick={() => setExpanded(!expanded)}
        >
          <span className="cwyc-aside-deck">
            {path ? <span className="cwyc-aside-deck-path">{path}</span> : null}
            <span className="cwyc-aside-deck-leaf">{leaf}</span>
          </span>
          <span className="cwyc-aside-front">{entry.front || "(blank front)"}</span>
          <svg
            className="cwyc-aside-chevron"
            viewBox="0 0 12 12"
            width="10"
            height="10"
            aria-hidden="true"
          >
            <path
              d="M3 4.5 6 7.5l3-3"
              stroke="currentColor"
              strokeWidth="1.5"
              strokeLinecap="round"
              strokeLinejoin="round"
              fill="none"
            />
          </svg>
        </button>
        <button
          type="button"
          className="cwyc-aside-next"
          data-testid={`aside-next-${entry.cardId}`}
          title="Review this card next"
          aria-label={`Review next: ${entry.front.slice(0, 60)}`}
          onClick={() => store.bringBackCard(entry.cardId)}
        >
          {/* the Set-aside chip's card-stepping-back glyph, mirrored: the card steps forward */}
          <svg viewBox="0 0 14 14" width="12" height="12" aria-hidden="true">
            <path
              d="M12 3.5H4v7h8z M3 5 1 7l2 2"
              fill="none"
              stroke="currentColor"
              strokeWidth="1.4"
              strokeLinecap="round"
              strokeLinejoin="round"
            />
          </svg>
        </button>
      </div>
      {expanded ? (
        <div className="cwyc-aside-detail">
          <div className="cwyc-aside-side-label">Front</div>
          <div className="cwyc-aside-text">{entry.front || "(blank)"}</div>
          <div className="cwyc-aside-side-label">Back</div>
          <div className="cwyc-aside-text">{entry.back || "(blank)"}</div>
        </div>
      ) : null}
    </li>
  );
}

export function SetAsidePane({ store }: { store: ChatStore }) {
  const ui = useChatState(store).ui;
  const entries = ui.setAside;
  const shortcut = ui.settings?.deferShortcut || "Ctrl+Shift+D";
  return (
    <div className="cwyc-aside" data-testid="aside-pane" role="region" aria-label="Cards set aside today">
      <div className="cwyc-aside-top">
        <button
          type="button"
          className="cwyc-aside-back"
          data-testid="aside-back"
          title="Back to the chat (Esc)"
          aria-label="Back to the chat"
          onClick={() => store.closeSetAside()}
        >
          <svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true">
            <path
              d="M9.8 3.5 5.3 8l4.5 4.5"
              stroke="currentColor"
              strokeWidth="1.6"
              strokeLinecap="round"
              strokeLinejoin="round"
              fill="none"
            />
          </svg>
        </button>
        <div className="cwyc-aside-heading">
          <span className="cwyc-aside-title">Set aside</span>
          <span className="cwyc-aside-sub">
            {entries.length === 0
              ? "nothing today"
              : entries.length === 1
                ? "1 card today"
                : `${entries.length} cards today`}
          </span>
        </div>
        {entries.length > 0 ? (
          <button
            type="button"
            className="cwyc-chip cwyc-aside-all"
            data-testid="aside-bring-all"
            title="Every set-aside card returns to today's queue"
            onClick={() => store.bringAllBack()}
          >
            Bring all back
          </button>
        ) : null}
      </div>
      {entries.length === 0 ? (
        <div className="cwyc-aside-empty" data-testid="aside-empty">
          <svg viewBox="0 0 28 28" width="26" height="26" aria-hidden="true">
            <path
              d="M4 8.5h16v13H4z M23 11l3 3-3 3"
              fill="none"
              stroke="currentColor"
              strokeWidth="1.6"
              strokeLinecap="round"
              strokeLinejoin="round"
            />
            <path d="M7.5 13h9M7.5 16.5h6" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" />
          </svg>
          <p className="cwyc-aside-empty-title">Nothing set aside</p>
          <p className="cwyc-aside-empty-hint">
            While reviewing, the <strong>Set aside</strong> chip (or {shortcut}) parks the
            card here. It stays out of today&rsquo;s counts until you bring it back &mdash;
            or until the next day begins, when Anki returns it on its own.
          </p>
        </div>
      ) : (
        <ul className="cwyc-aside-list">
          {entries.map((entry) => (
            <AsideCard key={entry.cardId} entry={entry} store={store} />
          ))}
        </ul>
      )}
    </div>
  );
}
