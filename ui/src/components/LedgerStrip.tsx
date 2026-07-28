import { useState } from "react";
import type { ChatStore } from "../store";
import { useChatState } from "../ChatRuntimeProvider";

/**
 * What this chat has actually changed in the collection, and the way back.
 *
 * proposals.py has pushed `ledger` since M2 and __init__.py has bound
 * `undo_session` and `open_session_browser` the whole time - but nothing
 * rendered any of it and it was not on the Tools menu either, so session-wide
 * undo and the Browser jump were unreachable by ANY means (task #18).
 *
 * Deliberately quiet: a one-line summary that only expands on request. It sits
 * above the composer because it is about the session, not about any one
 * message, and a change you cannot see is a change you cannot undo.
 */
export function LedgerStrip({ store }: { store: ChatStore }) {
  const { ui } = useChatState(store);
  const [open, setOpen] = useState(false);
  const [confirmUndo, setConfirmUndo] = useState(false);
  const entries = ui.ledger.entries;
  const live = entries.filter((e) => !e.undone);
  if (!entries.length) return null;

  return (
    <div className="cwyc-ledger" data-testid="ledger-strip">
      <div className="cwyc-ledger-head">
        <button
          type="button"
          className="cwyc-ledger-toggle"
          data-testid="ledger-toggle"
          aria-expanded={open}
          onClick={() => setOpen((v) => !v)}
        >
          {live.length
            ? `${live.length} change${live.length === 1 ? "" : "s"} this chat`
            : "All changes undone"}
        </button>
        <span className="cwyc-ledger-actions">
          <button
            type="button"
            className="cwyc-ledger-link"
            data-testid="ledger-browse"
            onClick={() => store.openSessionBrowser()}
            title="Open Anki's Browser filtered to this chat's notes"
          >
            Browse
          </button>
          {live.length ? (
            confirmUndo ? (
              <>
                <button
                  type="button"
                  className="cwyc-ledger-link"
                  onClick={() => setConfirmUndo(false)}
                >
                  Cancel
                </button>
                <button
                  type="button"
                  className="cwyc-ledger-link cwyc-ledger-danger"
                  data-testid="ledger-undo-confirm"
                  onClick={() => {
                    setConfirmUndo(false);
                    store.undoSession();
                  }}
                >
                  Undo all
                </button>
              </>
            ) : (
              // Confirmed, not immediate: this reverses everything the chat
              // did. Anything it cannot safely undo is left alone and
              // reported, rather than forced.
              <button
                type="button"
                className="cwyc-ledger-link"
                data-testid="ledger-undo"
                onClick={() => setConfirmUndo(true)}
              >
                Undo all
              </button>
            )
          ) : null}
        </span>
      </div>
      {open ? (
        <ul className="cwyc-ledger-list">
          {entries.map((entry) => (
            <li
              key={entry.id}
              className={"cwyc-ledger-item" + (entry.undone ? " cwyc-ledger-undone" : "")}
            >
              <span className="cwyc-ledger-label">{entry.label || entry.kind}</span>
              {entry.undone ? (
                <span className="cwyc-ledger-state">undone</span>
              ) : entry.revertible ? (
                <button
                  type="button"
                  className="cwyc-ledger-link"
                  data-testid="ledger-revert"
                  onClick={() => store.revertProposal(entry.id)}
                >
                  Undo
                </button>
              ) : (
                // Never a button that is guaranteed to fail: bulk deletes and
                // skill writes are backup-only.
                <span className="cwyc-ledger-state" title="Only an Anki backup can undo this">
                  backup only
                </span>
              )}
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}
