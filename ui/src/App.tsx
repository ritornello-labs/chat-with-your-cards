import { ChatRuntimeProvider, useChatState } from "./ChatRuntimeProvider";
import { Thread } from "./Thread";
import { UsageFooter } from "./components/UsageFooter";
import type { ChatStore } from "./store";

function Footer({ store }: { store: ChatStore }) {
  const state = useChatState(store);
  return <UsageFooter usage={state.usage} />;
}

/**
 * Mounted into the host page's #cwyc-root container (see main.tsx / dev-main.tsx) -
 * this component does not own that id itself, so it can be mounted anywhere.
 */
export function App({ store }: { store: ChatStore }) {
  return (
    <ChatRuntimeProvider store={store}>
      <div className="cwyc-app">
        <Thread store={store} />
        <Footer store={store} />
      </div>
    </ChatRuntimeProvider>
  );
}
