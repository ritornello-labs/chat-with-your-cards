import { useCallback, useEffect, useRef, useState } from "react";
import { useChatState } from "../ChatRuntimeProvider";
import type { ChatStore } from "../store";
import { CWYC_DISMISS_EVENT } from "../hooks/useDismiss";

interface SelectedReply { messageId: string; text: string; left: number; top: number }

/** Only answer prose/code in one text part, never proposal controls or reasoning. */
function selectedReply(): SelectedReply | null {
  const selection = window.getSelection();
  if (!selection || selection.isCollapsed || selection.rangeCount !== 1) return null;
  const range = selection.getRangeAt(0);
  const parent = (node: Node) => node instanceof Element ? node : node.parentElement;
  const textPart = parent(range.startContainer)?.closest<HTMLElement>("[data-reply-text]");
  if (!textPart || !textPart.contains(range.endContainer)) return null;
  const row = textPart.closest<HTMLElement>("[data-reply-message]");
  const viewport = row?.closest<HTMLElement>(".cwyc-viewport");
  if (!row || !viewport || row.closest("[inert]")) return null;
  const text = selection.toString();
  if (!text.trim()) return null;
  const bounds = viewport.getBoundingClientRect();
  const rects = [...range.getClientRects()].filter((r) => r.width && r.height);
  const rect = rects[rects.length - 1];
  if (!rect || rect.bottom < bounds.top || rect.top > bounds.bottom) return null;
  return {
    messageId: row.dataset.replyMessage!, text,
    left: Math.max(bounds.left + 6, Math.min(rect.right - 18, bounds.right - 96)),
    top: Math.max(bounds.top + 6, Math.min(rect.bottom + 6, bounds.bottom - 38)),
  };
}

export function SelectionReply({ store }: { store: ChatStore }) {
  const { ui, messages } = useChatState(store);
  const [selection, setSelection] = useState<SelectedReply | null>(null);
  const buttonRef = useRef<HTMLButtonElement>(null);
  const attach = useCallback((candidate: SelectedReply) => {
    if (store.addReplyQuote(candidate.messageId, candidate.text)) {
      window.getSelection()?.removeAllRanges();
      window.chatUI?.focusComposer();
    }
    setSelection(null);
  }, [store]);

  useEffect(() => {
    let frame = 0;
    let dragging = false;
    const clear = () => {
      cancelAnimationFrame(frame);
      setSelection(null);
    };
    const update = () => {
      cancelAnimationFrame(frame);
      if (!dragging) frame = requestAnimationFrame(() => setSelection(selectedReply()));
    };
    const down = (event: PointerEvent) => {
      if (buttonRef.current?.contains(event.target as Node)) return;
      dragging = true;
      clear();
    };
    const up = () => { dragging = false; update(); };
    const key = (event: KeyboardEvent) => {
      if (event.key === "Escape") clear();
      if (event.key === "Enter" && event.altKey && !event.ctrlKey && !event.metaKey) {
        const candidate = selectedReply();
        if (candidate) {
          event.preventDefault();
          event.stopPropagation();
          attach(candidate);
        }
      }
    };
    document.addEventListener("selectionchange", update);
    document.addEventListener("pointerdown", down);
    document.addEventListener("pointerup", up);
    document.addEventListener("keydown", key, true);
    document.addEventListener("scroll", clear, true);
    document.addEventListener(CWYC_DISMISS_EVENT, clear);
    window.addEventListener("blur", clear);
    window.addEventListener("resize", clear);
    return () => {
      clear();
      document.removeEventListener("selectionchange", update);
      document.removeEventListener("pointerdown", down);
      document.removeEventListener("pointerup", up);
      document.removeEventListener("keydown", key, true);
      document.removeEventListener("scroll", clear, true);
      document.removeEventListener(CWYC_DISMISS_EVENT, clear);
      window.removeEventListener("blur", clear);
      window.removeEventListener("resize", clear);
    };
  }, [attach]);

  if (!selection || ui.pane !== "chat" || ui.dock?.expanded === false ||
    !messages.some((m) => m.id === selection.messageId)) return null;
  return (
    <button ref={buttonRef} type="button" className="cwyc-selection-reply"
      data-testid="reply-to-selection" aria-label="Reply to selected text"
      title="Reply to selection (Alt+Enter)" style={{ left: selection.left, top: selection.top }}
      onPointerDown={(event) => event.preventDefault()}
      onClick={() => attach(selection)}>
      <svg width="14" height="14" viewBox="0 0 16 16" fill="none" aria-hidden="true">
        <path d="M6 3 2 7l4 4M2 7h7a4 4 0 0 1 4 4v2" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
      </svg>
      Reply
    </button>
  );
}
