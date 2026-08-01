import type { ChatStore, GradingCardData } from "../store";

interface GradingCardProps {
  data: GradingCardData;
  store: ChatStore;
}

const STATUS_LABELS: Record<string, string> = {
  pending: "Confirmation needed",
  applying: "Applying…",
  accepted: "Applied",
  "auto-accepted": "Auto-applied",
  rejected: "Cancelled",
  failed: "Failed",
};

function cardMeta(card: GradingCardData["cards"][number]): string {
  const parts = [card.deck, card.template, `card ${card.card_id}`].filter(Boolean);
  if (card.current_deck && card.current_deck !== card.deck) {
    parts.unshift(`in ${card.current_deck}`);
  }
  return parts.join(" · ");
}

/** Which button this records (#16). Payloads from before ratings were
 *  selectable carry no `rating`, and those were always Again. */
function ratingLabel(data: GradingCardData): string {
  const rating = data.rating ?? "again";
  return rating.charAt(0).toUpperCase() + rating.slice(1);
}

function resultNotes(data: GradingCardData): string[] {
  if (data.action !== "fail" || !data.result) return [];
  const notes: string[] = [];
  const preview = data.result.preview_exits;
  const filtered = data.result.rescheduling_filtered;
  const leeches = data.result.newly_suspended;
  if (Array.isArray(preview) && preview.length) {
    notes.push(`${preview.length} preview-filtered card${preview.length === 1 ? "" : "s"} returned home first.`);
  }
  if (Array.isArray(filtered) && filtered.length) {
    notes.push(`${filtered.length} card${filtered.length === 1 ? "" : "s"} received ${ratingLabel(data)} in a rescheduling filtered deck.`);
  }
  if (Array.isArray(leeches) && leeches.length) {
    notes.push(`Anki newly suspended ${leeches.length} card${leeches.length === 1 ? "" : "s"} as leeches.`);
  }
  return notes;
}

export function GradingCard({ data, store }: GradingCardProps) {
  const pending = data.status === "pending";
  const applying = data.status === "applying";
  const applied = data.status === "accepted" || data.status === "auto-accepted";
  const makeAvailable = applied && data.action === "fail" && data.available_card_ids.length > 0;
  // "wrong" is only true of Again (#16). A Good or Easy review is still a
  // real review, so it gets neutral wording rather than a verdict it is not.
  const isAgain = (data.rating ?? "again") === "again";
  const plural = data.card_ids.length === 1 ? "" : "s";
  const title =
    data.action === "fail"
      ? isAgain
        ? `Mark ${data.card_ids.length === 1 ? "card" : "cards"} wrong`
        : `Record ${ratingLabel(data)} on ${data.card_ids.length} card${plural}`
      : "Make cards available";
  const appliedTitle =
    data.action === "fail"
      ? isAgain
        ? `${data.card_ids.length} card${plural} marked wrong`
        : `${data.card_ids.length} card${plural} graded ${ratingLabel(data)}`
      : `${data.card_ids.length} card${plural} made available`;

  return (
    <section
      className={`cwyc-grading cwyc-grading-${data.status}`}
      data-testid="grading-card"
      aria-busy={applying}
    >
      <header className="cwyc-grading-head">
        <span className="cwyc-grading-mark" aria-hidden="true">↘</span>
        <div className="cwyc-grading-title">
          <strong>{applied ? appliedTitle : title}</strong>
          <span>
            {data.action === "fail"
              ? `Uses Anki’s ${ratingLabel(data)} rating`
              : "Review history stays unchanged"}
          </span>
        </div>
        <span className="cwyc-grading-status">{STATUS_LABELS[data.status] ?? data.status}</span>
      </header>

      {data.rationale ? <p className="cwyc-grading-rationale">{data.rationale}</p> : null}

      <div className="cwyc-grading-cards">
        {data.cards.map((card) => (
          <div className="cwyc-grading-card-row" key={card.card_id}>
            <div className="cwyc-grading-prompt">
              {card.prompt_field ? <span>{card.prompt_field}</span> : null}
              {card.prompt}
            </div>
            <div className="cwyc-grading-meta">{cardMeta(card)}</div>
            {card.hidden_state ? (
              <span className="cwyc-grading-hidden">{card.hidden_state}</span>
            ) : null}
          </div>
        ))}
      </div>

      {[...data.warnings, ...resultNotes(data)].map((warning, index) => (
        <div className="cwyc-grading-warning" key={`${index}:${warning}`}>
          {warning}
        </div>
      ))}

      {data.availability ? (
        <div className="cwyc-grading-success">
          Cards are available now. The recorded failure and review history were not changed.
        </div>
      ) : null}

      {pending ? (
        <div className="cwyc-grading-actions">
          <button
            type="button"
            className="cwyc-btn-reject"
            onClick={() => store.rejectGrading(data.id)}
            data-testid="grading-reject"
          >
            Cancel
          </button>
          <button
            type="button"
            className="cwyc-btn-accept cwyc-primary"
            onClick={() => store.acceptGrading(data.id)}
            data-testid="grading-approve"
          >
            {data.action === "fail" ? "Mark wrong" : "Make available"}
          </button>
        </div>
      ) : null}

      {makeAvailable ? (
        <div className="cwyc-grading-offer">
          <span>
            {data.available_card_ids.length} card{data.available_card_ids.length === 1 ? "" : "s"} still hidden.
          </span>
          <button
            type="button"
            className="cwyc-btn-suggest"
            onClick={() => store.makeGradingCardsAvailable(data.id)}
            data-testid="grading-make-available"
          >
            Make available
          </button>
        </div>
      ) : null}
    </section>
  );
}
