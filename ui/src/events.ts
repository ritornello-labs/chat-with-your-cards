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
 * app.js's renderProposal() consumes. "create" and "edit" get field-level
 * editing; the other kinds (bulk/delete/change_set/deck_op/skill_update) share
 * one card whose body is built from `op`, `op_args`, `samples` and `items`
 * (ProposalBody.tsx).
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
  /** Whether accepting this can be undone from the dock (#7). Absent when the
   *  proposal kind's default decides; false is the card's red irreversibility
   *  line. */
  revertible?: boolean;
  /** Edit only (#24a): the note's OTHER fields, unchanged by this proposal.
   *  Read-only review context - the card keeps them collapsed by default so
   *  the diff stays the thing you actually see. */
  context_fields?: { name: string; value: string }[];
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
  /** A revert refused because it would discard a change made after ours; the
   *  card may offer an explicit override (proposals.py's StaleRevert). */
  conflict?: boolean;
}

/**
 * proposals.py's preview_request answering a debounced `proposal_preview`:
 * the card re-rendered from the draft the user is typing, so an edit is
 * reviewed against what it will actually look like rather than against the
 * preview the assistant proposed. Carries `previews` and nothing else - it
 * must never move status, fields, or revision.
 */
export interface PreviewUpdateEvent {
  type: "preview_update";
  id: string;
  previews: unknown;
}

/** One applied change this session, from proposals.py's LedgerEntry. */
export interface LedgerEntryPayload {
  id: string;
  kind: string;
  note_id: number;
  label: string;
  undone: boolean;
  /** False when only an Anki backup can undo it (bulk deletes, skill writes);
   *  the UI must say so rather than offer a button that always fails. */
  revertible: boolean;
}

/**
 * The session ledger (proposals.py `_push_ledger`). Python has pushed this
 * since M2 and nothing rendered it, so session-wide undo and the Browser jump
 * were unreachable by any means (task #18).
 */
export interface LedgerEvent {
  type: "ledger";
  session_id: string;
  session_tag: string;
  entries: LedgerEntryPayload[];
}

/** Whether a review card is on screen (pushed on reviewer/state changes), so
 *  the composer's "Set aside" button only shows when it can do something. */
export interface ReviewStateEvent {
  type: "review_state";
  reviewing: boolean;
  card_id: number | null;
  /** Today's set-aside card count, for the tray badge (task #33). */
  set_aside_count?: number;
}

/** A card was just deferred (shortcut, menu, send-with-defer-on, or the
 *  agent); drives the transient "Undo" chip by the composer. */
export interface CardDeferredEvent {
  type: "card_deferred";
  card_id: number;
}

/** One set-aside card as the tray shows it (__init__.py's _deferred_entries
 *  via deferral.py's card_summary): stripped text, media as [image]/[audio]
 *  markers, newest-set-aside first. */
export interface SetAsideEntryPayload {
  card_id: number;
  deck: string;
  front: string;
  back: string;
}

/** What the assistant will see with the next message (#23a). */
export interface ContextEvent {
  type: "context";
  label: string;
  kind?: string;
}

/** Learning nudge state (#23d): pending edit-observation count. */
export interface LearningEvent {
  type: "learning";
  pending: number;
  nudge: boolean;
}

/** The full set-aside list (task #33): pushed on ready, on get_deferred, and
 *  after every deferral mutation, so the tray and its badge never go stale. */
export interface DeferredListEvent {
  type: "deferred_list";
  entries: SetAsideEntryPayload[];
}

/** Composer attachments (#15a): files staged for the NEXT message. Pushed on
 *  every change so the chips row never goes stale. No paths - those are
 *  agent-facing and ride the message text at send. */
export interface AttachmentPayload {
  id: string;
  name: string;
  kind: string;
  size: number;
}

export interface AttachmentsEvent {
  type: "attachments";
  items: AttachmentPayload[];
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
  /** Which native rating a `fail` request records (#16). Absent on payloads
   *  from before ratings were selectable; treat that as "again". */
  rating?: "again" | "hard" | "good" | "easy";
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
  /** Reviewing affordances (task #32): chord, button visibility, auto-defer. */
  defer_shortcut?: string;
  defer_button?: boolean;
  defer_on_send?: boolean;
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
 * Ask-each-read (approvals.py): a read tool call is blocked on the MCP thread
 * waiting for this answer. The call itself gives up its slot after
 * APPROVAL_GRACE_S (45s) and reports back that it did not run, but this chip
 * STAYS ANSWERABLE until `expires_at_ms`: an Allow inside that window is
 * remembered and the agent's next attempt at the same call proceeds. Rendering
 * it is therefore not optional - an unhandled event is a stalled agent.
 */
export interface ToolApprovalEvent {
  type: "tool_approval";
  id: string;
  tool: string;
  summary: string;
  /** Wall-clock ms after which answering does nothing (config
   *  `approval_timeout_minutes`). Absent when expiry is switched off. */
  expires_at_ms?: number;
}

/**
 * The approval was settled: by our own response (echoed back), by expiry
 * (`reason: "expired"`), or by session teardown. Idempotent - the chip may
 * already have marked itself resolved optimistically.
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
  | PreviewUpdateEvent
  | ReviewStateEvent
  | CardDeferredEvent
  | DeferredListEvent
  | ContextEvent
  | LearningEvent
  | AttachmentsEvent
  | LedgerEvent
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
