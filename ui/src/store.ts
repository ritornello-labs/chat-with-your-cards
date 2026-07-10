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
 *   thinking_delta       -> appended to a trailing {type:"reasoning"} part (stub-only, see events.ts)
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

interface ReasoningPart {
  readonly type: "reasoning";
  readonly text: string;
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
}

export interface ChatState {
  readonly messages: readonly ThreadMessageLike[];
  readonly isRunning: boolean;
  readonly usage: UsageSnapshot | null;
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
      case "thinking_delta":
        this.appendText("reasoning", String((event as { text: string }).text ?? ""));
        break;
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

  private appendText(kind: "text" | "reasoning", delta: string): void {
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
}
