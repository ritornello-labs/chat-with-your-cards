import type { InteractionDocument, InteractionStatus } from "@elvis-labs/interaction-ui";
import type { ProposalPayload } from "./events";

const STATUS_MAP: Record<string, InteractionStatus> = {
  pending: "pending",
  accepted: "succeeded",
  "auto-accepted": "succeeded",
  rejected: "rejected",
  undone: "failed",
  superseded: "expired",
};

/**
 * Adapt CWYC's local ProposalManager payload to the renderer-neutral protocol.
 * Execution intentionally stays inside ProposalManager, its sole Anki write
 * chokepoint; this adapter changes review semantics and presentation only.
 */
export function createProposalInteraction(proposal: ProposalPayload): InteractionDocument {
  const revision = proposal.revision ?? 1;
  const fields = Object.fromEntries((proposal.fields ?? []).map((field) => [field.name, field.new]));
  const blocks: InteractionDocument["view"]["blocks"] = [
    {
      type: "key_value",
      items: [
        { label: "Deck", value: proposal.deck },
        { label: "Note type", value: proposal.note_type },
      ],
    },
    { type: "field_set", fields: proposal.fields ?? [] },
  ];
  if (proposal.previews) blocks.splice(1, 0, { type: "card_preview", ref: `cwyc:${proposal.id}:${revision}` });
  blocks.push(...(proposal.warnings ?? []).map((text) => ({ type: "warning" as const, text, severity: "warning" })));
  return {
    schema_version: "1.0",
    id: proposal.id,
    revision,
    status: STATUS_MAP[proposal.status] ?? "failed",
    operation_digest: proposal.operation_digest ?? `legacy:${proposal.id}:${revision}`,
    operation: {
      type: "anki.note.create",
      deck: proposal.deck,
      note_type: proposal.note_type,
      fields,
      tags: proposal.tags ?? [],
    },
    view: {
      title: "Create Anki note",
      summary: proposal.rationale,
      blocks,
    },
    actions: [
      { id: "revise", intent: "revise", label: "Edit" },
      { id: "reject", intent: "reject", label: "Reject" },
      { id: "approve", intent: "approve", label: "Add note" },
    ],
    policy: { risk: "routine" },
    owner: "chat-with-your-cards",
  };
}
