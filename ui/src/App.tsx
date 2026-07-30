import { useEffect, useState } from "react";
import { ChatRuntimeProvider, useChatState } from "./ChatRuntimeProvider";
import { Thread } from "./Thread";
import { useKeyboardReview } from "./hooks/useKeyboardReview";
import { Header } from "./components/Header";
import { Rail } from "./components/Rail";
import { SetAsidePane } from "./components/SetAsidePane";
import { UsageFooter } from "./components/UsageFooter";
import type { ChatStore } from "./store";

function Footer({ store }: { store: ChatStore }) {
  const state = useChatState(store);
  return <UsageFooter usage={state.usage} model={state.ui.agent.model} />;
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
 * The dock shell: two layers over one warm surface. `.cwyc-full` is the whole
 * chat UI; `.cwyc-rail` is the slim collapsed presence. The host (dock.py, or
 * the dev replayer) drives which one shows via dock_state pushes; the two
 * crossfade while Python animates the real dock width, and the full layer is
 * width-pinned during the animation so its text never rewraps mid-slide.
 *
 * Until the first dock_state is known the shell renders the quiet boot state
 * (bare background, no chrome) rather than guessing and flashing the wrong
 * layer. In Anki, dock.py plants window.CWYC_INITIAL_DOCK in the body
 * fragment so this resolves at mount, before the first paint.
 */
function Shell({ store }: { store: ChatStore }) {
  const { ui } = useChatState(store);
  const dock = ui.dock;

  // Document-level, capture-phase: proposal review chords and Escape (#21).
  useKeyboardReview(store);

  useEffect(() => {
    // The production bundle runs in <head>, before dock.py's body fragment
    // (and its initial-state script) exists - so read it at mount, not at
    // store construction. Live dock_state pushes take over from there.
    if (store.getSnapshot().ui.dock === null) {
      const initial = (window as unknown as { CWYC_INITIAL_DOCK?: unknown }).CWYC_INITIAL_DOCK;
      if (initial && typeof initial === "object") {
        store.dispatch({ type: "dock_state", animating: false, ...initial });
      }
    }
  }, [store]);

  const railed = dock !== null && !dock.expanded;
  const animating = dock !== null && dock.animating;
  // Pin the full layer to the expanded width while collapsed or animating so
  // the one-step dock resize clips it instead of rewrapping every line.
  const pinWidth = dock !== null && (dock.animating || !dock.expanded) ? dock.width : undefined;
  return (
    <div
      className={
        "cwyc-app" +
        (railed ? " cwyc-railed" : "") +
        (animating ? " cwyc-anim" : "") +
        (animating && !railed ? " cwyc-anim-expand" : "") +
        (dock !== null && dock.side === "left" ? " cwyc-side-left" : " cwyc-side-right") +
        (dock === null ? " cwyc-boot" : "")
      }
    >
      <div className="cwyc-full" style={pinWidth !== undefined ? { width: pinWidth } : undefined}>
        <Header store={store} />
        {/* The set-aside tray (task #33) replaces the thread, not the shell:
            header, notices and footer stay put so it reads as a view flip. */}
        {ui.pane === "aside" ? <SetAsidePane store={store} /> : <Thread store={store} />}
        <NoticeStrip store={store} />
        <Footer store={store} />
      </div>
      <Rail store={store} />
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
      <Shell store={store} />
    </ChatRuntimeProvider>
  );
}
