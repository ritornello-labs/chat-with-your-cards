/**
 * The external store: owns chat state outside React and maps the
 * ChatEvent stream (see events.ts, mirroring backends/base.py's
 * event_to_dict() plus proposals.py's Proposal.to_payload()) onto
 * assistant-ui's ThreadMessageLike shape for useExternalStoreRuntime.
 *
 * This is intentionally a plain class with subscribe/getSnapshot (the
 * classic external-store shape react's useSyncExternalStore expects), NOT a
 * React hook, because window.chatUI.dispatch (installed by bridge.ts) must
 * be callable the instant the script loads - before React has necessarily
 * mounted and subscribed. Events landing before mount just accumulate in
 * `messages`; the first render picks up the latest snapshot.
 *
 * Event -> ThreadMessageLike part mapping:
 *   text_delta          -> appended to a trailing {type:"text"} part
 *   thinking_delta       -> appended to a trailing {type:"reasoning"} part. Text is redacted upstream at
 *                           every reasoning effort level observed so far, so the part's `text` is kept at
 *                           THINKING_SENTINEL (a non-empty, invisible placeholder) whenever no real thinking
 *                           text has streamed - assistant-ui's fromThreadMessageLike DROPS any reasoning part
 *                           whose text is empty/whitespace-only when converting ThreadMessageLike ->
 *                           ThreadMessage (confirmed by reading @assistant-ui/core's shipped source, not the
 *                           public docs), which would otherwise make the part - and the running indicator it
 *                           carries - vanish outright while text is empty. estimatedTokens rides along as an
 *                           extra field on the part object (assistant-ui's MessageParts.js spreads the whole
 *                           part into the rendered component's props, so it survives untouched); see
 *                           ReasoningBlock.tsx for how the sentinel/estimatedTokens pair drives the UI.
 *   tool_call_started    -> a new {type:"tool-call", result: undefined} part (renders "running": assistant-ui
 *                           derives per-part status from the *message's* running status while result is unset)
 *   tool_call_finished   -> sets `result`/`isError` on the matching tool-call part (renders "complete" immediately,
 *                           regardless of the rest of the message, because assistant-ui treats any tool-call
 *                           part with a result as complete)
 *   proposal              -> a {type:"data", name:"proposal"} part (created once, updated in place on repeat
 *                           "proposal" events for the same id - e.g. change_set progress)
 *   proposal_resolved     -> merges status/note_id/warnings into that same data part
 *   proposal_error         -> attaches an errorMessage to that same data part
 *   usage                 -> NOT a message part; a side channel read by the footer (usage indicator)
 *   done                  -> current assistant message -> status complete; isRunning=false
 *   cancelled              -> current assistant message -> status incomplete/cancelled; isRunning=false
 *   error                  -> appends a {type:"data", name:"error"} part, then behaves like cancelled/error
 *   reset                  -> clears the transcript (Python-initiated, e.g. new chat / history load)
 *   setup_needed           -> NOT a message part; sets ui.setup (rendered as a card ONLY on an
 *                           empty thread - see Thread.tsx), read on-demand like ui.dock/ui.settings
 *   setup_resolved         -> clears ui.setup (the setup card's "Re-check" succeeded)
 *   (unknown type)          -> ignored, matching app.js dispatch()'s forward-compatible default case
 */

import type { ThreadMessageLike } from "@assistant-ui/react";
import { postCommand } from "./bridge";
import { dismissAllPopovers } from "./hooks/useDismiss";
import type {
  BridgeCommand,
  ChatEvent,
  ProposalErrorEvent,
  ProposalEvent,
  ProposalPayload,
  ProposalResolvedEvent,
  GradingEvent,
  GradingPayload,
  SetAsideEntryPayload,
  ToolCallFinishedEvent,
  ToolCallStartedEvent,
  UsageEvent,
} from "./events";

// ---- internal part/message shapes (structurally = ThreadMessageLike's) ----

interface TextPart {
  readonly type: "text";
  readonly text: string;
}

/**
 * A non-empty, effectively-invisible placeholder (U+200B, zero-width
 * space - NOT stripped by String.prototype.trim(), unlike ordinary
 * whitespace) used as a reasoning part's `text` while no real thinking text
 * has streamed. See the ChatStore doc comment above for why this exists:
 * assistant-ui drops any reasoning part whose text is empty after trim().
 * ReasoningBlock.tsx checks for this sentinel to distinguish "no real text
 * yet" from "real (possibly short) thinking text streamed in" - it is never
 * rendered to the user as visible content.
 */
export const THINKING_SENTINEL = "\u200B"; // zero-width space

interface ReasoningPart {
  readonly type: "reasoning";
  readonly text: string;
  readonly estimatedTokens: number | null;
}

export interface ToolCallResult {
  readonly ok: boolean;
  readonly summary?: string;
}

interface ToolCallPart {
  readonly type: "tool-call";
  readonly toolCallId: string;
  readonly toolName: string;
  readonly args: Record<string, unknown>;
  readonly argsText: string;
  readonly result?: ToolCallResult;
  readonly isError?: boolean;
}

/** The live ProposalPayload plus a client-only error message from proposal_error. */
export type ProposalCardData = ProposalPayload & {
  errorMessage?: string;
  /** The error is a refused revert that would discard a newer change, so the
   *  card may offer an explicit override (proposals.py's StaleRevert). */
  revertConflict?: boolean;
  /** From proposal_resolved: false = only an Anki backup can undo this. */
  revertible?: boolean;
};

interface ProposalDataPart {
  readonly type: "data";
  readonly name: "proposal";
  readonly data: ProposalCardData;
}

export type GradingCardData = GradingPayload;

interface GradingDataPart {
  readonly type: "data";
  readonly name: "grading";
  readonly data: GradingCardData;
}

interface ErrorDataPart {
  readonly type: "data";
  readonly name: "error";
  readonly data: { message: string };
}

interface ImageDataPart {
  readonly type: "data";
  readonly name: "image";
  readonly data: { src: string; caption: string };
}

/** Sandboxed widget (render_widget). `html` is UNTRUSTED agent output;
 *  only WidgetCard's opaque-origin iframe may render it. */
interface WidgetDataPart {
  readonly type: "data";
  readonly name: "widget";
  readonly data: { html: string; title: string };
}

/** Inline consent chip: render_widget was called while the feature is off.
 *  `resolved` is client-only ("Not now" dismissal / after enabling). */
interface WidgetOfferDataPart {
  readonly type: "data";
  readonly name: "widget_offer";
  readonly data: { id: string; title: string; resolved: boolean };
}

/** Ask-each-read approval chip. A pending one means a tool call is BLOCKED on
 *  the MCP thread. `late` marks an answer that arrived after the call gave up
 *  its slot, so the chip never implies work resumed when it did not. */
interface ToolApprovalDataPart {
  readonly type: "data";
  readonly name: "tool_approval";
  readonly data: {
    id: string;
    tool: string;
    summary: string;
    resolved: boolean;
    allow?: boolean;
    reason?: string;
    late?: boolean;
    /** Wall-clock ms after which Python stops honouring an answer. Absent =
     *  expiry disabled, and the chip then shows no deadline. */
    expiresAtMs?: number;
  };
}

type AssistantPart =
  | TextPart
  | ReasoningPart
  | ToolCallPart
  | ProposalDataPart
  | GradingDataPart
  | ErrorDataPart
  | ImageDataPart
  | WidgetDataPart
  | WidgetOfferDataPart
  | ToolApprovalDataPart;

type MessageStatus =
  | { type: "running" }
  | { type: "complete"; reason: "stop" | "unknown" }
  | { type: "incomplete"; reason: "cancelled" | "error"; error?: unknown };

interface StoreMessage {
  readonly id: string;
  readonly role: "user" | "assistant";
  readonly content: readonly AssistantPart[];
  readonly status?: MessageStatus;
  readonly createdAt: Date;
}

export interface UsageSnapshot {
  readonly costUsd: number | null;
  readonly inputTokens: number | null;
  readonly outputTokens: number | null;
  readonly cacheReadTokens: number | null;
  readonly cacheCreationTokens: number | null;
  // Real per-turn window from the CLI (modelUsage.contextWindow); when set the
  // footer prefers it over the hardcoded contextWindow.ts table. null on
  // scripted/older backends that don't report it.
  readonly contextWindow: number | null;
  // CLI's ground-truth "on"/"off": whether fast mode actually engaged.
  readonly fastState: string | null;
}

// ---- UI/control state pushed by Python (controller.py / __init__.py) ----

/**
 * Agent-tools axis: how much of the CLI's own shell/file toolset the agent
 * gets and how its calls are approved in our headless session (respawns to
 * change). Orthogonal to the collection-write `mode`. See backends/claude_cli
 * build_argv and ComposerControls' AGENT_TOOLS for the per-tier meaning.
 */
export type AgentTools = "sandbox" | "acceptEdits" | "auto" | "full";
export const AGENT_TOOLS_IDS: readonly AgentTools[] = ["sandbox", "acceptEdits", "auto", "full"];

export interface AgentState {
  readonly backend: string;
  readonly model: string; // "" | "fable" | "opus" | "sonnet" | "haiku"
  readonly effort: string; // "" | "low" | "medium" | "high" | "max"
  readonly mode: string; // permission mode (collection-write axis)
  readonly fast: boolean; // fast mode (Opus-only; requires a respawn to change)
  readonly tools: AgentTools; // agent-tools axis: shell/file access (respawns to change)
}

export interface NoteTypeMeta {
  readonly name: string;
  readonly fields: readonly string[];
}

/** The session ledger: what this chat has actually applied. */
export interface LedgerState {
  readonly sessionTag: string;
  readonly entries: readonly {
    readonly id: string;
    readonly kind: string;
    readonly label: string;
    readonly undone: boolean;
    readonly revertible: boolean;
  }[];
}

export interface CollectionMeta {
  readonly decks: readonly string[];
  readonly noteTypes: readonly NoteTypeMeta[];
  readonly tags: readonly string[];
}

export interface PinsState {
  readonly deck: string;
  readonly note_type: string;
  readonly tags: readonly string[];
  readonly fields: Readonly<Record<string, string>>;
}

export interface HistoryEntry {
  readonly id: string;
  readonly title: string;
  readonly updated_at: number;
  readonly events: number;
}

export interface DoctorRow {
  readonly label: string;
  readonly status: string;
  readonly detail: string;
  readonly link?: string;
}

/**
 * Dock shell state (mirrors events.ts DockStateEvent). `null` until the host
 * reports it: in Anki that is window.CWYC_INITIAL_DOCK (read at mount, set by
 * dock.py's body fragment so the first paint is already correct) followed by
 * live dock_state pushes; in dev the replayer pushes one on ready. While
 * null the App renders the quiet boot state (plain warm background, no
 * chrome) instead of guessing wrong and flashing.
 */
export interface DockUiState {
  readonly expanded: boolean;
  readonly animating: boolean;
  readonly width: number;
  readonly side: "left" | "right";
}

/** The selectable colour palettes (styles.css cwyc-theme-* blocks). */
export type ThemeName = "teal" | "indigo" | "evergreen";
export const THEME_NAMES: readonly ThemeName[] = ["teal", "indigo", "evergreen"];

/**
 * Apply the palette by swapping the `cwyc-theme-<name>` class on <html>. Kept
 * in one place so both the live settings push and the boot path use identical
 * logic. dock.py plants the same class in the first paint so there is no flash.
 */
export function applyThemeClass(theme: ThemeName): void {
  if (typeof document === "undefined") return;
  const root = document.documentElement;
  for (const t of THEME_NAMES) root.classList.toggle(`cwyc-theme-${t}`, t === theme);
}

/** User-facing settings snapshot (events.ts SettingsEvent); null = not pushed yet. */
export interface SettingsState {
  readonly restoreLastChat: boolean;
  readonly dockSide: "left" | "right";
  readonly toggleShortcut: string;
  readonly newChatShortcut: string;
  readonly vimMode: boolean;
  readonly vimMappings: readonly [string, string, string][];
  readonly theme: ThemeName;
  readonly mcpInheritUser: boolean;
  readonly widgetRendering: boolean;
  readonly deferShortcut: string;
  readonly deferButton: boolean;
  readonly deferOnSend: boolean;
}

/**
 * First-run onboarding (task #19, events.ts SetupNeededEvent): non-null while
 * the setup card should be offered. Persists across `reset()` (new chat /
 * history load) like the rest of `ui.*` chrome - it reflects a standing
 * environment fact (Claude Code isn't found), not per-chat state, so the
 * card correctly reappears on the next empty thread rather than needing
 * Python to re-push it.
 */
export interface SetupState {
  readonly platform: string; // "darwin" | "linux" | "windows"
}

/** One set-aside card as the tray renders it (events.ts SetAsideEntryPayload). */
export interface SetAsideEntry {
  readonly cardId: number;
  readonly deck: string;
  readonly front: string;
  readonly back: string;
}

export interface UiState {
  readonly agent: AgentState;
  readonly openTarget: "terminal" | "desktop";
  readonly meta: CollectionMeta;
  readonly ledger: LedgerState;
  readonly pins: PinsState;
  readonly history: readonly HistoryEntry[] | null; // null = never fetched
  readonly doctor: readonly DoctorRow[] | null; // null = never run
  readonly notice: { text: string; seq: number } | null;
  readonly dock: DockUiState | null; // null = host has not reported yet
  readonly settings: SettingsState | null; // null = not pushed yet
  readonly setup: SetupState | null; // null = no onboarding needed (or resolved)
  /** Which pending proposal the keyboard acts on. A shortcut that accepts a
   *  card must be visibly aimed at one, or Cmd+Enter is a blind write. */
  readonly activeProposalId: string | null;
  /** Whether a review card is on screen (drives the "Set aside" button). */
  readonly reviewing: boolean;
  /** The just-deferred card behind the transient Undo chip; seq forces the
   *  chip (and its auto-hide timer) to restart on every defer. */
  readonly deferred: { cardId: number; seq: number } | null;
  /** Which full-pane view the dock shows (task #33): the chat thread, or the
   *  set-aside tray. Chrome state - survives reset() like the rest of ui.*. */
  readonly pane: "chat" | "aside";
  /** Today's set-aside cards, newest first (deferred_list pushes). */
  readonly setAside: readonly SetAsideEntry[];
  /** Badge count. Usually setAside.length, but review_state can update it
   *  alone (cheap count without re-rendering every card summary). */
  readonly setAsideCount: number;
}

export const PERMISSION_MODES: readonly { id: string; label: string; hint: string }[] = [
  { id: "ask-each-read", label: "Ask each read", hint: "Every tool call needs your OK" },
  { id: "read-only", label: "Read-only", hint: "No write tools offered at all" },
  { id: "default", label: "Propose", hint: "Reads free; writes as review cards" },
  { id: "auto-accept", label: "Auto-accept", hint: "New notes and grading apply instantly (capped)" },
  { id: "trusted-writes", label: "Trusted writes", hint: "Writes apply under a session budget" },
];

const EMPTY_PINS: PinsState = { deck: "", note_type: "", tags: [], fields: {} };

const DEFAULT_UI_STATE: UiState = {
  agent: { backend: "auto", model: "", effort: "", mode: "default", fast: false, tools: "sandbox" },
  openTarget: "terminal",
  meta: { decks: [], noteTypes: [], tags: [] },
  ledger: { sessionTag: "", entries: [] },
  pins: EMPTY_PINS,
  history: null,
  doctor: null,
  notice: null,
  dock: null,
  settings: null,
  setup: null,
  activeProposalId: null,
  reviewing: false,
  deferred: null,
  pane: "chat",
  setAside: [],
  setAsideCount: 0,
};

export interface ChatState {
  readonly messages: readonly ThreadMessageLike[];
  readonly isRunning: boolean;
  readonly hasUnread: boolean; // a reply finished while the dock was collapsed, not yet expanded
  readonly usage: UsageSnapshot | null;
  readonly ui: UiState;
}

export type Listener = () => void;

let idCounter = 0;
function nextId(prefix: string): string {
  idCounter += 1;
  return `${prefix}-${idCounter}`;
}

function safeParseArgs(summary: string | undefined): Record<string, unknown> {
  if (!summary) return {};
  try {
    const parsed: unknown = JSON.parse(summary);
    return parsed && typeof parsed === "object" ? (parsed as Record<string, unknown>) : {};
  } catch {
    return {};
  }
}

function toThreadMessageLike(msg: StoreMessage): ThreadMessageLike {
  return {
    id: msg.id,
    role: msg.role,
    content: msg.content,
    createdAt: msg.createdAt,
    ...(msg.role === "assistant" ? { status: msg.status } : {}),
  } as ThreadMessageLike;
}

export class ChatStore {
  private messages: StoreMessage[] = [];
  private isRunning = false;
  private hasUnread = false;
  private usage: UsageSnapshot | null = null;
  private ui: UiState = DEFAULT_UI_STATE;
  private noticeSeq = 0;
  private currentAssistantId: string | null = null;
  private readonly listeners = new Set<Listener>();
  private snapshot: ChatState;

  constructor() {
    this.snapshot = this.computeSnapshot();
  }

  subscribe = (listener: Listener): (() => void) => {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  };

  getSnapshot = (): ChatState => this.snapshot;

  private computeSnapshot(): ChatState {
    return {
      messages: this.messages.map(toThreadMessageLike),
      isRunning: this.isRunning,
      hasUnread: this.hasUnread,
      usage: this.usage,
      ui: this.ui,
    };
  }

  private emit(): void {
    this.snapshot = this.computeSnapshot();
    for (const listener of this.listeners) listener();
  }

  // ---- outbound: UI -> Python (postCommand mirrors app.js's post()) ----

  sendUserMessage(text: string): void {
    if (this.isRunning || !text.trim()) return;
    // The message IS the revision request, so the proposal it replaces stops
    // being the live offer exactly now - not when the button was clicked.
    if (this.pendingSupersedeId) {
      postCommand({ type: "proposal_supersede", id: this.pendingSupersedeId });
      this.pendingSupersedeId = null;
    }
    const userMsg: StoreMessage = {
      id: nextId("u"),
      role: "user",
      content: [{ type: "text", text }],
      createdAt: new Date(),
    };
    const assistantMsg: StoreMessage = {
      id: nextId("a"),
      role: "assistant",
      content: [],
      status: { type: "running" },
      createdAt: new Date(),
    };
    this.messages = [...this.messages, userMsg, assistantMsg];
    this.currentAssistantId = assistantMsg.id;
    this.isRunning = true;
    this.emit();
    postCommand({ type: "send", text });
  }

  cancel(): void {
    postCommand({ type: "cancel" });
  }

  newChat(): void {
    this.reset();
    postCommand({ type: "new_chat" });
  }

  acceptProposal(id: string, fields: Record<string, string>, kind: string): void {
    const cmd: BridgeCommand = { type: "proposal_accept", id, fields };
    if (kind === "edit") {
      cmd.accepted_fields = Object.keys(fields);
    }
    postCommand(cmd);
  }

  /**
   * `overrides` carries the destination the user chose on the card itself -
   * deck and tags, which _accept_create already honours over the proposal's
   * own values. Omitted keys leave the proposal's choice alone; sending an
   * empty tag list is meaningful (strip them all) and is preserved as such.
   */
  acceptProposalRevision(
    id: string,
    revision: number,
    overrides?: { deck?: string; tags?: readonly string[] }
  ): void {
    const cmd: BridgeCommand = { type: "proposal_accept", id, revision };
    if (overrides?.deck) cmd.deck = overrides.deck;
    if (overrides?.tags) cmd.tags = [...overrides.tags];
    postCommand(cmd);
  }

  reviseProposal(id: string, expectedRevision: number, fields: Record<string, string>): void {
    postCommand({ type: "proposal_revise", id, expected_revision: expectedRevision, fields });
  }

  /**
   * Ask Python to re-render the card preview from an in-progress draft
   * (proposals.py preview_request -> `preview_update`). Fire-and-forget and
   * safe to spam - the caller debounces; a stale answer only ever repaints a
   * preview, never the proposal itself.
   */
  previewProposal(id: string, fields: Record<string, string>): void {
    postCommand({ type: "proposal_preview", id, fields });
  }

  rejectProposal(id: string): void {
    postCommand({ type: "proposal_reject", id });
  }

  /** Undo an APPLIED change. `force` overwrites a later edit that revert would
   *  otherwise refuse to discard (proposals.py's stale-compensation guard). */
  revertProposal(id: string, force = false): void {
    postCommand(force ? { type: "proposal_revert", id, force } : { type: "proposal_revert", id });
  }

  /** Re-apply a change that was undone - one click back from a mis-click. */
  readdProposal(id: string): void {
    postCommand({ type: "proposal_readd", id });
  }

  /** Put a rejected or superseded proposal back up for review. */
  restoreProposal(id: string): void {
    postCommand({ type: "proposal_restore", id });
  }

  /**
   * The next message the user sends is a request for a DIFFERENT proposal, so
   * set the current one aside when it goes.
   *
   * The classic UI's "Suggest change" did exactly this and the React port kept
   * only the button's CSS class (reused for the in-card Edit toggle), leaving
   * proposal_supersede orphaned in __init__.py - so a proposal you discussed
   * and moved on from stayed `pending` forever (task #19).
   *
   * Deliberately at SEND, not at click: until you actually ask for something
   * else, the old proposal is still the live offer and must stay acceptable.
   */
  markForSupersede(id: string): void {
    this.pendingSupersedeId = id;
  }

  /** Drop a refused-revert error once the user has decided to keep the newer
   *  change, so the card stops offering an override it no longer needs. */
  dismissProposalError(id: string): void {
    const messageId = this.findProposalMessageId(id);
    if (!messageId) return;
    this.updateMessage(messageId, (msg) => ({
      ...msg,
      content: msg.content.map((part) =>
        part.type === "data" && part.name === "proposal" && part.data.id === id
          ? { ...part, data: { ...part.data, errorMessage: undefined, revertConflict: undefined } }
          : part
      ),
    }));
    this.emit();
  }

  /** The composer's "Set aside" button: defer the card on screen (#32). */
  deferCurrentCard(): void {
    postCommand({ type: "defer_current" });
  }

  /** The transient Undo chip: unmark and put the card straight back. */
  undoDefer(cardId: number): void {
    postCommand({ type: "undo_defer", card_id: cardId });
    this.dismissDeferredNotice();
  }

  dismissDeferredNotice(): void {
    if (this.ui.deferred === null) return;
    this.ui = { ...this.ui, deferred: null };
    this.emit();
  }

  // ---- the set-aside tray (task #33) ----

  /** Flip the dock to the tray and ask Python for the fresh list (the badge
   *  count may be ahead of the entries we hold). */
  openSetAside(): void {
    if (this.ui.pane !== "aside") {
      this.ui = { ...this.ui, pane: "aside" };
      this.emit();
    }
    postCommand({ type: "get_deferred" });
  }

  closeSetAside(): void {
    if (this.ui.pane === "aside") {
      this.ui = { ...this.ui, pane: "chat" };
      this.emit();
    }
  }

  toggleSetAside(): void {
    if (this.ui.pane === "aside") this.closeSetAside();
    else this.openSetAside();
  }

  /** Tray per-card action: unbury AND pin as the next card (same verb as the
   *  transient Undo chip - "I want to review this one now"). */
  bringBackCard(cardId: number): void {
    postCommand({ type: "undo_defer", card_id: cardId });
  }

  /** Tray bulk action: everything returns to the queue, no pin. */
  bringAllBack(): void {
    postCommand({ type: "unbury_all_deferred" });
  }

  /** Aim the keyboard at a specific pending proposal (task #21). */
  setActiveProposal(id: string | null): void {
    if (this.ui.activeProposalId === id) return;
    this.ui = { ...this.ui, activeProposalId: id };
    this.emit();
  }

  /** Every pending proposal, in transcript order — what the keyboard cycles. */
  pendingProposalIds(): string[] {
    const ids: string[] = [];
    for (const msg of this.messages) {
      if (msg.role !== "assistant") continue;
      for (const part of msg.content) {
        if (part.type === "data" && part.name === "proposal" && part.data.status === "pending") {
          ids.push(part.data.id);
        }
      }
    }
    return ids;
  }

  undoSession(): void {
    postCommand({ type: "undo_session" });
  }

  openSessionBrowser(): void {
    postCommand({ type: "open_session_browser" });
  }

  acceptGrading(id: string): void {
    postCommand({ type: "grading_accept", id });
  }

  rejectGrading(id: string): void {
    postCommand({ type: "grading_reject", id });
  }

  makeGradingCardsAvailable(id: string): void {
    postCommand({ type: "grading_make_available", id });
  }

  // ---- outbound: control-surface commands (header + composer row) ----

  setAgent(model: string, effort: string, fast?: boolean, tools?: AgentTools): void {
    // Optimistic: Python re-pushes the authoritative "agent" state after.
    // fast and tools default to the current value so callers that only mean to
    // change one axis (the Model/Effort/Fast/Agent-tools menu sections) don't
    // clobber the others.
    const nextFast = fast === undefined ? this.ui.agent.fast : fast;
    let nextTools = tools === undefined ? this.ui.agent.tools : tools;
    const modelChanged = model !== this.ui.agent.model;
    // Auto's classifier only runs on premium models; the CLI silently drops
    // `--permission-mode auto` to a no-classifier mode on Haiku. Auto was chosen
    // FOR that safety net, so honour the intent by falling back to the safe end
    // (sandbox) rather than leaving a phantom "Auto" that quietly grants
    // unguarded tools. Surface it so the tier change isn't a mystery.
    if (nextTools === "auto" && model === "haiku") {
      nextTools = "sandbox";
      this.noticeSeq += 1;
      this.ui = {
        ...this.ui,
        notice: {
          text: "Auto tools need Opus or Sonnet — agent tools set to Sandbox on Haiku.",
          seq: this.noticeSeq,
        },
      };
    }
    this.ui = {
      ...this.ui,
      agent: { ...this.ui.agent, model, effort, fast: nextFast, tools: nextTools },
    };
    // The CLI's reported context window (usage.contextWindow) is per-model and
    // only refreshes on the next real turn. On a model switch it is stale - a
    // haiku turn's 200k would keep showing under an "Opus" label until the next
    // message. Drop it so UsageFooter falls back to the per-model table
    // (contextWindow.ts) for the newly selected model right away.
    if (modelChanged && this.usage) {
      this.usage = { ...this.usage, contextWindow: null };
    }
    this.emit();
    postCommand({ type: "set_agent", model, effort, fast: nextFast, tools: nextTools });
  }

  setAgentTools(tools: AgentTools): void {
    // Convenience wrapper for the "Agent tools" menu section: change only the
    // environment-access axis, keeping model/effort/fast as-is.
    this.setAgent(this.ui.agent.model, this.ui.agent.effort, this.ui.agent.fast, tools);
  }

  setPermissionMode(mode: string): void {
    this.ui = { ...this.ui, agent: { ...this.ui.agent, mode } };
    this.emit();
    postCommand({ type: "set_permission_mode", mode });
  }

  cyclePermissionMode(): void {
    const ids = PERMISSION_MODES.map((m) => m.id);
    const index = ids.indexOf(this.ui.agent.mode);
    this.setPermissionMode(ids[(index + 1) % ids.length]);
  }

  setPins(pins: PinsState): void {
    this.ui = { ...this.ui, pins };
    this.emit();
    postCommand({ type: "set_pins", pins });
  }

  openInClaude(): void {
    postCommand({ type: "open_in_claude", target: this.ui.openTarget });
  }

  setOpenTarget(target: "terminal" | "desktop"): void {
    this.ui = { ...this.ui, openTarget: target };
    this.emit();
    postCommand({ type: "set_open_in_claude_target", target });
  }

  requestHistory(): void {
    postCommand({ type: "list_history" });
  }

  loadHistory(id: string): void {
    postCommand({ type: "load_history", id });
  }

  runDoctor(): void {
    postCommand({ type: "run_doctor" });
  }

  /** The setup card's "Re-check" button (task #19): asks Python to re-run
   *  CLI discovery with no Anki restart. Not optimistic - the card stays up
   *  until a "setup_resolved" push (success) or a fresh "notice" (still
   *  missing) comes back. */
  recheckBackend(): void {
    postCommand({ type: "recheck_backend" });
  }

  /**
   * Expand/collapse the dock shell. NOT optimistic on purpose: the crossfade
   * must be driven by the host's dock_state pushes (which arrive at width-
   * animation start/end) so the webview resize and the fade stay in step.
   */
  setDockExpanded(expanded: boolean): void {
    postCommand({ type: "set_dock_expanded", expanded });
  }

  /** Persist one whitelisted setting; Python re-pushes "settings" after. */
  setSetting(key: string, value: unknown): void {
    postCommand({ type: "set_setting", key, value });
  }

  /** Open Anki's add-on config (vim_mappings and other advanced options). */
  openAddonConfig(): void {
    postCommand({ type: "open_addon_config" });
  }

  // ---- inbound: Python -> UI ----

  /**
   * Accepts `unknown`, not ChatEvent: this is the far side of
   * window.chatUI.dispatch(payload), which Python (or the dev replayer)
   * calls with arbitrary JSON. Anything that is not a `{type: string, ...}`
   * object is dropped rather than throwing, matching app.js's dispatch()'s
   * forward-compatible default case for unrecognized shapes too.
   */
  dispatch = (payload: unknown): void => {
    if (!payload || typeof payload !== "object" || typeof (payload as { type?: unknown }).type !== "string") {
      return;
    }
    const event = payload as ChatEvent;
    switch (event.type) {
      case "text_delta":
        this.appendText("text", String((event as { text: string }).text ?? ""));
        break;
      case "thinking_delta": {
        const thinking = event as { text?: string; estimated_tokens?: number | null };
        this.appendThinking(String(thinking.text ?? ""), thinking.estimated_tokens ?? null);
        break;
      }
      case "tool_call_started":
        this.startToolCall(event as ToolCallStartedEvent);
        break;
      case "tool_call_finished":
        this.finishToolCall(event as ToolCallFinishedEvent);
        break;
      case "proposal":
        this.upsertProposal((event as ProposalEvent).proposal);
        break;
      case "proposal_resolved":
        this.resolveProposal(event as ProposalResolvedEvent);
        break;
      case "proposal_error":
        this.errorProposal(event as ProposalErrorEvent);
        break;
      case "ledger": {
        const ev = event as { session_tag?: string; entries?: unknown[] };
        this.ui = {
          ...this.ui,
          ledger: {
            sessionTag: String(ev.session_tag ?? ""),
            entries: (Array.isArray(ev.entries) ? ev.entries : []).map((raw) => {
              const e = raw as Record<string, unknown>;
              return {
                id: String(e.id ?? ""),
                kind: String(e.kind ?? ""),
                label: String(e.label ?? ""),
                undone: !!e.undone,
                revertible: e.revertible !== false,
              };
            }),
          },
        };
        this.emit();
        break;
      }
      case "preview_update":
        this.updateProposalPreview(event as { id?: string; previews?: unknown });
        break;
      case "review_state": {
        const review = event as { reviewing?: boolean; set_aside_count?: number };
        const count =
          typeof review.set_aside_count === "number" && review.set_aside_count >= 0
            ? review.set_aside_count
            : this.ui.setAsideCount;
        if (this.ui.reviewing !== !!review.reviewing || this.ui.setAsideCount !== count) {
          this.ui = { ...this.ui, reviewing: !!review.reviewing, setAsideCount: count };
          this.emit();
        }
        break;
      }
      case "deferred_list": {
        const raw = (event as { entries?: unknown[] }).entries;
        const entries: SetAsideEntry[] = (Array.isArray(raw) ? raw : []).map((item) => {
          const e = item as Partial<SetAsideEntryPayload>;
          return {
            cardId: Number(e.card_id ?? 0),
            deck: String(e.deck ?? ""),
            front: String(e.front ?? ""),
            back: String(e.back ?? ""),
          };
        });
        this.ui = { ...this.ui, setAside: entries, setAsideCount: entries.length };
        this.emit();
        break;
      }
      case "card_deferred": {
        const deferred = event as { card_id?: number };
        if (deferred.card_id) {
          this.ui = {
            ...this.ui,
            deferred: { cardId: Number(deferred.card_id), seq: (this.ui.deferred?.seq ?? 0) + 1 },
          };
          this.emit();
        }
        break;
      }
      case "grading":
        this.upsertGrading((event as GradingEvent).grading);
        break;
      case "inline_image":
        this.appendImage(event as { src: string; caption?: string });
        break;
      case "inline_widget":
        this.appendWidget(event as { html?: string; title?: string });
        break;
      case "widget_offer":
        this.appendWidgetOffer(event as { title?: string });
        break;
      case "tool_approval":
        this.appendToolApproval(event as { id?: string; tool?: string; summary?: string });
        break;
      case "tool_approval_resolved":
        this.markToolApprovalResolved(
          event as { id?: string; allow?: boolean; reason?: string }
        );
        break;
      case "usage":
        this.setUsage(event as UsageEvent);
        break;
      case "done":
        this.finishTurn({ type: "complete", reason: "stop" });
        this.flushPendingAutoSend();
        break;
      case "cancelled":
        this.finishTurn({ type: "incomplete", reason: "cancelled" });
        this.flushPendingAutoSend();
        break;
      case "error":
        this.appendError(String((event as { message?: string }).message ?? "Unknown error"));
        this.finishTurn({ type: "incomplete", reason: "error" });
        this.flushPendingAutoSend();
        break;
      case "reset":
        this.pendingAutoSend = null; // never leak a queued nudge into a new chat
        this.reset();
        break;
      case "history_load":
        this.replayHistory(((event as { events?: unknown[] }).events ?? []) as unknown[]);
        break;
      case "agent": {
        const agent = event as {
          backend?: string;
          model?: string;
          effort?: string;
          mode?: string;
          fast?: boolean;
          tools?: string;
        };
        this.ui = {
          ...this.ui,
          agent: {
            backend: String(agent.backend ?? this.ui.agent.backend),
            model: String(agent.model ?? ""),
            effort: String(agent.effort ?? ""),
            mode: String(agent.mode ?? "default"),
            fast: Boolean(agent.fast),
            tools: AGENT_TOOLS_IDS.includes(agent.tools as AgentTools)
              ? (agent.tools as AgentTools)
              : "sandbox",
          },
        };
        this.emit();
        break;
      }
      case "ui_config": {
        const cfg = event as { open_in_claude_target?: string };
        const target = cfg.open_in_claude_target === "desktop" ? "desktop" : "terminal";
        this.ui = { ...this.ui, openTarget: target };
        this.emit();
        break;
      }
      case "collection_meta": {
        const meta = event as { decks?: string[]; note_types?: NoteTypeMeta[]; tags?: string[] };
        this.ui = {
          ...this.ui,
          meta: {
            decks: meta.decks ?? [],
            noteTypes: meta.note_types ?? [],
            tags: meta.tags ?? [],
          },
        };
        this.emit();
        break;
      }
      case "pins": {
        const pins = (event as { pins?: Partial<PinsState> }).pins ?? {};
        this.ui = {
          ...this.ui,
          pins: {
            deck: String(pins.deck ?? ""),
            note_type: String(pins.note_type ?? ""),
            tags: (pins.tags as string[]) ?? [],
            fields: (pins.fields as Record<string, string>) ?? {},
          },
        };
        this.emit();
        break;
      }
      case "history": {
        const sessions = ((event as { sessions?: unknown[] }).sessions ?? []) as HistoryEntry[];
        this.ui = { ...this.ui, history: sessions };
        this.emit();
        break;
      }
      case "doctor": {
        const results = ((event as { results?: unknown[] }).results ?? []) as DoctorRow[];
        this.ui = { ...this.ui, doctor: results };
        this.emit();
        break;
      }
      case "dock_state": {
        const dock = event as {
          expanded?: boolean;
          animating?: boolean;
          width?: number;
          side?: string;
        };
        const wasExpanded = this.ui.dock?.expanded ?? true;
        const nowExpanded = Boolean(dock.expanded);
        // Expanding is how a reply gets read: the unread ember goes out.
        if (nowExpanded) this.hasUnread = false;
        this.ui = {
          ...this.ui,
          dock: {
            expanded: nowExpanded,
            animating: Boolean(dock.animating),
            width: typeof dock.width === "number" && dock.width > 0 ? dock.width : 420,
            side: dock.side === "left" ? "left" : "right",
          },
        };
        this.emit();
        // Collapsing must take every open popover with it - otherwise a menu
        // (e.g. Settings) survives the collapse and reappears on re-expand
        // (dogfood 2026-07-14). Fire on the expanded->collapsed edge only.
        if (wasExpanded && !nowExpanded) dismissAllPopovers();
        break;
      }
      case "settings": {
        const s = event as {
          restore_last_chat?: boolean;
          dock_side?: string;
          toggle_shortcut?: string;
          new_chat_shortcut?: string;
          vim_mode?: boolean;
          vim_mappings?: unknown;
          theme?: string;
          mcp_inherit_user?: boolean;
          widget_rendering?: boolean;
          defer_shortcut?: string;
          defer_button?: boolean;
          defer_on_send?: boolean;
        };
        const rawMappings = Array.isArray(s.vim_mappings) ? s.vim_mappings : [];
        const vimMappings = rawMappings.filter(
          (m): m is [string, string, string] =>
            Array.isArray(m) && m.length === 3 && m.every((part) => typeof part === "string")
        );
        const theme = THEME_NAMES.includes(s.theme as ThemeName) ? (s.theme as ThemeName) : "teal";
        this.ui = {
          ...this.ui,
          settings: {
            restoreLastChat: Boolean(s.restore_last_chat),
            dockSide: s.dock_side === "left" ? "left" : "right",
            toggleShortcut: String(s.toggle_shortcut ?? ""),
            newChatShortcut: String(s.new_chat_shortcut ?? ""),
            vimMode: Boolean(s.vim_mode),
            vimMappings,
            theme,
            mcpInheritUser: Boolean(s.mcp_inherit_user),
            widgetRendering: Boolean(s.widget_rendering),
            deferShortcut: String(s.defer_shortcut ?? ""),
            deferButton: s.defer_button !== false,
            deferOnSend: Boolean(s.defer_on_send),
          },
        };
        applyThemeClass(theme);
        this.emit();
        break;
      }
      case "notice": {
        this.noticeSeq += 1;
        this.ui = {
          ...this.ui,
          notice: { text: String((event as { text?: string }).text ?? ""), seq: this.noticeSeq },
        };
        this.emit();
        break;
      }
      case "setup_needed": {
        const platform = String((event as { platform?: string }).platform ?? "linux");
        this.ui = { ...this.ui, setup: { platform } };
        this.emit();
        break;
      }
      case "setup_resolved": {
        this.ui = { ...this.ui, setup: null };
        this.emit();
        break;
      }
      default:
        // Unknown event types are ignored on purpose (forward-compatible),
        // matching app.js's dispatch() default case.
        break;
    }
  };

  private ensureCurrentAssistant(): StoreMessage {
    let msg = this.currentAssistantId
      ? this.messages.find((m) => m.id === this.currentAssistantId)
      : undefined;
    if (!msg) {
      // Defensive fallback for content arriving with no open turn (e.g. a
      // "proposal" push unrelated to any "send" - restores, change_set
      // updates). Only *inherit* running state, never fabricate it: this
      // must not flip the composer to "Stop" for a turn that was never
      // started, since nothing would ever arrive to close it back out.
      msg = {
        id: nextId("a"),
        role: "assistant",
        content: [],
        status: this.isRunning ? { type: "running" } : { type: "complete", reason: "unknown" },
        createdAt: new Date(),
      };
      this.messages = [...this.messages, msg];
      this.currentAssistantId = msg.id;
    }
    return msg;
  }

  private updateMessage(id: string, updater: (msg: StoreMessage) => StoreMessage): void {
    this.messages = this.messages.map((m) => (m.id === id ? updater(m) : m));
  }

  private appendText(kind: "text", delta: string): void {
    const current = this.ensureCurrentAssistant();
    this.updateMessage(current.id, (msg) => {
      const parts = msg.content.slice();
      const last = parts[parts.length - 1];
      if (last && last.type === kind) {
        parts[parts.length - 1] = { type: kind, text: last.text + delta };
      } else {
        parts.push({ type: kind, text: delta });
      }
      return { ...msg, content: parts };
    });
    this.emit();
  }

  /**
   * thinking_delta handling: appends to a trailing reasoning part if one is
   * already open (same rule as appendText - a tool call or other part type
   * interposed since the last reasoning delta starts a fresh part instead),
   * but keeps `text` at THINKING_SENTINEL whenever no real thinking text has
   * accumulated, so assistant-ui never drops the part outright (see the
   * THINKING_SENTINEL doc comment above). estimatedTokens is sticky: a
   * delta with no estimate (e.g. every content_block_start) keeps whatever
   * the part already had rather than resetting it to null.
   */
  private appendThinking(delta: string, estimatedTokens: number | null): void {
    const current = this.ensureCurrentAssistant();
    this.updateMessage(current.id, (msg) => {
      const parts = msg.content.slice();
      const last = parts[parts.length - 1];
      if (last && last.type === "reasoning") {
        const prevText = last.text === THINKING_SENTINEL ? "" : last.text;
        const nextText = prevText + delta;
        parts[parts.length - 1] = {
          type: "reasoning",
          text: nextText.length > 0 ? nextText : THINKING_SENTINEL,
          estimatedTokens: estimatedTokens ?? last.estimatedTokens,
        };
      } else {
        parts.push({
          type: "reasoning",
          text: delta.length > 0 ? delta : THINKING_SENTINEL,
          estimatedTokens,
        });
      }
      return { ...msg, content: parts };
    });
    this.emit();
  }

  private startToolCall(event: ToolCallStartedEvent): void {
    const current = this.ensureCurrentAssistant();
    const part: ToolCallPart = {
      type: "tool-call",
      toolCallId: event.call_id,
      toolName: event.tool,
      args: safeParseArgs(event.summary),
      argsText: event.summary ?? "",
    };
    this.updateMessage(current.id, (msg) => ({ ...msg, content: [...msg.content, part] }));
    this.emit();
  }

  private finishToolCall(event: ToolCallFinishedEvent): void {
    this.messages = this.messages.map((msg) => {
      if (msg.role !== "assistant") return msg;
      let touched = false;
      const content = msg.content.map((part) => {
        if (part.type === "tool-call" && part.toolCallId === event.call_id) {
          touched = true;
          const updated: ToolCallPart = {
            ...part,
            result: { ok: event.ok, summary: event.summary },
            isError: !event.ok,
          };
          return updated;
        }
        return part;
      });
      return touched ? { ...msg, content } : msg;
    });
    this.emit();
  }

  private findProposalMessageId(id: string): string | null {
    for (const msg of this.messages) {
      if (msg.role !== "assistant") continue;
      for (const part of msg.content) {
        if (part.type === "data" && part.name === "proposal" && part.data.id === id) {
          return msg.id;
        }
      }
    }
    return null;
  }

  private findGradingMessageId(id: string): string | null {
    for (const msg of this.messages) {
      if (msg.role !== "assistant") continue;
      for (const part of msg.content) {
        if (part.type === "data" && part.name === "grading" && part.data.id === id) {
          return msg.id;
        }
      }
    }
    return null;
  }

  private appendImage(event: { src: string; caption?: string }): void {
    if (!event.src) return;
    const current = this.ensureCurrentAssistant();
    const part: ImageDataPart = {
      type: "data",
      name: "image",
      data: { src: event.src, caption: String(event.caption ?? "") },
    };
    this.updateMessage(current.id, (msg) => ({ ...msg, content: [...msg.content, part] }));
    this.emit();
  }

  private appendWidget(event: { html?: string; title?: string }): void {
    if (!event.html) return;
    const current = this.ensureCurrentAssistant();
    const part: WidgetDataPart = {
      type: "data",
      name: "widget",
      data: { html: String(event.html), title: String(event.title ?? "") },
    };
    this.updateMessage(current.id, (msg) => ({ ...msg, content: [...msg.content, part] }));
    this.emit();
  }

  private appendWidgetOffer(event: { title?: string }): void {
    const current = this.ensureCurrentAssistant();
    const part: WidgetOfferDataPart = {
      type: "data",
      name: "widget_offer",
      data: { id: nextId("wo"), title: String(event.title ?? ""), resolved: false },
    };
    this.updateMessage(current.id, (msg) => ({ ...msg, content: [...msg.content, part] }));
    this.emit();
  }

  /**
   * Ask-each-read: a tool call is blocked on the MCP thread until this is
   * answered. The id comes from Python (approvals.py's broker owns it) - never
   * mint one here, it is the handle the waiting thread is keyed on.
   */
  private appendToolApproval(event: {
    id?: string;
    tool?: string;
    summary?: string;
    expires_at_ms?: number;
  }): void {
    const id = String(event.id ?? "");
    if (!id) return; // unanswerable: no handle to respond with
    const current = this.ensureCurrentAssistant();
    const expiresAtMs = Number(event.expires_at_ms);
    const part: ToolApprovalDataPart = {
      type: "data",
      name: "tool_approval",
      data: {
        id,
        tool: String(event.tool ?? ""),
        summary: String(event.summary ?? ""),
        resolved: false,
        expiresAtMs: Number.isFinite(expiresAtMs) && expiresAtMs > 0 ? expiresAtMs : undefined,
      },
    };
    this.updateMessage(current.id, (msg) => ({ ...msg, content: [...msg.content, part] }));
    this.emit();
  }

  /** Settled by us (echo), by expiry (`approval_timeout_minutes`), or by
   *  session teardown.
   *  Idempotent: respondToolApproval already marked it optimistically. */
  private markToolApprovalResolved(event: {
    id?: string;
    allow?: boolean;
    reason?: string;
    late?: boolean;
  }): void {
    const tool = this.setToolApprovalResolved(String(event.id ?? ""), {
      allow: !!event.allow,
      reason: event.reason ? String(event.reason) : undefined,
      late: !!event.late,
    });
    // A LATE approval cannot resume the call that gave up, so without this the
    // user clicks Allow and nothing happens while the agent's last message
    // still reads "approve it and I'll continue" - contradicting the chip
    // beside it (dogfood 2026-07-23). Nudge the agent the same way the widget
    // offer does: a visible user message is one of the only channels that
    // reliably reaches a mid-conversation CLI session. Approving IS the
    // instruction to proceed, so this needs no second click.
    if (tool && event.late && event.allow) {
      const note = `(I approved the ${tool} request - go ahead.)`;
      if (this.isRunning) this.pendingAutoSend = note;
      else this.sendUserMessage(note);
    }
  }

  /** Returns the resolved chip's tool name, or "" if no chip matched. */
  private setToolApprovalResolved(
    approvalId: string,
    outcome: { allow: boolean; reason?: string; late?: boolean }
  ): string {
    if (!approvalId) return "";
    let tool = "";
    let touched = false;
    this.messages = this.messages.map((msg) => {
      if (msg.role !== "assistant") return msg;
      let hit = false;
      const content = msg.content.map((part) => {
        if (part.type !== "data" || part.name !== "tool_approval" || part.data.id !== approvalId) {
          return part;
        }
        hit = true;
        tool = part.data.tool;
        return {
          ...part,
          data: {
            ...part.data,
            resolved: true,
            allow: outcome.allow,
            reason: outcome.reason,
            late: outcome.late,
          },
        };
      });
      touched = touched || hit;
      return hit ? { ...msg, content } : msg;
    });
    if (touched) this.emit();
    return tool;
  }

  /**
   * The Allow/Deny buttons. Marks the chip resolved immediately rather than
   * waiting for Python's echo, so the UI never looks stuck after a click; the
   * echo (or a timeout) lands on the same id and is idempotent.
   */
  respondToolApproval(approvalId: string, allow: boolean): void {
    this.setToolApprovalResolved(approvalId, { allow });
    postCommand({ type: "tool_approval_response", id: approvalId, allow });
  }

  /**
   * The enable-offer chip's buttons. Enabling flips the persisted setting AND
   * posts a visible user message so the agent (whose render_widget call
   * already returned "disabled") deterministically learns the state changed
   * and retries - tool results and user messages are the only channels that
   * reliably reach a mid-conversation CLI session.
   */
  resolveWidgetOffer(offerId: string, enable: boolean): void {
    this.messages = this.messages.map((msg) => {
      if (msg.role !== "assistant") return msg;
      let touched = false;
      const content = msg.content.map((part) => {
        if (part.type !== "data" || part.name !== "widget_offer" || part.data.id !== offerId) {
          return part;
        }
        touched = true;
        return { ...part, data: { ...part.data, resolved: true } };
      });
      return touched ? { ...msg, content } : msg;
    });
    this.emit();
    if (enable) {
      this.setSetting("widget_rendering", true);
      const note = "(I've enabled widget rendering - go ahead and render it.)";
      // The offer usually appears MID-turn (the tool already returned
      // "disabled" and the agent is still streaming its fallback), and
      // sendUserMessage drops input while running - so queue and flush on
      // the turn-ending event instead of losing the retry nudge.
      if (this.isRunning) {
        this.pendingAutoSend = note;
      } else {
        this.sendUserMessage(note);
      }
    }
  }

  private pendingAutoSend: string | null = null;
  private pendingSupersedeId: string | null = null;

  private flushPendingAutoSend(): void {
    const note = this.pendingAutoSend;
    this.pendingAutoSend = null;
    if (note) this.sendUserMessage(note);
  }

  private upsertProposal(proposal: ProposalPayload): void {
    const existingId = this.findProposalMessageId(proposal.id);
    if (existingId) {
      this.updateMessage(existingId, (msg) => ({
        ...msg,
        content: msg.content.map((part) =>
          part.type === "data" && part.name === "proposal" && part.data.id === proposal.id
            ? { ...part, data: proposal }
            : part
        ),
      }));
    } else {
      const current = this.ensureCurrentAssistant();
      const part: ProposalDataPart = { type: "data", name: "proposal", data: proposal };
      this.updateMessage(current.id, (msg) => ({ ...msg, content: [...msg.content, part] }));
    }
    this.emit();
  }

  private upsertGrading(grading: GradingPayload): void {
    const existingId = this.findGradingMessageId(grading.id);
    if (existingId) {
      this.updateMessage(existingId, (msg) => ({
        ...msg,
        content: msg.content.map((part) =>
          part.type === "data" && part.name === "grading" && part.data.id === grading.id
            ? { ...part, data: grading }
            : part
        ),
      }));
    } else {
      const current = this.ensureCurrentAssistant();
      const part: GradingDataPart = { type: "data", name: "grading", data: grading };
      this.updateMessage(current.id, (msg) => ({ ...msg, content: [...msg.content, part] }));
    }
    this.emit();
  }

  private resolveProposal(event: ProposalResolvedEvent): void {
    const messageId = this.findProposalMessageId(event.id);
    if (!messageId) return;
    this.updateMessage(messageId, (msg) => ({
      ...msg,
      content: msg.content.map((part) => {
        if (part.type !== "data" || part.name !== "proposal" || part.data.id !== event.id) return part;
        return {
          ...part,
          data: {
            ...part.data,
            status: event.status,
            note_id: event.note_id ?? part.data.note_id,
            warnings: event.warnings ?? part.data.warnings,
            revertible: event.revertible ?? part.data.revertible,
            // A fresh resolution clears any stale conflict from a previous
            // undo attempt, so the override offer cannot outlive its reason.
            errorMessage: undefined,
            revertConflict: undefined,
          },
        };
      }),
    }));
    this.emit();
  }

  /**
   * proposals.py's preview_request answering our debounced `proposal_preview`:
   * the card preview re-rendered from what the user is typing. Only `previews`
   * changes - status, fields and revision are NOT ours to move here, and a
   * preview that arrives after the proposal resolved is dropped rather than
   * repainting a settled card.
   */
  private updateProposalPreview(event: { id?: string; previews?: unknown }): void {
    const id = String(event.id ?? "");
    const messageId = this.findProposalMessageId(id);
    if (!messageId || !event.previews) return;
    this.updateMessage(messageId, (msg) => ({
      ...msg,
      content: msg.content.map((part) => {
        if (part.type !== "data" || part.name !== "proposal" || part.data.id !== id) return part;
        if (part.data.status !== "pending") return part;
        return { ...part, data: { ...part.data, previews: event.previews } };
      }),
    }));
    this.emit();
  }

  private errorProposal(event: ProposalErrorEvent): void {
    const messageId = this.findProposalMessageId(event.id);
    if (!messageId) return;
    this.updateMessage(messageId, (msg) => ({
      ...msg,
      content: msg.content.map((part) => {
        if (part.type !== "data" || part.name !== "proposal" || part.data.id !== event.id) return part;
        return {
          ...part,
          data: {
            ...part.data,
            errorMessage: event.message,
            revertConflict: !!(event as { conflict?: boolean }).conflict,
          },
        };
      }),
    }));
    this.emit();
  }

  private appendError(message: string): void {
    const current = this.ensureCurrentAssistant();
    const part: ErrorDataPart = { type: "data", name: "error", data: { message } };
    this.updateMessage(current.id, (msg) => ({ ...msg, content: [...msg.content, part] }));
  }

  private setUsage(event: UsageEvent): void {
    this.usage = {
      costUsd: event.cost_usd,
      inputTokens: event.input_tokens,
      outputTokens: event.output_tokens,
      cacheReadTokens: event.cache_read_tokens ?? null,
      cacheCreationTokens: event.cache_creation_tokens ?? null,
      contextWindow: event.context_window ?? null,
      fastState: event.fast_mode_state ?? null,
    };
    this.emit();
  }

  private finishTurn(status: MessageStatus): void {
    if (this.currentAssistantId) {
      this.updateMessage(this.currentAssistantId, (msg) => ({ ...msg, status }));
    }
    this.currentAssistantId = null;
    this.isRunning = false;
    // A reply that lands while the dock is collapsed is one the user hasn't
    // seen: light the rail ember until they expand. Cancelled turns are the
    // user's own doing, so nothing new awaits them.
    const cancelled = status.type === "incomplete" && status.reason === "cancelled";
    if (!cancelled && this.ui.dock?.expanded === false) this.hasUnread = true;
    this.emit();
  }

  private reset(): void {
    this.messages = [];
    this.currentAssistantId = null;
    this.isRunning = false;
    this.hasUnread = false;
    this.usage = null;
    this.emit();
  }

  /**
   * Rebuild the message list from a saved chat's recorded events (Python's
   * `history_load` push after the user picks a chat from History). The parity
   * rebuild dropped this handler, so clicking a chat did nothing but blank the
   * pane (dogfood 2026-07-12). Mirrors classic app.js `replayHistory`: a
   * user_message clears the current turn so the NEXT assistant content lazily
   * opens a fresh (completed) assistant bubble - interrupted turns with no
   * reply therefore leave no empty bubble. Everything else (assistant_text,
   * tool_call_*, proposal, proposal_resolved, usage) routes through the same
   * handlers the live stream uses, so replay and live render identically.
   * Recorded types: see transcripts.py RECORDED_TYPES.
   */
  private replayHistory(events: readonly unknown[]): void {
    this.reset();
    for (const raw of events) {
      const ev = raw as { type?: string; text?: string };
      if (!ev || typeof ev.type !== "string") continue;
      if (ev.type === "user_message") {
        const userMsg: StoreMessage = {
          id: nextId("u"),
          role: "user",
          content: [{ type: "text", text: String(ev.text ?? "") }],
          createdAt: new Date(),
        };
        this.messages = [...this.messages, userMsg];
        this.currentAssistantId = null; // next assistant content opens a fresh bubble
      } else if (ev.type === "assistant_text") {
        this.appendText("text", String(ev.text ?? ""));
      } else {
        // tool_call_started/finished, proposal, proposal_resolved, usage:
        // identical shapes to the live stream; reuse those handlers. isRunning
        // is false, so ensureCurrentAssistant marks new bubbles complete.
        this.dispatch(raw);
      }
    }
    this.currentAssistantId = null;
    this.isRunning = false;
    this.emit();
  }
}
