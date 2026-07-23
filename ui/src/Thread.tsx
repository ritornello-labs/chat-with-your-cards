import { useMemo } from "react";
import type { KeyboardEvent } from "react";
import { ComposerPrimitive, MessagePrimitive, ThreadPrimitive } from "@assistant-ui/react";
import { useChatState } from "./ChatRuntimeProvider";
import type { ChatStore, ProposalCardData } from "./store";
import { ProposalCard } from "./components/ProposalCard";
import { ToolCallCard } from "./components/ToolCallCard";
import { ReasoningBlock } from "./components/ReasoningBlock";
import { ErrorBanner } from "./components/ErrorBanner";
import { ErrorBoundary } from "./components/ErrorBoundary";
import { TextPart } from "./components/TextPart";
import { ModeChip, ModelPicker, PinsButton, ToolsChip } from "./components/ComposerControls";
import { VimComposer } from "./components/VimComposer";
import { WidgetCard, WidgetOfferChip } from "./components/WidgetCard";
import { SetupCard } from "./components/SetupCard";

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
          error: ErrorBanner,
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

function Composer({ store }: { store: ChatStore }) {
  const { isRunning, ui } = useChatState(store);

  // Make the tooltips true: Esc stops generation while streaming; Shift+Tab
  // cycles permission modes (both classic-UI behaviors, DESIGN.md section 9).
  const onKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Escape" && isRunning) {
      e.preventDefault();
      store.cancel();
    } else if (e.key === "Tab" && e.shiftKey) {
      e.preventDefault();
      store.cyclePermissionMode();
    }
  };

  return (
    <ComposerPrimitive.Root className="cwyc-composer">
      {ui.settings?.vimMode ? (
        <VimComposer store={store} />
      ) : (
        <ComposerPrimitive.Input
          className="cwyc-composer-input"
          placeholder="Ask about this card…"
          rows={1}
          data-testid="composer-input"
          onKeyDown={onKeyDown}
        />
      )}
      <div className="cwyc-composer-bar">
        <div className="cwyc-composer-left">
          <ModeChip store={store} />
          <PinsButton store={store} />
          <ToolsChip store={store} />
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
      <ThreadPrimitive.Viewport className="cwyc-viewport">
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
