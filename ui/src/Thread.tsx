import { useEffect, useMemo, useState } from "react";
import type { KeyboardEvent } from "react";
import {
  ComposerPrimitive,
  MessagePrimitive,
  ThreadPrimitive,
  useComposerRuntime,
} from "@assistant-ui/react";
import { useChatState } from "./ChatRuntimeProvider";
import type { ChatStore, GradingCardData, ProposalCardData } from "./store";
import { ProposalCard } from "./components/ProposalCard";
import { ToolCallCard } from "./components/ToolCallCard";
import { ReasoningBlock } from "./components/ReasoningBlock";
import { DenialBanner, ErrorBanner } from "./components/ErrorBanner";
import { ErrorBoundary } from "./components/ErrorBoundary";
import { TextPart } from "./components/TextPart";
import { AccessControl, AttachButton, ModelPicker, PinsButton } from "./components/ComposerControls";
import { VimComposer } from "./components/VimComposer";
import { LedgerStrip } from "./components/LedgerStrip";
import { Announcer } from "./components/Announcer";
import { DeferredUndoChip, SetAsideChip } from "./components/DeferControls";
import { WidgetCard, WidgetOfferChip } from "./components/WidgetCard";
import { SetupCard } from "./components/SetupCard";
import { ToolApprovalChip } from "./components/ToolApprovalChip";
import { GradingCard } from "./components/GradingCard";

function UserMessage() {
  return (
    <MessagePrimitive.Root className="cwyc-row cwyc-row-user" data-testid="user-message">
      <div className="cwyc-msg cwyc-msg-user">
        <MessagePrimitive.Parts />
      </div>
    </MessagePrimitive.Root>
  );
}

function AssistantMessage({ store }: { store: ChatStore }) {
  // MUST be memoized on a stable key. assistant-ui uses each entry below as a
  // JSX element TYPE, so a fresh arrow identity is a different component type
  // and React UNMOUNTS + remounts the whole part subtree instead of updating
  // it. That destroyed the proposal card's preview <iframe> and re-fetched its
  // images from Anki's media server — visible as a flicker whenever the
  // pointer crossed between the Anki window and the dock (assistant-ui's
  // MessagePrimitive.Root tracks per-message hover, and that state change
  // re-rendered this component). It also fired on every streamed token.
  // `store` is a single long-lived instance, so this memo effectively never
  // recomputes. (dogfood 2026-07-23)
  const components = useMemo(
    () => ({
      Text: TextPart,
      Reasoning: ReasoningBlock,
      tools: { Fallback: ToolCallCard },
      data: {
        by_name: {
          // A malformed proposal must degrade to a one-liner, not blank
          // the dock (dogfood 2026-07-12). resetKey = proposal id so a
          // fresh card retries rather than staying failed.
          proposal: (props: { data?: unknown }) => (
            <ErrorBoundary
              resetKey={(props.data as { id?: string } | undefined)?.id}
              fallback={
                <div className="cwyc-proposal cwyc-proposal-resolved" data-testid="proposal-card">
                  <div className="cwyc-proposal-warning">
                    This proposal card couldn’t be displayed.
                  </div>
                </div>
              }
            >
              {/* data/store are all ProposalCard needs (see its props doc). */}
              <ProposalCard data={props.data as ProposalCardData} store={store} />
            </ErrorBoundary>
          ),
          grading: (props: { data?: unknown }) => (
            <ErrorBoundary
              resetKey={(props.data as { id?: string } | undefined)?.id}
              fallback={
                <div className="cwyc-grading cwyc-grading-failed" data-testid="grading-card">
                  <div className="cwyc-grading-warning">This grading card couldn’t be displayed.</div>
                </div>
              }
            >
              <GradingCard data={props.data as GradingCardData} store={store} />
            </ErrorBoundary>
          ),
          error: ErrorBanner,
          denial: DenialBanner,
          // Ask-each-read: a blocked tool call is waiting on this (approvals.py).
          tool_approval: (props: { data?: unknown }) => (
            <ToolApprovalChip
              {...(props as { data: Parameters<typeof ToolApprovalChip>[0]["data"] })}
              store={store}
            />
          ),
          image: (props: Record<string, unknown>) => (
            <InlineImage {...(props as { data: { src: string; caption: string } })} />
          ),
          widget: (props: Record<string, unknown>) => (
            <WidgetCard {...(props as { data: { html: string; title: string } })} />
          ),
          widget_offer: (props: Record<string, unknown>) => (
            <WidgetOfferChip
              {...(props as { data: { id: string; title: string; resolved: boolean } })}
              store={store}
            />
          ),
        },
      },
    }),
    [store],
  );

  return (
    <MessagePrimitive.Root className="cwyc-row cwyc-row-assistant" data-testid="assistant-message">
      <div className="cwyc-msg cwyc-msg-assistant">
        <MessagePrimitive.Parts components={components} />
      </div>
    </MessagePrimitive.Root>
  );
}

/** Suggested-question ghost text (#23b): rotating, context-aware
 *  placeholders with Tab-to-accept - the classic UI's affordance, ported. */
const CARD_QUESTIONS = [
  "Why is this the answer?",
  "Give me a mnemonic for this",
  "Is this card well-formed?",
  "Set this card aside for today",
  "What else should I know about this?",
];
const COLLECTION_QUESTIONS = [
  "What's due today across my decks?",
  "What's my true retention this month?",
  "Find duplicates in my collection",
  "Suspend my leeches for now",
  "Spread my backlog over the next week",
];
const SUGGESTION_ROTATE_MS = 7000;

/** The bulk accept/reject bar (#23c): shows when several proposals await. */
function BulkProposalBar({ store }: { store: ChatStore }) {
  const { ui } = useChatState(store);
  const count = ui.pendingProposals.length;
  if (count < 2) return null;
  return (
    <div className="cwyc-bulk-bar" data-testid="bulk-bar" role="toolbar">
      <span className="cwyc-bulk-count">{count} proposals pending</span>
      <button
        type="button"
        className="cwyc-bulk-btn"
        data-testid="bulk-reject"
        onClick={() => store.rejectAllPending()}
      >
        Reject all
      </button>
      <button
        type="button"
        className="cwyc-bulk-btn cwyc-bulk-accept"
        data-testid="bulk-accept"
        onClick={() => store.acceptAllPending()}
      >
        Accept all
      </button>
    </div>
  );
}

/** This is a contextual action, not a persistent composer setting. Keeping it
 *  above the writing surface prevents a sporadic, wordy CTA from displacing
 *  Pins, Access, model, or Send. */
function LearningNudgeBar({ store }: { store: ChatStore }) {
  const { ui } = useChatState(store);
  const learning = ui.learning;
  if (
    !learning ||
    (!learning.nudge &&
      !learning.running &&
      !learning.updateReady &&
      !learning.automationNudge)
  ) return null;
  if (learning.automationNudge) {
    return (
      <div
        className="cwyc-learning-nudge cwyc-learning-automation"
        title="Open the learning settings for background analysis and automatic guidance updates."
      >
        <span>Run future pattern reviews on their own?</span>
        <div className="cwyc-learning-nudge-actions">
          <button
            type="button"
            data-testid="learning-automate"
            onClick={() => store.openLearningSettings()}
          >
            Automate this
          </button>
          <button
            type="button"
            className="cwyc-learning-nudge-dismiss"
            data-testid="learning-automate-dismiss"
            aria-label="Dismiss automation suggestion"
            title="Dismiss"
            onClick={() => store.dismissLearningAutomationNudge()}
          >
            ×
          </button>
        </div>
      </div>
    );
  }
  if (learning.updateReady) {
    return (
      <div
        className="cwyc-learning-nudge"
        title="Background analysis found a durable writing preference. Review the proposed guidance change before it affects future cards."
      >
        <span>Writing-guidance update ready</span>
        <button
          type="button"
          data-testid="learning-update-ready"
          onClick={() => store.showBackgroundSkillUpdate(learning.proposalId)}
        >
          Review
        </button>
      </div>
    );
  }
  if (learning.running) {
    return (
      <div
        className="cwyc-learning-nudge cwyc-learning-running"
        title="An isolated agent session is analyzing your corrections without changing this chat."
      >
        <span>
          Learning from <strong>{learning.pending}</strong> correction
          {learning.pending === 1 ? "" : "s"} in background…
        </span>
      </div>
    );
  }
  return (
    <div
      className="cwyc-learning-nudge"
      title="You changed AI-written cards after they were created. Review the patterns to improve future card writing."
    >
      <span>
        <strong>{learning.pending}</strong> of your edit{learning.pending === 1 ? "" : "s"} to learn from
      </span>
      <button type="button" data-testid="learning-nudge" onClick={() => store.startSkillReview()}>
        Review patterns
      </button>
    </div>
  );
}

function Composer({ store }: { store: ChatStore }) {
  const { isRunning, ui } = useChatState(store);
  const composer = useComposerRuntime();

  // Rotating suggestion for the placeholder (#23b). Pool follows the live
  // context; rotation is time-based and cheap (one state tick).
  const pool = ui.context?.kind === "card" ? CARD_QUESTIONS : COLLECTION_QUESTIONS;
  const [suggestionIndex, setSuggestionIndex] = useState(0);
  useEffect(() => {
    if (!ui.suggestedQuestions) return;
    const timer = window.setInterval(
      () => setSuggestionIndex((i) => i + 1),
      SUGGESTION_ROTATE_MS
    );
    return () => window.clearInterval(timer);
  }, [ui.suggestedQuestions]);
  const suggestion = ui.suggestedQuestions
    ? pool[suggestionIndex % pool.length]
    : null;
  const placeholder = suggestion
    ? `${suggestion}  (Tab to accept)`
    : "Ask about this card…";

  // Make the tooltips true: Esc stops generation while streaming; Shift+Tab
  // cycles permission modes (both classic-UI behaviors, DESIGN.md section 9).
  const onKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Escape" && isRunning) {
      e.preventDefault();
      store.cancel();
    } else if (e.key === "Tab" && e.shiftKey) {
      e.preventDefault();
      store.cyclePermissionMode();
    } else if (
      e.key === "Tab" &&
      suggestion &&
      e.currentTarget.value.trim() === ""
    ) {
      // Tab-to-accept the ghost suggestion (#23b) - only on an empty
      // composer, so Tab keeps its focus-move meaning while typing.
      e.preventDefault();
      composer.setText(suggestion);
    }
  };

  // Pasted images become attachments (#15): a clipboard screenshot has no
  // path, so this is the one transport where bytes cross the bridge - the
  // same route Anki's own editor paste uses. Text pastes pass through.
  const onPaste = (e: React.ClipboardEvent) => {
    const files = [...(e.clipboardData?.items ?? [])]
      .filter((item) => item.kind === "file" && item.type.startsWith("image/"))
      .map((item) => item.getAsFile())
      .filter((file): file is File => file !== null);
    if (!files.length) return;
    e.preventDefault();
    for (const file of files.slice(0, 4)) {
      if (file.size > 8_000_000) {
        // Mirrors the staging cap; a silent drop would look like a broken paste.
        store.dispatch({ type: "notice", text: `${file.name || "Pasted image"} is over the 8 MB cap` });
        continue;
      }
      const reader = new FileReader();
      reader.onload = () => {
        if (typeof reader.result === "string") {
          store.attachPasted(file.name || "", file.type, reader.result);
        }
      };
      reader.readAsDataURL(file);
    }
  };

  return (
    <ComposerPrimitive.Root className="cwyc-composer" onPaste={onPaste}>
      {/* What this chat changed, and the way back. Above the composer because
          it is about the session, not any one message (task #18). */}
      <LedgerStrip store={store} />
      <BulkProposalBar store={store} />
      <DeferredUndoChip store={store} />
      <LearningNudgeBar store={store} />
      {ui.settings?.vimMode ? (
        <VimComposer store={store} />
      ) : (
        <ComposerPrimitive.Input
          className="cwyc-composer-input"
          placeholder={placeholder}
          rows={1}
          data-testid="composer-input"
          onKeyDown={onKeyDown}
        />
      )}
      <div className="cwyc-composer-bar">
        {/* Message accessories, access policy, then model/send. Grid areas
            give each role a stable home; at narrow dock widths Access moves
            onto a deliberate second row instead of six chips wrapping in an
            accidental order. */}
        <div className="cwyc-composer-accessories">
          <PinsButton store={store} />
          <AttachButton store={store} />
          <SetAsideChip store={store} />
        </div>
        <div className="cwyc-composer-access">
          <AccessControl store={store} />
        </div>
        <div className="cwyc-composer-right">
          <ModelPicker store={store} />
          <ThreadPrimitive.If running={false}>
            <ComposerPrimitive.Send
              className="cwyc-send"
              aria-label="Send"
              title="Send (Enter)"
              data-testid="send"
            >
              <SendIcon />
            </ComposerPrimitive.Send>
          </ThreadPrimitive.If>
          <ThreadPrimitive.If running>
            <ComposerPrimitive.Cancel
              className="cwyc-send cwyc-send-stop"
              aria-label="Stop generating"
              title="Stop generating (Esc)"
              data-testid="stop"
            >
              <StopIcon />
            </ComposerPrimitive.Cancel>
          </ThreadPrimitive.If>
        </div>
      </div>
    </ComposerPrimitive.Root>
  );
}

function SendIcon() {
  return (
    <svg viewBox="0 0 16 16" width="16" height="16" aria-hidden="true">
      <path
        d="M8 13V3M4 7l4-4 4 4"
        stroke="currentColor"
        strokeWidth="1.8"
        strokeLinecap="round"
        strokeLinejoin="round"
        fill="none"
      />
    </svg>
  );
}

function StopIcon() {
  return (
    <svg viewBox="0 0 16 16" width="16" height="16" aria-hidden="true">
      <rect x="4" y="4" width="8" height="8" rx="1.5" fill="currentColor" />
    </svg>
  );
}

/** An image the agent chose to show inline (show_image). The src is a
 *  self-contained data: URI, so nothing loads over the network. */
function InlineImage({ data }: { data: { src: string; caption: string } }) {
  return (
    <figure className="cwyc-inline-image" data-testid="inline-image">
      <img src={data.src} alt={data.caption || "image"} loading="lazy" />
      {data.caption ? <figcaption>{data.caption}</figcaption> : null}
    </figure>
  );
}

export function Thread({ store }: { store: ChatStore }) {
  const { ui } = useChatState(store);
  return (
    <ThreadPrimitive.Root className="cwyc-thread">
      {/* Announcements come from here, NOT from a live region over the
          transcript: a reply streams token by token, and a live transcript
          re-announces the growing text on every delta (task #22). */}
      <Announcer store={store} />
      <ThreadPrimitive.Viewport
        className="cwyc-viewport"
        // `log` is the right role for a transcript and helps screen-reader
        // navigation, but it implies aria-live="polite" - hence the explicit
        // off, so the role does not smuggle the streaming spam back in.
        role="log"
        aria-live="off"
        aria-label="Conversation"
      >
        <ThreadPrimitive.Empty>
          {ui.setup ? (
            <SetupCard platform={ui.setup.platform} store={store} />
          ) : (
            <div className="cwyc-empty">Ask about this card, or anything in your collection.</div>
          )}
        </ThreadPrimitive.Empty>
        <ThreadPrimitive.Messages>
          {({ message }) => (message.role === "user" ? <UserMessage /> : <AssistantMessage store={store} />)}
        </ThreadPrimitive.Messages>
      </ThreadPrimitive.Viewport>
      <Composer store={store} />
    </ThreadPrimitive.Root>
  );
}
