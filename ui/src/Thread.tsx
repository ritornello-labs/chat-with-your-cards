import { ComposerPrimitive, MessagePrimitive, ThreadPrimitive } from "@assistant-ui/react";
import type { ChatStore } from "./store";
import { ProposalCard } from "./components/ProposalCard";
import { ToolCallCard } from "./components/ToolCallCard";
import { ReasoningBlock } from "./components/ReasoningBlock";
import { ErrorBanner } from "./components/ErrorBanner";
import { TextPart } from "./components/TextPart";

function UserMessage() {
  return (
    <MessagePrimitive.Root className="cwyc-row cwyc-row-user">
      <div className="cwyc-msg cwyc-msg-user">
        <MessagePrimitive.Parts />
      </div>
    </MessagePrimitive.Root>
  );
}

function AssistantMessage({ store }: { store: ChatStore }) {
  return (
    <MessagePrimitive.Root className="cwyc-row cwyc-row-assistant">
      <div className="cwyc-msg cwyc-msg-assistant">
        <MessagePrimitive.Parts
          components={{
            Text: TextPart,
            Reasoning: ReasoningBlock,
            tools: { Fallback: ToolCallCard },
            data: {
              by_name: {
                proposal: (props) => <ProposalCard {...props} store={store} />,
                error: ErrorBanner,
              },
            },
          }}
        />
      </div>
    </MessagePrimitive.Root>
  );
}

function Composer() {
  return (
    <ComposerPrimitive.Root className="cwyc-composer">
      <ComposerPrimitive.Input
        className="cwyc-composer-input"
        placeholder="Ask about this card…"
        rows={1}
      />
      <div className="cwyc-composer-bar">
        <ThreadPrimitive.If running={false}>
          <ComposerPrimitive.Send className="cwyc-send" aria-label="Send" title="Send (Enter)">
            <SendIcon />
          </ComposerPrimitive.Send>
        </ThreadPrimitive.If>
        <ThreadPrimitive.If running>
          <ComposerPrimitive.Cancel
            className="cwyc-send cwyc-send-stop"
            aria-label="Stop"
            title="Stop (Esc)"
          >
            <StopIcon />
          </ComposerPrimitive.Cancel>
        </ThreadPrimitive.If>
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

export function Thread({ store }: { store: ChatStore }) {
  return (
    <ThreadPrimitive.Root className="cwyc-thread">
      <ThreadPrimitive.Viewport className="cwyc-viewport">
        <ThreadPrimitive.Empty>
          <div className="cwyc-empty">Ask about this card, or anything in your collection.</div>
        </ThreadPrimitive.Empty>
        <ThreadPrimitive.Messages>
          {({ message }) => (message.role === "user" ? <UserMessage /> : <AssistantMessage store={store} />)}
        </ThreadPrimitive.Messages>
      </ThreadPrimitive.Viewport>
      <Composer />
    </ThreadPrimitive.Root>
  );
}
