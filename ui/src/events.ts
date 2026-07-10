/**
 * The ChatEvent vocabulary this UI understands, mirroring
 * chat_with_your_cards/backends/base.py's event_to_dict() output plus the
 * richer UI-directed "proposal" family pushed by proposals.py
 * (ProposalManager._push -> Proposal.to_payload()) and controller.py.
 *
 * Only the events this scaffold actually renders are typed strictly; any
 * other `{type: string, ...}` dict is accepted and silently ignored by the
 * store, matching app.js's dispatch()'s forward-compatible default case.
 */

export interface TextDeltaEvent {
  type: "text_delta";
  text: string;
}

/**
 * NOT part of backends/base.py's ChatEvent union yet - stubbed only by the
 * dev replayer (src/dev/replayer.ts) to prove out assistant-ui's Reasoning
 * primitive ahead of a real backend emitting it. Real bridge.py traffic will
 * never send this until a backend adds it upstream.
 */
export interface ThinkingDeltaEvent {
  type: "thinking_delta";
  text: string;
}

export interface ToolCallStartedEvent {
  type: "tool_call_started";
  call_id: string;
  tool: string;
  summary: string;
}

export interface ToolCallFinishedEvent {
  type: "tool_call_finished";
  call_id: string;
  ok: boolean;
  summary?: string;
}

/** One entry of Proposal.to_payload()["fields"] (proposals.py). */
export interface ProposalFieldPayload {
  name: string;
  new: string;
  old?: string;
}

/**
 * Proposal.to_payload() (chat_with_your_cards/proposals.py) - the dict
 * app.js's renderProposal() consumes. Only "create" and "edit" kinds get a
 * dedicated card in this scaffold (Approve / Edit / Reject, per the task);
 * other kinds (bulk/delete/change_set/deck_op/skill_update) render through
 * the same generic fallback so nothing throws, but without kind-specific UI.
 */
export interface ProposalPayload {
  id: string;
  kind: string;
  status: string;
  note_type: string;
  deck: string;
  tags: string[];
  note_id: number | null;
  fields: ProposalFieldPayload[];
  add_tags: string[];
  remove_tags: string[];
  rationale: string;
  warnings: string[];
  previews: unknown;
  op: string;
  op_args: Record<string, unknown>;
  title: string;
  count: number;
  samples: unknown[];
  items: unknown[];
  open: boolean;
}

export interface ProposalEvent {
  type: "proposal";
  proposal: ProposalPayload;
}

export interface ProposalResolvedEvent {
  type: "proposal_resolved";
  id: string;
  status: string;
  note_id?: number | null;
  warnings?: string[];
  revertible?: boolean;
}

export interface ProposalErrorEvent {
  type: "proposal_error";
  id: string;
  message: string;
}

export interface UsageEvent {
  type: "usage";
  cost_usd: number | null;
  input_tokens: number | null;
  output_tokens: number | null;
}

export interface DoneEvent {
  type: "done";
}

export interface CancelledEvent {
  type: "cancelled";
}

export interface ErrorEvent {
  type: "error";
  message: string;
}

export interface ResetEvent {
  type: "reset";
}

export type KnownChatEvent =
  | TextDeltaEvent
  | ThinkingDeltaEvent
  | ToolCallStartedEvent
  | ToolCallFinishedEvent
  | ProposalEvent
  | ProposalResolvedEvent
  | ProposalErrorEvent
  | UsageEvent
  | DoneEvent
  | CancelledEvent
  | ErrorEvent
  | ResetEvent;

/** Any dict Python (or the dev replayer) pushes; unknown types are ignored. */
export type ChatEvent = KnownChatEvent | { type: string; [key: string]: unknown };

/** Commands this UI sends to Python, mirroring app.js's post() call sites. */
export interface BridgeCommand {
  type: string;
  [key: string]: unknown;
}
