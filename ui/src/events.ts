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
 * Mirrors backends/base.py's ThinkingDelta (landed 2026-07-11). `text` is
 * empty at every reasoning effort level observed from the real CLI today -
 * the account/CLI redacts it upstream, only an opaque signature carries it -
 * so `estimated_tokens` (not non-empty text) is the signal that a thinking
 * phase is actually live. The parser (claude_cli.py) emits one of these on
 * `content_block_start` for a thinking block (text "", estimated_tokens
 * null - opens the UI's indicator immediately) and one per subsequent
 * `thinking_delta` stream event (carrying text and/or estimated_tokens when
 * present). store.ts drives the Reasoning part's rotating "Thinking…"
 * header off estimated_tokens; see ReasoningBlock.tsx.
 */
export interface ThinkingDeltaEvent {
  type: "thinking_delta";
  text: string;
  estimated_tokens?: number | null;
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

/**
 * cache_read_tokens/cache_creation_tokens mirror the Anthropic API's
 * cache_read_input_tokens/cache_creation_input_tokens usage fields
 * (backends/claude_cli.py's result handling; backends/base.py's
 * UsageUpdate). Together with input_tokens they approximate the size of
 * the context sent on the last turn.
 *
 * context_window is the real per-turn window the CLI reports (modelUsage);
 * when present the footer prefers it over the hardcoded contextWindow.ts
 * table. fast_mode_state ("on"/"off") is the CLI's ground-truth report of
 * whether the running process actually engaged fast mode.
 */
export interface UsageEvent {
  type: "usage";
  cost_usd: number | null;
  input_tokens: number | null;
  output_tokens: number | null;
  cache_read_tokens?: number | null;
  cache_creation_tokens?: number | null;
  context_window?: number | null;
  fast_mode_state?: string | null;
}

/**
 * Dock shell state pushed by Python (dock.py). `animating: true` arrives at
 * the START of a width animation (so the UI can crossfade + pin its layout
 * width before the webview starts resizing) and `animating: false` at the
 * end. `width` is the EXPANDED width in px (the rail width is a constant the
 * CSS owns); `side` picks the chevron directions.
 */
export interface DockStateEvent {
  type: "dock_state";
  expanded: boolean;
  animating: boolean;
  width: number;
  side: "left" | "right";
}

/**
 * The user-facing settings snapshot (__init__.py's _push_settings): pushed on
 * ready and re-pushed after every accepted set_setting command, so the panel
 * always reflects the authoritative persisted config.
 */
export interface SettingsEvent {
  type: "settings";
  restore_last_chat: boolean;
  dock_side: "left" | "right";
  toggle_shortcut: string;
  new_chat_shortcut: string;
  /** Composer vim keybindings (off by default). */
  vim_mode?: boolean;
  /** [keys, mapped-to, mode] triples applied via vim `:map` semantics. */
  vim_mappings?: [string, string, string][];
  /** Selected colour palette: "teal" (default) | "indigo" | "evergreen". */
  theme?: string;
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
  | DockStateEvent
  | SettingsEvent
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
