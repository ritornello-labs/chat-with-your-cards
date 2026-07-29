import { useEffect, useRef, useState } from "react";
import { useChatState } from "../ChatRuntimeProvider";
import type { ChatStore } from "../store";

/**
 * Screen-reader announcements for the conversation (task #22).
 *
 * The migration dropped the transcript's `aria-live="polite"` and the vendored
 * assistant-ui primitives supply none, so a streamed reply arrived in total
 * silence: nothing said the send had landed, and nothing said an answer was
 * there.
 *
 * Putting `aria-live` back ON THE TRANSCRIPT is not the fix, which is why this
 * component exists instead. A reply streams in token by token, and a live
 * region over it re-announces the growing text on every delta - dozens of
 * interruptions per reply, each one restarting mid-sentence. `aria-relevant`
 * does not save it either: the bubble is added EMPTY and then filled, so
 * "additions" announces nothing and "text" announces everything.
 *
 * So announcements are made from SETTLED state: the turn started, the turn
 * finished (with the reply, and a count of anything now waiting on a
 * decision), it failed, or it was cancelled. That is the same information a
 * sighted user gets from watching the dock, delivered once.
 *
 * Not announced here: the ask-each-read approval chip, which owns its own
 * assertive region because it BLOCKS the agent and must interrupt.
 */

/** Long replies are announced in full up to this, then pointed at the
 *  transcript - silently truncating an answer is its own accessibility bug. */
const MAX_SPOKEN = 600;

function messageText(message: { content: unknown }): string {
  const parts = Array.isArray(message.content) ? message.content : [];
  return parts
    .filter(
      (part): part is { type: "text"; text: string } =>
        !!part && typeof part === "object" && (part as { type?: string }).type === "text"
    )
    .map((part) => part.text)
    .join("")
    .trim();
}

/** Things the user now has to decide about; easy to miss without sight. */
function pendingDecisions(message: { content: unknown }): number {
  const parts = Array.isArray(message.content) ? message.content : [];
  return parts.filter((part) => {
    if (!part || typeof part !== "object") return false;
    const typed = part as { type?: string; name?: string; data?: { status?: string } };
    if (typed.type !== "data") return false;
    if (typed.name !== "proposal" && typed.name !== "grading") return false;
    return typed.data?.status === "pending";
  }).length;
}

export function Announcer({ store }: { store: ChatStore }) {
  const { messages, isRunning } = useChatState(store);
  const [announcement, setAnnouncement] = useState("");
  // Bumped with every announcement so an identical message is spoken again:
  // replacing the node counts as a change, whereas rewriting the same string
  // into the same node does not.
  const [seq, setSeq] = useState(0);
  const wasRunning = useRef(isRunning);

  const say = (text: string) => {
    setAnnouncement(text);
    setSeq((n) => n + 1);
  };

  useEffect(() => {
    const started = isRunning && !wasRunning.current;
    const finished = !isRunning && wasRunning.current;
    wasRunning.current = isRunning;

    if (started) {
      say("Working…");
      return;
    }
    if (!finished) return;

    const last = [...messages].reverse().find((m) => m.role === "assistant");
    if (!last) return;
    const status = (last as { status?: { type?: string; reason?: string } }).status;
    if (status?.type === "incomplete") {
      say(status.reason === "cancelled" ? "Stopped." : "The reply failed.");
      return;
    }

    const text = messageText(last);
    const decisions = pendingDecisions(last);
    const waiting =
      decisions === 1
        ? " One card is waiting for your decision."
        : decisions > 1
          ? ` ${decisions} cards are waiting for your decision.`
          : "";
    if (!text) {
      say(waiting.trim() || "Reply finished.");
      return;
    }
    const spoken =
      text.length > MAX_SPOKEN
        ? `${text.slice(0, MAX_SPOKEN)}… ${text.length - MAX_SPOKEN} more characters in the transcript.`
        : text;
    say(`${spoken}${waiting}`);
  }, [isRunning, messages]);

  return (
    <div className="cwyc-sr-only" role="status" aria-live="polite" data-testid="announcer">
      <p key={seq}>{announcement}</p>
    </div>
  );
}
