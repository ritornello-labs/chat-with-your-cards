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
  revision?: number;
  operation_digest?: string;
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
  /** Staged audio attachments (proposals.py Proposal.media, task #21):
   *  playable data: URIs for the review card's schema-1.1 player strip.
   *  final_name appears after accept-time import (rename tracking). */
  media?: {
    id: string;
    kind: string;
    name: string;
    mime: string;
    src: string;
    bytes?: number;
    final_name?: string;
  }[];
  /** Preview-only audio (proposals.py Proposal.preview_media): [sound:...]
   *  markers resolving to media ALREADY in the collection, as data: URIs for
   *  the same player strip. Never imported on accept (already in the
   *  collection), so it carries no final_name. */
  preview_media?: {
    id: string;
    kind: string;
    name: string;
    mime: string;
    src: string;
    bytes?: number;
  }[];
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

export interface GradingCardSummary {
  card_id: number;
  note_id: number;
  deck: string;
  current_deck: string;
  template: string;
  prompt_field: string;
  prompt: string;
  queue: number;
  hidden_state: string | null;
  preview_filtered: boolean;
  rescheduling_filtered: boolean;
}

export interface GradingPayload {
  id: string;
  action: "fail" | "make_available";
  status: "pending" | "applying" | "accepted" | "auto-accepted" | "rejected" | "failed";
  card_ids: number[];
  cards: GradingCardSummary[];
  rationale: string;
  warnings: string[];
  result: Record<string, unknown> | null;
  availability: Record<string, unknown> | null;
  available_card_ids: number[];
  automatic_mode: string | null;
}

export interface GradingEvent {
  type: "grading";
  grading: GradingPayload;
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
  /** Whether the agent inherits the user's own Claude Code MCP servers. */
  mcp_inherit_user?: boolean;
  /** Whether sandboxed inline widgets (render_widget) may render. */
  widget_rendering?: boolean;
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

/** An image the agent chose to show inline (show_image tool), as a data URI. */
export interface InlineImageEvent {
  type: "inline_image";
  src: string;
  caption: string;
  bytes?: number;
}

/**
 * A sandboxed widget the agent rendered (render_widget tool). `html` is
 * agent output and therefore UNTRUSTED - WidgetCard (Thread.tsx) confines it
 * to an opaque-origin iframe with a no-network CSP; it must never reach the
 * document any other way.
 */
export interface InlineWidgetEvent {
  type: "inline_widget";
  html: string;
  title: string;
}

/**
 * render_widget was called while widget rendering is off: offer the user the
 * consent switch inline (ephemeral - not recorded to the transcript).
 */
export interface WidgetOfferEvent {
  type: "widget_offer";
  title: string;
}

/**
 * Ask-each-read (approvals.py): a read tool call is BLOCKED on the MCP thread
 * waiting for this answer. Python pushes this, then waits up to
 * APPROVAL_TIMEOUT_S (120s) for a `tool_approval_response` command before
 * denying by itself. Rendering it is therefore not optional - an unhandled
 * event is a two-minute freeze followed by a spurious "user declined".
 */
export interface ToolApprovalEvent {
  type: "tool_approval";
  id: string;
  tool: string;
  summary: string;
}

/**
 * The approval was settled: by our own response (echoed back), by the 120s
 * timeout (`reason: "timed out"`), or by session teardown. Idempotent - the
 * chip may already have marked itself resolved optimistically.
 */
export interface ToolApprovalResolvedEvent {
  type: "tool_approval_resolved";
  id: string;
  allow: boolean;
  reason?: string;
  /** True when the answer arrived after the tool call had already given up its
   *  slot, so it cannot resume that call - only the agent's next attempt can
   *  use it. The chip says so rather than implying work continued. */
  late?: boolean;
}

/**
 * First-run onboarding (task #19): pushed by controller.py's _build_backend
 * the first time it falls back to the demo backend because Claude Code
 * couldn't be found (find_claude_cli() returned None). `platform` is
 * "darwin" | "linux" | "windows" (sys.platform, mapped) and picks which
 * install one-liner the setup card shows. Superseded the old plain "notice"
 * fallback - see controller.py.
 */
export interface SetupNeededEvent {
  type: "setup_needed";
  platform: string;
}

/**
 * Pushed by ChatController.recheck_backend() when the "Re-check" button's
 * retry finds the CLI: dismisses the setup card. No Anki restart involved -
 * the backend/session are rebuilt in-process (DESIGN.md section 9's
 * "no restart" contract, same as new_chat()).
 */
export interface SetupResolvedEvent {
  type: "setup_resolved";
}

export type KnownChatEvent =
  | TextDeltaEvent
  | ThinkingDeltaEvent
  | ToolCallStartedEvent
  | ToolCallFinishedEvent
  | ProposalEvent
  | ProposalResolvedEvent
  | ProposalErrorEvent
  | GradingEvent
  | UsageEvent
  | DockStateEvent
  | SettingsEvent
  | DoneEvent
  | CancelledEvent
  | ErrorEvent
  | ResetEvent
  | InlineImageEvent
  | InlineWidgetEvent
  | WidgetOfferEvent
  | ToolApprovalEvent
  | ToolApprovalResolvedEvent
  | SetupNeededEvent
  | SetupResolvedEvent;

/** Any dict Python (or the dev replayer) pushes; unknown types are ignored. */
export type ChatEvent = KnownChatEvent | { type: string; [key: string]: unknown };

/** Commands this UI sends to Python, mirroring app.js's post() call sites. */
export interface BridgeCommand {
  type: string;
  [key: string]: unknown;
}
