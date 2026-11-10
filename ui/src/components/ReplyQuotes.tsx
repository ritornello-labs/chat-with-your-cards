import { MAX_REPLY_COMMENT_CHARS, type ReplyQuote } from "../replyQuotes";

export function ReplyQuotes({ quotes, onRemove, onCommentChange }: {
  quotes: readonly ReplyQuote[];
  onRemove?: (index: number) => void;
  onCommentChange?: (index: number, comment: string) => void;
}) {
  if (!quotes.length) return null;
  return (
    <div className="cwyc-reply-quotes" data-testid={onRemove ? "reply-draft-quotes" : "sent-reply-quotes"}
      role="group" aria-label={onRemove ? "Quoted selections for your reply" : "Quoted selections"}>
      {quotes.map((quote, index) => (
        <div className="cwyc-reply-quote" key={`${quote.source_turn}:${quote.text}`}>
          <div className="cwyc-reply-quote-header">
            <span><span aria-hidden="true">↳ </span>Selection {index + 1}</span>
            {onRemove && <button type="button" className="cwyc-reply-remove"
              aria-label={`Remove quoted selection ${index + 1}`} title="Remove quote"
              onClick={() => onRemove(index)}>×</button>}
          </div>
          <blockquote>{quote.text}</blockquote>
          {onCommentChange ? (
            <textarea className="cwyc-reply-comment" data-testid="reply-comment"
              aria-label={`Your reply to selection ${index + 1}`}
              placeholder="Reply to this passage…" rows={2} maxLength={MAX_REPLY_COMMENT_CHARS}
              value={quote.comment ?? ""}
              onChange={(event) => onCommentChange(index, event.target.value)} />
          ) : quote.comment ? (
            <div className="cwyc-reply-sent-comment" data-testid="sent-reply-comment">{quote.comment}</div>
          ) : null}
        </div>
      ))}
    </div>
  );
}
