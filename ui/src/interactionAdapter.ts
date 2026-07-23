import type { InteractionPresentation, MediaAttachment } from "@elvis-labs/interaction-schema";
import type { ProposalPayload } from "./events";

function isAudioDataAttachment(item: unknown): item is MediaAttachment {
  return (
    !!item &&
    typeof item === "object" &&
    (item as { kind?: unknown }).kind === "audio" &&
    typeof (item as { src?: unknown }).src === "string" &&
    (item as { src: string }).src.startsWith("data:")
  );
}

/**
 * Audio riding the proposal payload -> schema-1.1 MediaAttachment[]. Two
 * sources, both drawn as the same player strip: `media` (task #21, newly
 * staged files awaiting import) and `preview_media` (task #25, [sound:...]
 * refs to media already in the collection, resolved to data: URIs so reused
 * audio is replayable too). Python emits the exact shape; this filter is
 * belt-and-braces (the schema validator and renderer both re-check), keeping
 * non-data: src out at the first trusted layer.
 */
function mediaAttachments(proposal: ProposalPayload): MediaAttachment[] {
  const staged = (proposal as { media?: unknown }).media;
  const preview = (proposal as { preview_media?: unknown }).preview_media;
  const all = [...(Array.isArray(staged) ? staged : []), ...(Array.isArray(preview) ? preview : [])];
  return all.filter(isAudioDataAttachment);
}

/**
 * Status -> badge mapping. The label strings are exactly what the old
 * combined renderer (interaction-ui 0.1.0) displayed for CWYC's statuses
 * (pending->"Pending review", accepted/auto-accepted->"Completed",
 * undone->"Failed", superseded->"Expired"), preserved through the package
 * split: the renderer no longer owns any status vocabulary - hosts hand it
 * finished strings (interaction-schema's PresentationBadge).
 */
const BADGES: Record<string, { label: string; tone: "neutral" | "negative" }> = {
  pending: { label: "Pending review", tone: "neutral" },
  accepted: { label: "Completed", tone: "neutral" },
  "auto-accepted": { label: "Completed", tone: "neutral" },
  rejected: { label: "Rejected", tone: "negative" },
  undone: { label: "Failed", tone: "negative" },
  superseded: { label: "Expired", tone: "neutral" },
};

/**
 * Adapt CWYC's local ProposalManager payload to the renderer-neutral
 * presentation standard (interaction-schema 1.0). Execution intentionally
 * stays inside ProposalManager, its sole Anki write chokepoint; this adapter
 * changes presentation only, and it is the layer that decides actionability:
 * the renderer shows action buttons iff we pass them, so only `pending`
 * proposals get any (same gating the old renderer did internally on status).
 * `revision` and `digest` are opaque tokens to the renderer - it echoes them
 * byte-for-byte; ProposalCard converts revision back to the bridge's number
 * at the boundary.
 */
export function createProposalInteraction(proposal: ProposalPayload): InteractionPresentation {
  const revision = proposal.revision ?? 1;
  const blocks: InteractionPresentation["blocks"] = [
    {
      type: "key_value",
      items: [
        { label: "Deck", value: proposal.deck },
        { label: "Note type", value: proposal.note_type },
      ],
    },
    { type: "field_set", fields: proposal.fields ?? [] },
  ];
  // Media (schema 1.1) rides the preview block; the renderer draws the
  // player strip as block chrome ALONGSIDE our renderBlock-injected flip
  // card, so a proposal with audio but no renderable preview still needs
  // the block to exist for the strip to have a home.
  const media = mediaAttachments(proposal);
  if (proposal.previews || media.length > 0) {
    blocks.splice(1, 0, {
      type: "card_preview",
      ref: `cwyc:${proposal.id}:${revision}`,
      ...(media.length > 0 ? { media } : {}),
    });
  }
  blocks.push(
    ...(proposal.warnings ?? []).map((text) => ({ type: "warning" as const, text, severity: "warning" }))
  );
  const pending = proposal.status === "pending";
  return {
    // 1.1 = 1.0 + optional card-preview media; emitted unconditionally (an
    // additive version is valid with or without the new field).
    version: "1.1",
    interactionId: proposal.id,
    revision: String(revision),
    digest: proposal.operation_digest ?? `legacy:${proposal.id}:${revision}`,
    eyebrow: "anki.note.create",
    title: "Create Anki note",
    summary: proposal.rationale,
    badge: BADGES[proposal.status] ?? { label: "Failed", tone: "negative" },
    blocks,
    // No "revise"/Edit action: the renderer is click-to-edit (click a field
    // to edit it in place; a Save action appears once the draft diverges),
    // so an explicit Edit button would be dead chrome.
    actions: pending
      ? [
          { id: "reject", intent: "reject", label: "Reject" },
          { id: "approve", intent: "approve", label: "Add note" },
        ]
      : [],
    revisionLabel: `Revision ${revision}`,
  };
}
