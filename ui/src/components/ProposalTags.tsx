/**
 * Tags on an edit proposal: what the note carries now, plus what this change
 * would add or remove.
 *
 * This is a correctness fix, not decoration. The React migration dropped tag
 * rendering entirely, so a TAG-ONLY edit proposal - add `leech`, drop
 * `needs-source` - showed literally no change at all: same fields, same
 * preview, nothing to review. Approving it was an act of faith.
 *
 * Read-only by design. Edits carry a specific tag delta the assistant computed
 * and Python applies verbatim; letting the card mutate it would need a second
 * delta to travel back, and the accept path has no channel for one. Creates
 * get a real editor instead (ProposalCard's ProposalDestination).
 */
export function ProposalTagDiff({
  tags,
  addTags,
  removeTags,
}: {
  tags: readonly string[];
  addTags: readonly string[];
  removeTags: readonly string[];
}) {
  // Both deltas are filtered against what the note actually carries: adding a
  // tag it already has, or removing one it does not, changes nothing, and
  // drawing either as a change would promise something that will not happen.
  const current = new Set(tags);
  const added = addTags.filter((tag) => !current.has(tag));
  const removed = removeTags.filter((tag) => current.has(tag));
  const removing = new Set(removed);
  if (!tags.length && !added.length) return null;

  return (
    <div className="cwyc-proposal-tags" data-testid="proposal-tags">
      <span className="cwyc-proposal-tags-label">Tags</span>
      <span className="cwyc-proposal-tags-list">
        {tags.map((tag) => (
          <span
            key={`now-${tag}`}
            className={"cwyc-tagchip cwyc-tagchip-static" + (removing.has(tag) ? " cwyc-tag-removed" : "")}
          >
            {removing.has(tag) ? <span aria-hidden="true">−&nbsp;</span> : null}
            {tag}
          </span>
        ))}
        {added.map((tag) => (
          <span key={`add-${tag}`} className="cwyc-tagchip cwyc-tagchip-static cwyc-tag-added">
            <span aria-hidden="true">+&nbsp;</span>
            {tag}
          </span>
        ))}
        {!tags.length && !added.length ? (
          <span className="cwyc-proposal-tags-empty">none</span>
        ) : null}
      </span>
      {/* The +/- glyphs are decorative; state the delta once, in words, for
          anyone who cannot see the colour or the sign. */}
      <span className="cwyc-sr-only">
        {added.length ? `adding ${added.join(", ")}. ` : ""}
        {removed.length ? `removing ${removed.join(", ")}.` : ""}
      </span>
    </div>
  );
}
