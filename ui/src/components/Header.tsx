import { useEffect, useRef, useState, type ReactNode } from "react";
import { useChatState } from "../ChatRuntimeProvider";
import type { ChatStore, DoctorRow, HistoryEntry } from "../store";

/**
 * The dock header (DESIGN.md section 9): chat management only - New chat,
 * History, the Open-in-Claude-Code split button, and the doctor cog. Every
 * action posts a bridge command whose Python handler already exists
 * (__init__.py's _wire_bridge); this component adds no new protocol.
 */

/** Close an open panel on outside-click or Esc, the classic menu contract. */
function useDismiss(open: boolean, close: () => void) {
  const ref = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) close();
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") close();
    };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open, close]);
  return ref;
}

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

function DoctorPanel({ rows }: { rows: readonly DoctorRow[] | null }) {
  return (
    <div className="cwyc-panel cwyc-panel-header" role="dialog" aria-label="Setup check">
      <div className="cwyc-panel-title">Setup check</div>
      {rows === null ? (
        <div className="cwyc-panel-empty">Checking…</div>
      ) : (
        rows.map((row, i) => (
          <div key={i} className={"cwyc-doctor-row cwyc-doctor-" + row.status}>
            <span className="cwyc-doctor-dot" aria-hidden="true" />
            <span className="cwyc-doctor-label">{row.label}</span>
            <span className="cwyc-doctor-detail">{row.detail}</span>
          </div>
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
  const [open, setOpen] = useState<"history" | "doctor" | "cc" | null>(null);
  const ref = useDismiss(open !== null, () => setOpen(null));
  const ui = useChatState(store).ui;
  const targetLabel = ui.openTarget === "desktop" ? "Desktop app" : "Terminal";

  return (
    <div className="cwyc-header" data-testid="header" ref={ref}>
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

      <HeaderButton title="Setup check" testid="settings" onClick={() => {
        if (open !== "doctor") store.runDoctor();
        setOpen(open === "doctor" ? null : "doctor");
      }}>
        <svg viewBox="0 0 16 16" width="15" height="15" aria-hidden="true">
          <path
            d="M8 10.2A2.2 2.2 0 1 0 8 5.8a2.2 2.2 0 0 0 0 4.4zm5.4-2.2.9-1.6-1.4-1.4-1.6.9a4.7 4.7 0 0 0-1-.6L10 3.5H8l-.3 1.8a4.7 4.7 0 0 0-1 .6l-1.6-.9-1.4 1.4.9 1.6a4.7 4.7 0 0 0-.6 1L2.2 9v2h1.8"
            stroke="currentColor"
            strokeWidth="1.45"
            strokeLinecap="round"
            fill="none"
          />
        </svg>
      </HeaderButton>

      {open === "history" ? <HistoryPanel store={store} onClose={() => setOpen(null)} /> : null}
      {open === "doctor" ? <DoctorPanel rows={ui.doctor} /> : null}
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
