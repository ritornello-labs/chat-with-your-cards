import { useEffect, useRef, useState } from "react";
import type { ChatStore } from "../store";

/**
 * Ask-each-read approval (approvals.py). The tool call that raised this chip
 * blocks for at most APPROVAL_GRACE_S (45s) and then reports back that it did
 * NOT run - but the chip stays answerable until `expiresAtMs` (config
 * `approval_timeout_minutes`, default 5), and an Allow inside that window is
 * remembered so the agent's next attempt goes through. So this must always
 * render, and it must be obvious: an unanswered chip is a stalled agent.
 *
 * The countdown is the honest part. Without it the chip looked answerable
 * forever, which is how abandoned prompts turned into the assistant re-raising
 * them hours later (dogfood 2026-07-23).
 *
 * Autofocuses Allow so the whole loop is keyboard-only (the mode means a lot
 * of these), and announces itself: the agent is waiting on the user, which is
 * exactly the case a screen-reader user must not miss.
 */

function formatRemaining(ms: number): string {
  const total = Math.max(0, Math.ceil(ms / 1000));
  return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, "0")}`;
}

/** Milliseconds left, or null when this prompt never expires / is settled. */
function useRemaining(expiresAtMs: number | undefined, active: boolean): number | null {
  const [remaining, setRemaining] = useState<number | null>(() =>
    expiresAtMs && active ? expiresAtMs - Date.now() : null,
  );
  useEffect(() => {
    if (!expiresAtMs || !active) {
      setRemaining(null);
      return;
    }
    const tick = () => setRemaining(expiresAtMs - Date.now());
    tick();
    const timer = window.setInterval(tick, 1000);
    return () => window.clearInterval(timer);
  }, [expiresAtMs, active]);
  return remaining;
}

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
    late?: boolean;
    expiresAtMs?: number;
  };
  store: ChatStore;
}) {
  const allowRef = useRef<HTMLButtonElement | null>(null);
  const remaining = useRemaining(data.expiresAtMs, !data.resolved);
  // Python is the authority on expiry; this only stops us offering buttons
  // that we know have stopped doing anything.
  const expired = remaining !== null && remaining <= 0;
  const settled = data.resolved || expired;

  useEffect(() => {
    if (!settled) allowRef.current?.focus();
  }, [settled]);

  if (settled) {
    // "expired" is NOT a refusal - the user never answered - so it must not
    // read as one. Anything else that carries a reason is a genuine denial
    // with a cause (e.g. session ended).
    const verdict =
      expired || data.reason === "expired"
        ? "Expired, no answer"
        : data.allow
          ? "Allowed"
          : data.reason
            ? `Denied (${data.reason})`
            : "Denied";
    return (
      <div
        className="cwyc-approval cwyc-approval-resolved"
        data-testid="tool-approval"
        data-resolved="true"
        data-late={data.late ? "true" : undefined}
      >
        {/* A late approval cannot resume the call that gave up, so the store
            posts a visible "go ahead" message on the user's behalf. That
            message sits right below this chip and explains itself, so no hint
            belongs here - and telling the user to "ask again" would now be
            actively wrong. */}
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
        {remaining !== null ? (
          // aria-hidden: this text changes every second and the container is
          // an assertive live region, so announcing it would talk over
          // everything. The static label below carries the same fact once.
          <span className="cwyc-approval-countdown" data-testid="tool-approval-countdown">
            <span aria-hidden="true">expires in {formatRemaining(remaining)}</span>
            <span className="cwyc-sr-only">this request expires if left unanswered</span>
          </span>
        ) : null}
      </div>
    </div>
  );
}
