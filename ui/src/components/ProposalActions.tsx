import type { ChatStore, ProposalCardData } from "../store";

/**
 * What you can still do to a proposal AFTER it resolves.
 *
 * The React migration rendered the action row only while `pending`, so a
 * resolved card had no buttons at all - while Python kept every handler
 * (proposal_revert / proposal_readd / proposal_restore) and the `revertible`
 * flag it pushes on proposal_resolved. The entire safety net for applied
 * changes was reachable by no means whatsoever (task #18).
 *
 * Degrade, never hide: a change that cannot be reverted still says so, with
 * the reason, rather than leaving a blank card that looks final.
 */
export function ProposalActions({
  data,
  store,
}: {
  data: ProposalCardData;
  store: ChatStore;
}) {
  const status = data.status;

  if (status === "accepted" || status === "auto-accepted") {
    // `revertible === false` means only an Anki backup can undo it (bulk
    // deletes, skill writes). Say that instead of offering a button that is
    // guaranteed to fail.
    const revertible = (data as { revertible?: boolean }).revertible !== false;
    if (!revertible) {
      return (
        <div className="cwyc-proposal-actions cwyc-proposal-after" data-testid="proposal-after" data-proposal-id={data.id}>
          <span className="cwyc-proposal-after-note">
            Applied — undo this from Anki’s backup (File → Switch Profile)
          </span>
        </div>
      );
    }
    return (
      <div className="cwyc-proposal-actions cwyc-proposal-after" data-testid="proposal-after" data-proposal-id={data.id}>
        {/* The override appears only once Python has actually REFUSED, and
            says what it would cost. It is driven by the refusal itself
            (proposals.py's StaleRevert -> proposal_error{conflict:true}), not
            by a hidden gesture: an override nobody can find is the same as no
            override, and one offered up front invites skipping the check. */}
        {data.revertConflict ? (
          <>
            <button
              type="button"
              className="cwyc-btn-reject"
              data-testid="proposal-revert-keep"
              onClick={() => store.dismissProposalError(data.id)}
            >
              Keep the newer change
            </button>
            <button
              type="button"
              className="cwyc-btn-suggest"
              data-testid="proposal-revert-force"
              onClick={() => store.revertProposal(data.id, true)}
            >
              Undo anyway
            </button>
          </>
        ) : (
          <button
            type="button"
            className="cwyc-btn-suggest"
            data-testid="proposal-revert"
            onClick={() => store.revertProposal(data.id)}
            title="Undo this change. If the note changed since, you will be asked first."
          >
            Undo
          </button>
        )}
      </div>
    );
  }

  if (status === "undone") {
    return (
      <div className="cwyc-proposal-actions cwyc-proposal-after" data-testid="proposal-after" data-proposal-id={data.id}>
        <button
          type="button"
          className="cwyc-btn-suggest"
          data-testid="proposal-readd"
          onClick={() => store.readdProposal(data.id)}
        >
          Re-apply
        </button>
      </div>
    );
  }

  if (status === "rejected" || status === "superseded") {
    return (
      <div className="cwyc-proposal-actions cwyc-proposal-after" data-testid="proposal-after" data-proposal-id={data.id}>
        <button
          type="button"
          className="cwyc-btn-suggest"
          data-testid="proposal-restore"
          onClick={() => store.restoreProposal(data.id)}
        >
          Put back for review
        </button>
      </div>
    );
  }

  return null;
}
