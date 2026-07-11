/**
 * JS <-> Python bridge, mirroring chat_with_your_cards/bridge.py exactly so
 * the Python side needed zero changes to load this UI in place of the old
 * hand-rolled vanilla-JS UI:
 *
 *   JS -> Python: pycmd("cwyc:" + JSON.stringify({type, ...}))
 *   Python -> JS: window.chatUI.dispatch(payload)   (bridge.push, evaluated)
 *   Python -> JS: window.chatUI.ackReady()           (dock.py's ready handshake, evaluated once)
 *
 * This module only implements the *transport*: postCommand() always talks to
 * window.pycmd, and installChatUI() always installs the window.chatUI global
 * the Python side (dock.py / __init__.py) drives. It has no opinion about
 * where events end up (that is
 * store.ts) and no opinion about who answers window.pycmd:
 *
 *   - Real mode (Anki): window.pycmd is provided by AnkiWebView. Nothing else
 *     to do - postCommand() reaches Python directly.
 *   - Dev mode (npm run dev): src/dev/replayer.ts installs a fake
 *     window.pycmd before the app mounts, so the exact same postCommand()
 *     call path is exercised end-to-end against scripted data instead of a
 *     live backend. See src/dev-main.tsx.
 */

import type { BridgeCommand } from "./events";

const PREFIX = "cwyc:";

declare global {
  interface Window {
    pycmd?: (raw: string) => unknown;
    chatUI?: {
      dispatch: (payload: unknown) => void;
      ackReady: () => void;
      focusComposer: () => void;
    };
  }
}

/** JS -> Python. No-op (with a console warning) if no host has wired pycmd. */
export function postCommand(cmd: BridgeCommand): void {
  if (typeof window.pycmd === "function") {
    window.pycmd(PREFIX + JSON.stringify(cmd));
    return;
  }
  // eslint-disable-next-line no-console
  console.warn("cwyc: window.pycmd is not wired; dropped command", cmd);
}

export interface ChatUIHandlers {
  dispatch: (payload: unknown) => void;
  focusComposer: () => void;
}

export interface ChatUIHandle {
  isAcked: () => boolean;
}

/**
 * Installs window.chatUI, matching app.js's shape
 * ({dispatch, ackReady, focusComposer}) field-for-field. Safe to call before
 * React mounts - dispatch/focusComposer are stable callbacks the caller
 * supplies (typically store.ts's dispatch, which buffers into external
 * state regardless of whether anything has subscribed yet).
 */
export function installChatUI(handlers: ChatUIHandlers): ChatUIHandle {
  let acked = false;
  window.chatUI = {
    dispatch: handlers.dispatch,
    ackReady: () => {
      acked = true;
    },
    focusComposer: handlers.focusComposer,
  };
  return { isAcked: () => acked };
}

/**
 * Mirrors app.js's pingReadyUntilAcked(): posts {type:"ready"} on a retry
 * loop every 250ms (up to 40 attempts) until Python acks via
 * window.chatUI.ackReady(). pycmd may not be wired the instant this script
 * runs, so the retry - not a single ping - is what makes the handshake
 * reliable.
 */
export function startReadyHandshake(isAcked: () => boolean, maxAttempts = 40): void {
  let attempts = 0;
  const tick = () => {
    if (isAcked() || attempts > maxAttempts) {
      return;
    }
    attempts += 1;
    postCommand({ type: "ready" });
    window.setTimeout(tick, 250);
  };
  tick();
}
