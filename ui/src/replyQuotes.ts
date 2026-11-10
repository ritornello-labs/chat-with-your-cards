/** Selected answer excerpts. Text is plain text, never HTML or instructions. */
export interface ReplyQuote {
  readonly text: string;
  /** Ordinal user turn in this chat; survives replay unlike DOM message IDs. */
  readonly source_turn: number;
}

export const MAX_REPLY_QUOTES = 8;
export const MAX_REPLY_QUOTE_CHARS = 8000;

export function readReplyQuotes(value: unknown): ReplyQuote[] {
  if (!Array.isArray(value)) return [];
  return value.slice(0, MAX_REPLY_QUOTES).flatMap((item: unknown) => {
    if (!item || typeof item !== "object") return [];
    const q = item as Partial<ReplyQuote>;
    return typeof q.text === "string" && q.text.trim() && q.text.length <= MAX_REPLY_QUOTE_CHARS &&
      Number.isInteger(q.source_turn) && Number(q.source_turn) >= 0
      ? [{ text: q.text, source_turn: Number(q.source_turn) }]
      : [];
  });
}
