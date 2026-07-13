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
 *   (unknown type)          -> ignored, matching app.js dispatch()'s forward-compatible default case
 */

import type { ThreadMessageLike } from "@assistant-ui/react";
import { postCommand } from "./bridge";
import type {
  BridgeCommand,
  ChatEvent,
  ProposalErrorEvent,
  ProposalEvent,
  ProposalPayload,
  ProposalResolvedEvent,
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
export type ProposalCardData = ProposalPayload & { errorMessage?: string };

interface ProposalDataPart {
  readonly type: "data";
  readonly name: "proposal";
  readonly data: ProposalCardData;
}

interface ErrorDataPart {
  readonly type: "data";
  readonly name: "error";
  readonly data: { message: string };
}

type AssistantPart = TextPart | ReasoningPart | ToolCallPart | ProposalDataPart | ErrorDataPart;

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
}

// ---- UI/control state pushed by Python (controller.py / __init__.py) ----

export interface AgentState {
  readonly backend: string;
  readonly model: string; // "" | "fable" | "opus" | "sonnet" | "haiku"
  readonly effort: string; // "" | "low" | "medium" | "high" | "max"
  readonly mode: string; // permission mode
  readonly fast: boolean; // fast mode (Opus-only; requires a respawn to change)
}

export interface NoteTypeMeta {
  readonly name: string;
  readonly fields: readonly string[];
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

export interface UiState {
  readonly agent: AgentState;
  readonly openTarget: "terminal" | "desktop";
  readonly meta: CollectionMeta;
  readonly pins: PinsState;
  readonly history: readonly HistoryEntry[] | null; // null = never fetched
  readonly doctor: readonly DoctorRow[] | null; // null = never run
  readonly notice: { text: string; seq: number } | null;
}

export const PERMISSION_MODES: readonly { id: string; label: string; hint: string }[] = [
  { id: "ask-each-read", label: "Ask each read", hint: "Every tool call needs your OK" },
  { id: "read-only", label: "Read-only", hint: "No write tools offered at all" },
  { id: "default", label: "Propose", hint: "Reads free; writes as review cards" },
  { id: "auto-accept", label: "Auto-accept", hint: "New notes apply instantly (capped)" },
  { id: "trusted-writes", label: "Trusted writes", hint: "Writes apply under a session budget" },
];

const EMPTY_PINS: PinsState = { deck: "", note_type: "", tags: [], fields: {} };

const DEFAULT_UI_STATE: UiState = {
  agent: { backend: "auto", model: "", effort: "", mode: "default", fast: false },
  openTarget: "terminal",
  meta: { decks: [], noteTypes: [], tags: [] },
  pins: EMPTY_PINS,
  history: null,
  doctor: null,
  notice: null,
};

export interface ChatState {
  readonly messages: readonly ThreadMessageLike[];
  readonly isRunning: boolean;
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

  rejectProposal(id: string): void {
    postCommand({ type: "proposal_reject", id });
  }

  // ---- outbound: control-surface commands (header + composer row) ----

  setAgent(model: string, effort: string, fast?: boolean): void {
    // Optimistic: Python re-pushes the authoritative "agent" state after.
    // fast defaults to the current value so model/effort-only callers
    // (the Model/Effort menu sections) don't clobber it.
    const nextFast = fast === undefined ? this.ui.agent.fast : fast;
    this.ui = { ...this.ui, agent: { ...this.ui.agent, model, effort, fast: nextFast } };
    this.emit();
    postCommand({ type: "set_agent", model, effort, fast: nextFast });
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
      case "usage":
        this.setUsage(event as UsageEvent);
        break;
      case "done":
        this.finishTurn({ type: "complete", reason: "stop" });
        break;
      case "cancelled":
        this.finishTurn({ type: "incomplete", reason: "cancelled" });
        break;
      case "error":
        this.appendError(String((event as { message?: string }).message ?? "Unknown error"));
        this.finishTurn({ type: "incomplete", reason: "error" });
        break;
      case "reset":
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
        };
        this.ui = {
          ...this.ui,
          agent: {
            backend: String(agent.backend ?? this.ui.agent.backend),
            model: String(agent.model ?? ""),
            effort: String(agent.effort ?? ""),
            mode: String(agent.mode ?? "default"),
            fast: Boolean(agent.fast),
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
      case "notice": {
        this.noticeSeq += 1;
        this.ui = {
          ...this.ui,
          notice: { text: String((event as { text?: string }).text ?? ""), seq: this.noticeSeq },
        };
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
          },
        };
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
        return { ...part, data: { ...part.data, errorMessage: event.message } };
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
    };
    this.emit();
  }

  private finishTurn(status: MessageStatus): void {
    if (this.currentAssistantId) {
      this.updateMessage(this.currentAssistantId, (msg) => ({ ...msg, status }));
    }
    this.currentAssistantId = null;
    this.isRunning = false;
    this.emit();
  }

  private reset(): void {
    this.messages = [];
    this.currentAssistantId = null;
    this.isRunning = false;
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
