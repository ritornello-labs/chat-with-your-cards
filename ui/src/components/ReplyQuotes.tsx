import type { ReplyQuote } from "../replyQuotes";

export function ReplyQuotes({ quotes, onRemove }: {
  quotes: readonly ReplyQuote[];
  onRemove?: (index: number) => void;
}) {
  if (!quotes.length) return null;
  return (
    <div className="cwyc-reply-quotes" data-testid={onRemove ? "reply-draft-quotes" : "sent-reply-quotes"}
      role="group" aria-label={onRemove ? "Quoted selections for your reply" : "Quoted selections"}>
      {quotes.map((quote, index) => (
        <div className="cwyc-reply-quote" key={`${quote.source_turn}:${quote.text}`}>
          <div className="cwyc-reply-quote-header">
            <span><span aria-hidden="true">↳ </span>Replying to {quotes.length > 1 ? `selection ${index + 1}` : "selection"}</span>
            {onRemove && <button type="button" className="cwyc-reply-remove"
              aria-label={`Remove quoted selection ${index + 1}`} title="Remove quote"
              onClick={() => onRemove(index)}>×</button>}
          </div>
          <blockquote>{quote.text}</blockquote>
        </div>
      ))}
    </div>
  );
}
