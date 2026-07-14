import { useEffect, useRef } from "react";

/**
 * Broadcast that closes every open popover at once. The store dispatches it on
 * document when the dock collapses (store.ts dock_state handler) so a menu left
 * open can't survive a collapse/re-expand - the bug where you could reopen the
 * dock with Settings still showing (dogfood 2026-07-14).
 */
export const CWYC_DISMISS_EVENT = "cwyc:dismiss-popovers";

export function dismissAllPopovers(): void {
  if (typeof document !== "undefined") {
    document.dispatchEvent(new Event(CWYC_DISMISS_EVENT));
  }
}

/**
 * The classic menu-dismiss contract, shared by every composer/header popover.
 * Closes on:
 *  - outside mousedown (a click anywhere else inside our webview),
 *  - Escape,
 *  - window `blur` - the ONLY trace of a click that lands outside our webview
 *    entirely (Anki's reviewer/deck-browser is a separate QtWebEngine view, so
 *    our document never sees that mousedown; the dock webview just loses focus),
 *  - the app-wide dismiss broadcast (dock collapse; see CWYC_DISMISS_EVENT).
 */
export function useDismiss<T extends HTMLElement = HTMLDivElement>(
  open: boolean,
  close: () => void
) {
  const ref = useRef<T | null>(null);
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
    window.addEventListener("blur", close);
    document.addEventListener(CWYC_DISMISS_EVENT, close);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
      window.removeEventListener("blur", close);
      document.removeEventListener(CWYC_DISMISS_EVENT, close);
    };
  }, [open, close]);
  return ref;
}
