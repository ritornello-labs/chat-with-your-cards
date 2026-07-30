import { useState, type ReactNode } from "react";
import { useChatState } from "../ChatRuntimeProvider";
import { useDismiss } from "../hooks/useDismiss";
import { SettingsPanel } from "./SettingsPanel";
import type { ChatStore, HistoryEntry } from "../store";

/**
 * The dock header (DESIGN.md section 9): the collapse-to-rail control, chat
 * management (New chat, History), the Open-in-Claude-Code split button, and
 * the Settings cog (which also hosts the Setup check). With the native Qt
 * title bar gone (dock.py), this row IS the dock's chrome.
 */

function when(entry: HistoryEntry): string {
  if (!entry.updated_at) return "";
  const d = new Date(entry.updated_at * 1000);
  const today = new Date();
  const sameDay = d.toDateString() === today.toDateString();
  return sameDay
    ? d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" })
    : d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

function HistoryPanel({ store, onClose }: { store: ChatStore; onClose: () => void }) {
  const history = useChatState(store).ui.history;
  return (
    <div className="cwyc-panel cwyc-panel-header cwyc-panel-header-left" role="menu">
      <div className="cwyc-panel-title">Chats</div>
      {history === null ? (
        <div className="cwyc-panel-empty">Loading…</div>
      ) : history.length === 0 ? (
        <div className="cwyc-panel-empty">No saved chats yet.</div>
      ) : (
        history.map((entry) => (
          <button
            key={entry.id}
            type="button"
            className="cwyc-menu-item"
            onClick={() => {
              store.loadHistory(entry.id);
              onClose();
            }}
          >
            <span className="cwyc-menu-label">{entry.title || "Untitled chat"}</span>
            <span className="cwyc-menu-hint">{when(entry)}</span>
          </button>
        ))
      )}
    </div>
  );
}

function HeaderButton(props: {
  title: string;
  testid: string;
  onClick: () => void;
  children: ReactNode;
}) {
  return (
    <button
      type="button"
      className="cwyc-hbtn"
      title={props.title}
      aria-label={props.title}
      data-testid={props.testid}
      onClick={props.onClick}
    >
      {props.children}
    </button>
  );
}

function TargetGlyph({ target }: { target: "terminal" | "desktop" }) {
  // Generic glyphs, deliberately NOT Anthropic's logo (COMPLIANCE.md rule 4:
  // naming the launch target is nominative use; their mark is not cleared).
  // The glyph doubles as the current-target indicator: terminal prompt vs
  // app window, readable without opening the chooser.
  return target === "terminal" ? (
    <svg viewBox="0 0 14 14" width="13" height="13" aria-hidden="true">
      <rect x="1" y="2" width="12" height="10" rx="2" stroke="currentColor" strokeWidth="1.2" fill="none" />
      <path d="M3.6 5.4 5.8 7.2 3.6 9M7.2 9.2h3.2" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round" fill="none" />
    </svg>
  ) : (
    <svg viewBox="0 0 14 14" width="13" height="13" aria-hidden="true">
      <rect x="1" y="2" width="12" height="10" rx="2" stroke="currentColor" strokeWidth="1.2" fill="none" />
      <path d="M1 5.2h12" stroke="currentColor" strokeWidth="1.2" fill="none" />
      <circle cx="3.2" cy="3.6" r="0.7" fill="currentColor" />
    </svg>
  );
}

export function Header({ store }: { store: ChatStore }) {
  const [open, setOpen] = useState<"history" | "settings" | "cc" | null>(null);
  const ref = useDismiss(open !== null, () => setOpen(null));
  const ui = useChatState(store).ui;
  const targetLabel = ui.openTarget === "desktop" ? "Desktop app" : "Terminal";
  const side = ui.dock?.side ?? "right";

  return (
    <div className="cwyc-header" data-testid="header" ref={ref}>
      <HeaderButton
        title="Collapse to the side rail (stays one click away)"
        testid="collapse"
        onClick={() => store.setDockExpanded(false)}
      >
        {/* Double chevron pointing INTO the dock edge: the direction the panel folds away. */}
        <svg viewBox="0 0 16 16" width="15" height="15" aria-hidden="true">
          {side === "right" ? (
            <path
              d="M4.5 4 8.5 8l-4 4M8.5 4l4 4-4 4"
              stroke="currentColor"
              strokeWidth="1.5"
              strokeLinecap="round"
              strokeLinejoin="round"
              fill="none"
            />
          ) : (
            <path
              d="M11.5 4 7.5 8l4 4M7.5 4l-4 4 4 4"
              stroke="currentColor"
              strokeWidth="1.5"
              strokeLinecap="round"
              strokeLinejoin="round"
              fill="none"
            />
          )}
        </svg>
      </HeaderButton>
      <HeaderButton title="New chat" testid="new-chat" onClick={() => store.newChat()}>
        <svg viewBox="0 0 16 16" width="15" height="15" aria-hidden="true">
          <path d="M8 3v10M3 8h10" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" fill="none" />
        </svg>
      </HeaderButton>
      <HeaderButton
        title="Chat history"
        testid="history-button"
        onClick={() => {
          if (open !== "history") store.requestHistory();
          setOpen(open === "history" ? null : "history");
        }}
      >
        <svg viewBox="0 0 16 16" width="15" height="15" aria-hidden="true">
          <path
            d="M8 4.5V8l2.5 1.5M14 8A6 6 0 1 1 8 2a6 6 0 0 1 6 6z"
            stroke="currentColor"
            strokeWidth="1.4"
            strokeLinecap="round"
            fill="none"
          />
        </svg>
      </HeaderButton>
      {/* The set-aside tray (task #33). Present only while it has contents
          (or is open): an empty tray is not worth a permanent button, and
          the transient "Card set aside" chip introduces it at the moment it
          first becomes non-empty. */}
      {ui.setAsideCount > 0 || ui.pane === "aside" ? (
        <button
          type="button"
          className={"cwyc-hbtn cwyc-hbtn-tray" + (ui.pane === "aside" ? " cwyc-hbtn-active" : "")}
          title={
            ui.pane === "aside"
              ? "Back to the chat"
              : `Cards set aside today (${ui.setAsideCount})`
          }
          aria-label={`Set-aside tray, ${ui.setAsideCount} card${ui.setAsideCount === 1 ? "" : "s"}`}
          aria-pressed={ui.pane === "aside"}
          data-testid="tray-button"
          onClick={() => store.toggleSetAside()}
        >
          <svg viewBox="0 0 16 16" width="15" height="15" aria-hidden="true">
            {/* an inbox tray: where the set-aside cards wait */}
            <path
              d="M2 9.5V12a1.5 1.5 0 0 0 1.5 1.5h9A1.5 1.5 0 0 0 14 12V9.5M2 9.5h3.2l1 1.6h3.6l1-1.6H14M2 9.5l1.6-5A1.5 1.5 0 0 1 5 3.5h6a1.5 1.5 0 0 1 1.4 1l1.6 5"
              stroke="currentColor"
              strokeWidth="1.3"
              strokeLinecap="round"
              strokeLinejoin="round"
              fill="none"
            />
          </svg>
          {ui.setAsideCount > 0 ? (
            <span className="cwyc-hbtn-badge" data-testid="tray-badge">
              {ui.setAsideCount > 9 ? "9+" : ui.setAsideCount}
            </span>
          ) : null}
        </button>
      ) : null}

      <div className="cwyc-header-spacer" />

      <div className="cwyc-split" data-testid="open-cc-split">
        <button
          type="button"
          className="cwyc-split-main"
          title={`Open this chat in Claude Code — ${targetLabel}`}
          aria-label={`Open this chat in Claude Code (${targetLabel})`}
          data-testid="open-cc"
          onClick={() => store.openInClaude()}
        >
          <TargetGlyph target={ui.openTarget === "desktop" ? "desktop" : "terminal"} />
          <span>Claude Code</span>
          <svg className="cwyc-split-out" viewBox="0 0 10 10" width="9" height="9" aria-hidden="true">
            <path d="M4 2h4v4M8 2 3.2 6.8" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" fill="none" />
          </svg>
        </button>
        <button
          type="button"
          className="cwyc-split-caret"
          aria-label="Choose where to open"
          data-testid="open-cc-caret"
          onClick={() => setOpen(open === "cc" ? null : "cc")}
        >
          <svg viewBox="0 0 12 12" width="10" height="10" aria-hidden="true">
            <path d="M2.5 4.5 6 8l3.5-3.5" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" fill="none" />
          </svg>
        </button>
      </div>

      <HeaderButton
        title="Settings"
        testid="settings"
        onClick={() => setOpen(open === "settings" ? null : "settings")}
      >
        {/* A real gear (Feather "settings", MIT): the previous hand-drawn
            glyph didn't read as a cog (dogfood 2026-07-13). */}
        <svg viewBox="0 0 24 24" width="15" height="15" aria-hidden="true">
          <circle cx="12" cy="12" r="3" fill="none" stroke="currentColor" strokeWidth="2" />
          <path
            d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z"
            fill="none"
            stroke="currentColor"
            strokeWidth="2"
            strokeLinecap="round"
            strokeLinejoin="round"
          />
        </svg>
      </HeaderButton>

      {open === "history" ? <HistoryPanel store={store} onClose={() => setOpen(null)} /> : null}
      {open === "settings" ? <SettingsPanel store={store} /> : null}
      {open === "cc" ? (
        <div className="cwyc-panel cwyc-panel-header" role="menu">
          <div className="cwyc-panel-title">Open in</div>
          {(["terminal", "desktop"] as const).map((target) => (
            <button
              key={target}
              type="button"
              className={"cwyc-menu-item" + (ui.openTarget === target ? " cwyc-active" : "")}
              onClick={() => {
                store.setOpenTarget(target);
                setOpen(null);
              }}
            >
              <span className="cwyc-menu-label cwyc-menu-label-glyph">
                <TargetGlyph target={target} />
                {target === "terminal" ? "Terminal" : "Desktop app"}
              </span>
              {ui.openTarget === target ? <span className="cwyc-menu-hint">current</span> : null}
            </button>
          ))}
        </div>
      ) : null}
    </div>
  );
}
