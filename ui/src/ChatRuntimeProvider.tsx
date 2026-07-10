import { useCallback, useSyncExternalStore, type ReactNode } from "react";
import {
  AssistantRuntimeProvider,
  useExternalStoreRuntime,
  type AppendMessage,
  type ThreadMessageLike,
} from "@assistant-ui/react";
import type { ChatState, ChatStore } from "./store";

function extractUserText(content: AppendMessage["content"]): string {
  return content
    .filter((part): part is { type: "text"; text: string } => part.type === "text")
    .map((part) => part.text)
    .join("");
}

/**
 * Bridges our external ChatStore into assistant-ui's ExternalStoreRuntime.
 * `messages` are already ThreadMessageLike (store.ts does the event ->
 * message-part mapping), so convertMessage is the identity function - it
 * only exists because useExternalStoreRuntime is generic over the message
 * type T and requires a converter unless T is exactly ThreadMessage.
 */
export function ChatRuntimeProvider({
  store,
  children,
}: {
  store: ChatStore;
  children: ReactNode;
}) {
  const state = useSyncExternalStore<ChatState>(store.subscribe, store.getSnapshot, store.getSnapshot);

  const onNew = useCallback(
    async (message: AppendMessage) => {
      store.sendUserMessage(extractUserText(message.content));
    },
    [store]
  );

  const onCancel = useCallback(async () => {
    store.cancel();
  }, [store]);

  const convertMessage = useCallback((message: ThreadMessageLike) => message, []);

  const runtime = useExternalStoreRuntime<ThreadMessageLike>({
    messages: state.messages,
    isRunning: state.isRunning,
    convertMessage,
    onNew,
    onCancel,
  });

  return <AssistantRuntimeProvider runtime={runtime}>{children}</AssistantRuntimeProvider>;
}

export function useChatState(store: ChatStore): ChatState {
  return useSyncExternalStore<ChatState>(store.subscribe, store.getSnapshot, store.getSnapshot);
}
