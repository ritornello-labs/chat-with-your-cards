import { useState } from "react";
import { useChatState } from "../ChatRuntimeProvider";
import type { ChatStore } from "../store";
import { useDismiss } from "../hooks/useDismiss";

export const EDIT_ANNOTATION_EVENT = "cwyc:edit-annotation";

export function AnnotationSummary({ store }: { store: ChatStore }) {
  const { ui } = useChatState(store);
  const [open, setOpen] = useState(false);
  const ref = useDismiss(open, () => setOpen(false));
  if (!ui.replyQuotes.length) return null;
  const edit = (index: number) => {
    setOpen(false);
    document.dispatchEvent(new CustomEvent(EDIT_ANNOTATION_EVENT, { detail: index }));
  };
  return <div ref={ref} className="cwyc-annotation-summary" data-testid="reply-draft-quotes">
    <button type="button" className="cwyc-annotation-summary-button" data-testid="annotation-summary"
      aria-label={`Review ${ui.replyQuotes.length} annotations`} aria-expanded={open}
      onClick={() => ui.replyQuotes.length === 1 ? edit(0) : setOpen(!open)}>
      <svg width="15" height="15" viewBox="0 0 18 18" fill="none" aria-hidden="true"><path d="M4 2.5h7l3 3v10H4zM11 2.5v3h3M6.5 8h5M6.5 11h4" stroke="currentColor" strokeWidth="1.4" strokeLinejoin="round" /></svg>
      {ui.replyQuotes.length} annotation{ui.replyQuotes.length === 1 ? "" : "s"}
    </button>
    {open && <div role="menu" aria-label="Annotations" className="cwyc-annotation-list cwyc-panel">
      {ui.replyQuotes.map((quote, index) => <button type="button" role="menuitem"
        key={`${quote.source_turn}:${quote.text}`} onClick={() => edit(index)}>
        <span className="cwyc-annotation-list-number">{index + 1}</span>
        <span><span className="cwyc-annotation-excerpt">{quote.text}</span>
          <span className="cwyc-annotation-comment-preview">{quote.comment || "No comment"}</span></span>
      </button>)}
    </div>}
  </div>;
}
