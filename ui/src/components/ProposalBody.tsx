import { useState } from "react";
import { wordDiff } from "@elvis-labs/interaction-ui-react";
import type { ProposalCardData } from "../store";

/**
 * What a non-note proposal actually does.
 *
 * Bulk, delete, change-set, deck and skill proposals all used to render as the
 * string "{count} item(s)" - a number with no verb, no target, and no way to
 * see which notes were in the blast radius. proposals.py has always sent the
 * material (op, op_args, samples, items, and a unified diff for skills); the
 * React card simply never read it. Approving a 400-note find/replace on the
 * strength of "400 item(s)" is not review.
 *
 * Everything here is defensive about shape: these payloads are also replayed
 * from saved transcripts, where an older schema can turn a dict into a string.
 */

const OP_LABELS: Record<string, string> = {
  find_replace: "Find and replace",
  rename_tag: "Rename tag",
  move_cards: "Move cards",
  create_deck: "Create deck",
  rename_deck: "Rename deck",
  set_deck_options: "Deck options",
  set_deck_limits: "Deck limits",
  create_filtered_deck: "Create filtered deck",
  update_filtered_deck: "Rebuild filtered deck",
  filtered_deck_action: "Filtered deck",
  suspend_cards: "Suspend cards",
  unsuspend_cards: "Unsuspend cards",
  bury_cards: "Bury cards",
  unbury_cards: "Unbury cards",
  set_card_flag: "Flag cards",
  add_tags: "Add tags",
  remove_tags: "Remove tags",
  clear_unused_tags: "Clear unused tags",
  set_due_date: "Set due date",
  forget_cards: "Forget cards",
  reposition_new_cards: "Reposition new cards",
  undo_change: "Undo",
  check_database: "Check Database",
  sync_now: "Sync",
  delete_deck: "Delete deck",
  manage_preset: "Options preset",
  assign_preset: "Assign preset",
  set_deck_description: "Deck description",
  saved_search: "Saved search",
  set_note_type_styling: "Note-type CSS",
  set_card_template: "Card template",
  manage_note_type_fields: "Note-type fields",
  manage_card_templates: "Card templates",
  create_note_type: "New note type",
  change_note_type: "Change note type",
  remove_empty_cards: "Empty cards",
  set_preferences: "Preferences",
  import_csv: "CSV import",
};

/** What `count` counts - "400 item(s)" says less than nothing. Keyed by op,
 *  then by kind for the proposals that carry no op. */
const COUNT_NOUNS: Record<string, [string, string]> = {
  move_cards: ["card", "cards"],
  suspend_cards: ["card", "cards"],
  unsuspend_cards: ["card", "cards"],
  bury_cards: ["card", "cards"],
  unbury_cards: ["card", "cards"],
  set_card_flag: ["card", "cards"],
  add_tags: ["note", "notes"],
  remove_tags: ["note", "notes"],
  clear_unused_tags: ["tag", "tags"],
  set_due_date: ["card", "cards"],
  forget_cards: ["card", "cards"],
  reposition_new_cards: ["card", "cards"],
  set_deck_limits: ["change", "changes"],
  filtered_deck_action: ["deck", "decks"],
  import_csv: ["row", "rows"],
  manage_card_templates: ["note", "notes"],
  change_note_type: ["note", "notes"],
  remove_empty_cards: ["card", "cards"],
  set_preferences: ["setting", "settings"],
  find_replace: ["note", "notes"],
  rename_tag: ["note", "notes"],
  // skill_update counts the OBSERVED EDITS behind the suggestion, not notes.
  skill_update: ["edit", "edits"],
  delete: ["note", "notes"],
  change_set: ["note", "notes"],
};

const DEFAULT_NOUN: [string, string] = ["note", "notes"];
/** Rows shown before collapsing. Long enough to see the pattern, short enough
 *  not to bury the Approve button under 200 rows. */
const VISIBLE_ROWS = 8;

interface TextSample {
  text: string;
}
interface DiffSample {
  label: string;
  old: string;
  new: string;
}
interface ItemRow {
  note_id?: number;
  label?: string;
  fields?: string[];
  /** Generic-op batch items (#27). */
  index?: number;
  op?: string;
  risk?: string;
  revert?: string;
}

function asTextSample(value: unknown): TextSample | null {
  if (typeof value === "string") return { text: value };
  if (value && typeof value === "object" && typeof (value as TextSample).text === "string") {
    return value as TextSample;
  }
  return null;
}

function asDiffSample(value: unknown): DiffSample | null {
  if (!value || typeof value !== "object") return null;
  const sample = value as DiffSample;
  return typeof sample.old === "string" && typeof sample.new === "string" ? sample : null;
}

function asItemRow(value: unknown): ItemRow | null {
  return value && typeof value === "object" ? (value as ItemRow) : null;
}

function countLabel(count: number, op: string, kind: string): string {
  const [one, many] = COUNT_NOUNS[op] ?? COUNT_NOUNS[kind] ?? DEFAULT_NOUN;
  return `${count} ${count === 1 ? one : many}`;
}

/** A list that admits how much it is hiding, rather than truncating silently. */
function Collapsible({
  children,
  total,
  noun,
}: {
  children: React.ReactNode[];
  total: number;
  noun: string;
}) {
  const [expanded, setExpanded] = useState(false);
  const hidden = total - VISIBLE_ROWS;
  const shown = expanded ? children : children.slice(0, VISIBLE_ROWS);
  return (
    <>
      {shown}
      {hidden > 0 ? (
        <button
          type="button"
          className="cwyc-proposal-more"
          data-testid="proposal-body-more"
          onClick={() => setExpanded((value) => !value)}
        >
          {expanded ? "Show less" : `Show ${hidden} more ${noun}`}
        </button>
      ) : null}
    </>
  );
}

function WordDiff({ before, after }: { before: string; after: string }) {
  return (
    <span className="eui-field-diff">
      {wordDiff(before, after).map((part, i) => {
        if (part.op === "eq") return <span key={i}>{part.text}</span>;
        const Tag = part.op === "del" ? "del" : "ins";
        return (
          <Tag key={i} className={`eui-diff-${part.op}`}>
            {part.text}
          </Tag>
        );
      })}
    </span>
  );
}

/**
 * Claim the sample belonging to `label`, once. Python builds samples inside
 * the same loop as items, so they line up - but matching by label rather than
 * index means a future reorder degrades to "no diff shown" instead of
 * attaching one note's diff to another note's name.
 */
function takeSample(pool: DiffSample[], label: string | undefined): DiffSample | null {
  if (!label) return null;
  const index = pool.findIndex((sample) => sample.label === label);
  if (index < 0) return null;
  return pool.splice(index, 1)[0];
}

/** The unified diff proposals.py already builds for a skill update. */
function SkillDiff({ diff }: { diff: string }) {
  return (
    <pre className="cwyc-proposal-diff" data-testid="proposal-skill-diff">
      {diff.split("\n").map((line, i) => {
        const kind = line.startsWith("+++") || line.startsWith("---")
          ? "meta"
          : line.startsWith("@@")
            ? "hunk"
            : line.startsWith("+")
              ? "ins"
              : line.startsWith("-")
                ? "del"
                : "ctx";
        return (
          <span className={`cwyc-diff-line cwyc-diff-${kind}`} key={i}>
            {line || " "}
            {"\n"}
          </span>
        );
      })}
    </pre>
  );
}

export function ProposalBody({
  data,
  excludedItems,
  onToggleItem,
}: {
  data: ProposalCardData;
  /** Batch per-item reject (#27): indices unchecked at review; only passed
   *  for a PENDING change set, so resolved cards render read-only rows. */
  excludedItems?: ReadonlySet<number>;
  onToggleItem?: (index: number) => void;
}) {
  const samples = Array.isArray(data.samples) ? data.samples : [];
  const items = Array.isArray(data.items) ? data.items : [];
  // No title fallback: the card header already shows the title, and a
  // second copy here reads as two different facts.
  const opLabel = OP_LABELS[data.op] ?? "";
  const diff = typeof data.op_args?.diff === "string" ? (data.op_args.diff as string) : "";

  // find_replace samples carry old/new, so the change reads as a change.
  const diffSamples = samples.map(asDiffSample).filter((s): s is DiffSample => s !== null);
  const textSamples = samples.map(asTextSample).filter((s): s is TextSample => s !== null);
  const rows = items.map(asItemRow).filter((row): row is ItemRow => row !== null);

  return (
    <div className="cwyc-proposal-body" data-testid="proposal-body">
      <div className="cwyc-proposal-summary">
        {opLabel ? <span className="cwyc-proposal-op">{opLabel}</span> : null}
        {data.count ? (
          <span className="cwyc-proposal-count">{countLabel(data.count, data.op, data.kind)}</span>
        ) : null}
      </div>

      {/* ONE list of affected notes. Python samples only the first few
          (MAX_SAMPLES) but sends every item, so a note with a sample shows its
          diff inline and the rest still show up by name - rendering samples
          and items as two lists repeated the same note twice. */}
      {rows.length > 0 ? (
        <ul className="cwyc-proposal-items" data-testid="proposal-items">
          <Collapsible total={rows.length} noun="notes">
            {rows.map((row, i) => {
              const sample = takeSample(diffSamples, row.label);
              const index = typeof row.index === "number" ? row.index : i;
              const excluded = excludedItems?.has(index) ?? false;
              return (
                <li
                  className={
                    "cwyc-proposal-item" +
                    (sample ? " cwyc-proposal-item-wide" : "") +
                    (excluded ? " cwyc-proposal-item-excluded" : "")
                  }
                  key={row.note_id ?? `${row.op ?? "item"}-${index}`}
                >
                  <span className="cwyc-proposal-item-head">
                    {onToggleItem ? (
                      <input
                        type="checkbox"
                        className="cwyc-item-include"
                        checked={!excluded}
                        aria-label={`Include ${row.label || row.op || "item"}`}
                        data-testid={`item-include-${index}`}
                        onChange={() => onToggleItem(index)}
                      />
                    ) : null}
                    <span className="cwyc-proposal-item-label" title={row.label}>
                      {row.label || (row.note_id ? `Note ${row.note_id}` : row.op)}
                    </span>
                    {row.fields?.length ? (
                      <span className="cwyc-proposal-item-fields">{row.fields.join(", ")}</span>
                    ) : null}
                    {row.risk ? (
                      <span className="cwyc-proposal-item-risk">{row.risk}</span>
                    ) : null}
                  </span>
                  {row.revert ? (
                    <span className="cwyc-proposal-item-revert">{row.revert}</span>
                  ) : null}
                  {sample ? <WordDiff before={sample.old} after={sample.new} /> : null}
                </li>
              );
            })}
          </Collapsible>
        </ul>
      ) : diffSamples.length > 0 ? (
        <div className="cwyc-proposal-samples">
          <Collapsible total={diffSamples.length} noun="examples">
            {diffSamples.map((sample, i) => (
              <div className="cwyc-proposal-sample" key={i}>
                {sample.label ? (
                  <div className="cwyc-proposal-sample-label">{sample.label}</div>
                ) : null}
                <WordDiff before={sample.old} after={sample.new} />
              </div>
            ))}
          </Collapsible>
        </div>
      ) : textSamples.length > 0 ? (
        <ul className="cwyc-proposal-samples">
          <Collapsible total={textSamples.length} noun="lines">
            {textSamples.map((sample, i) => (
              <li className="cwyc-proposal-sample-line" key={i}>
                {sample.text}
              </li>
            ))}
          </Collapsible>
        </ul>
      ) : null}

      {diff ? <SkillDiff diff={diff} /> : null}

      {/* Only reachable when Python sent an operation with nothing to show;
          say so rather than rendering an empty box. */}
      {!textSamples.length && !diffSamples.length && !rows.length && !diff ? (
        <div className="cwyc-proposal-sample-line">{countLabel(data.count, data.op, data.kind)} affected</div>
      ) : null}
    </div>
  );
}
