import { useEffect, useRef } from "react";
import type { ChatStore } from "../store";

/**
 * Ask-each-read approval (approvals.py). While this chip is pending, a tool
 * call is BLOCKED on the MCP thread: Python waits up to APPROVAL_TIMEOUT_S
 * (120s) for our answer, then denies on its own. So this must always render,
 * and it must be obvious - an unanswered chip is a stalled agent.
 *
 * Autofocuses Allow so the whole loop is keyboard-only (the mode means a lot
 * of these), and announces itself: the agent is waiting on the user, which is
 * exactly the case a screen-reader user must not miss.
 */
export function ToolApprovalChip({
  data,
  store,
}: {
  data: {
    id: string;
    tool: string;
    summary: string;
    resolved: boolean;
    allow?: boolean;
    reason?: string;
  };
  store: ChatStore;
}) {
  const allowRef = useRef<HTMLButtonElement | null>(null);
  useEffect(() => {
    if (!data.resolved) allowRef.current?.focus();
  }, [data.resolved]);

  if (data.resolved) {
    const verdict = data.allow ? "Allowed" : data.reason ? `Denied (${data.reason})` : "Denied";
    return (
      <div
        className="cwyc-approval cwyc-approval-resolved"
        data-testid="tool-approval"
        data-resolved="true"
      >
        {verdict}: <code>{data.tool}</code>
      </div>
    );
  }

  return (
    <div className="cwyc-approval" data-testid="tool-approval" role="alertdialog" aria-live="assertive">
      <div className="cwyc-approval-text">
        The assistant wants to run <code>{data.tool}</code>. This is a read — it cannot change
        your collection.
      </div>
      {/* The raw tool arguments (json.dumps(args)[:120] from __init__.py), so
          the decision is made on what the call actually does, not its name. */}
      {data.summary ? <pre className="cwyc-approval-args">{data.summary}</pre> : null}
      <div className="cwyc-approval-actions">
        <button
          type="button"
          ref={allowRef}
          className="cwyc-chip cwyc-chip-primary"
          data-testid="tool-approval-allow"
          onClick={() => store.respondToolApproval(data.id, true)}
        >
          Allow
        </button>
        <button
          type="button"
          className="cwyc-chip"
          data-testid="tool-approval-deny"
          onClick={() => store.respondToolApproval(data.id, false)}
        >
          Deny
        </button>
      </div>
    </div>
  );
}
