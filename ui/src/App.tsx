import { useEffect, useState } from "react";
import { ChatRuntimeProvider, useChatState } from "./ChatRuntimeProvider";
import { Thread } from "./Thread";
import { Header } from "./components/Header";
import { UsageFooter } from "./components/UsageFooter";
import type { ChatStore } from "./store";

function Footer({ store }: { store: ChatStore }) {
  const state = useChatState(store);
  return <UsageFooter usage={state.usage} />;
}

/** Transient strip for Python "notice" pushes (e.g. "Switched to Opus…"). */
function NoticeStrip({ store }: { store: ChatStore }) {
  const notice = useChatState(store).ui.notice;
  const [visibleSeq, setVisibleSeq] = useState(0);
  useEffect(() => {
    if (!notice) return;
    setVisibleSeq(notice.seq);
    const timer = window.setTimeout(() => setVisibleSeq(0), 6000);
    return () => window.clearTimeout(timer);
  }, [notice]);
  if (!notice || visibleSeq !== notice.seq || !notice.text) return null;
  return (
    <div className="cwyc-notice" role="status">
      {notice.text}
    </div>
  );
}

/**
 * Mounted into the host page's #cwyc-root container (see main.tsx / dev-main.tsx) -
 * this component does not own that id itself, so it can be mounted anywhere.
 */
export function App({ store }: { store: ChatStore }) {
  return (
    <ChatRuntimeProvider store={store}>
      <div className="cwyc-app">
        <Header store={store} />
        <Thread store={store} />
        <NoticeStrip store={store} />
        <Footer store={store} />
      </div>
    </ChatRuntimeProvider>
  );
}
