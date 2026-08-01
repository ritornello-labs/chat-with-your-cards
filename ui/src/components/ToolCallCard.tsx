import type { ToolCallMessagePartProps } from "@assistant-ui/react";
import type { ToolCallResult } from "../store";

/**
 * Generic collapsible tool-call card, registered as MessagePrimitive.Parts'
 * `tools.Fallback` (src/App.tsx) so it renders every tool name uniformly.
 * assistant-ui derives `status` for us (see store.ts's header comment):
 * running while unresolved and the message is running, complete the instant
 * `result` is set - we only need to read it, not compute it.
 *
 * Friendly labels + internal-tool hiding (#23e), ported from app.js: raw
 * MCP names map to human phrases and plumbing calls the user cannot act on
 * (ToolSearch, structured-output glue) render nothing at all. The raw name
 * stays visible inside the expanded details, so debugging loses nothing.
 */
const HIDDEN_TOOLS = new Set(["ToolSearch", "StructuredOutput", "TodoWrite"]);

const TOOL_LABELS: Record<string, string> = {
  search_notes: "Searched your cards",
  find_cards: "Found matching cards",
  get_note: "Read a note",
  get_card: "Read a card",
  get_collection_overview: "Read your collection overview",
  deck_tree: "Read your deck tree",
  tag_tree: "Read your tags",
  collection_stats: "Read collection totals",
  list_note_types: "Listed note types",
  get_note_type: "Read a note type's templates",
  find_related: "Looked for related cards",
  get_card_images: "Looked at card images",
  get_card_sources: "Read a card's sources",
  show_image: "Showed an image",
  get_study_stats: "Measured your study stats",
  get_deck_due_counts: "Counted what's due per deck",
  get_due_forecast: "Forecast your review load",
  get_card_history: "Read a card's review history",
  find_duplicates: "Looked for duplicates",
  check_media: "Checked your media folder",
  get_undo_status: "Checked Anki's undo queue",
  get_sync_status: "Checked sync status",
  create_backup_now: "Wrote a backup",
  undo_last_change: "Proposed an undo",
  check_database: "Proposed a database check",
  sync_now: "Proposed a sync",
  open_browse: "Opened Browse",
  list_saved_searches: "Read saved searches",
  manage_saved_search: "Proposed a saved-search change",
  preview_csv_import: "Previewed a CSV file",
  import_csv_file: "Proposed a CSV import",
  export_csv: "Exported notes as text",
  export_apkg: "Exported a deck package",
  propose_note: "Proposed a new note",
  propose_note_edit: "Proposed a note edit",
  rename_tag: "Proposed a tag rename",
  find_replace: "Proposed find & replace",
  move_cards: "Proposed moving cards",
  delete_notes: "Proposed deleting notes",
  open_change_set: "Started a change set",
  add_to_change_set: "Added to the change set",
  close_change_set: "Handed the change set over",
  suspend_cards: "Proposed suspending cards",
  unsuspend_cards: "Proposed unsuspending cards",
  bury_cards: "Proposed burying cards",
  unbury_cards: "Proposed unburying cards",
  set_card_flag: "Proposed flagging cards",
  add_tags: "Proposed adding tags",
  remove_tags: "Proposed removing tags",
  clear_unused_tags: "Proposed clearing unused tags",
  set_due_date: "Proposed new due dates",
  forget_cards: "Proposed resetting cards",
  reposition_new_cards: "Proposed repositioning new cards",
  create_deck: "Proposed a new deck",
  rename_deck: "Proposed a deck rename",
  delete_deck: "Proposed deleting a deck",
  set_deck_options: "Proposed deck options",
  set_deck_limits: "Proposed deck limits",
  manage_options_preset: "Proposed a preset change",
  assign_options_preset: "Proposed a preset assignment",
  set_deck_description: "Proposed a deck description",
  create_filtered_deck: "Proposed a filtered deck",
  update_filtered_deck: "Proposed filtered-deck changes",
  filtered_deck_action: "Proposed a filtered-deck rebuild",
  store_media_asset: "Proposed storing a media file",
  set_note_type_styling: "Proposed note-type CSS",
  set_card_template: "Proposed a template change",
  manage_note_type_fields: "Proposed a field change",
  manage_card_templates: "Proposed a card-template change",
  create_note_type: "Proposed a new note type",
  change_note_type: "Proposed converting notes",
  remove_empty_cards: "Proposed removing empty cards",
  defer_card: "Set the card aside",
  undefer_card: "Brought a card back",
};

/** Strip the MCP transport prefix ("mcp__anki__search_notes" and friends);
 *  bare names pass through. */
function bareToolName(name: string): string {
  const match = /^mcp__[^_]+(?:_[^_]+)*__(.+)$/.exec(name);
  return match ? match[1] : name;
}

function friendlyLabel(name: string): string {
  const bare = bareToolName(name);
  return TOOL_LABELS[bare] ?? bare.replace(/_/g, " ");
}

export function ToolCallCard(props: ToolCallMessagePartProps) {
  const { toolName, argsText, result, isError, status } = props;
  const running = status.type === "running";
  const typedResult = result as ToolCallResult | undefined;
  const finished = typedResult !== undefined;
  if (HIDDEN_TOOLS.has(bareToolName(toolName))) return null;

  return (
    <div
      className={
        "cwyc-tool-chip" +
        (running ? " cwyc-tool-running" : finished ? (isError ? " cwyc-tool-failed" : " cwyc-tool-ok") : "")
      }
      data-testid="tool-chip"
    >
      <details>
        <summary>
          <CaretIcon />
          <span className="cwyc-tool-name">{friendlyLabel(toolName)}</span>
          {running ? <span className="cwyc-ember cwyc-tool-result" aria-label="running" /> : null}
          {finished ? (
            <span className="cwyc-tool-result">{isError ? "✗ failed" : "✓ ok"}</span>
          ) : null}
        </summary>
        <div className="cwyc-tool-detail-block">
          <div className="cwyc-tool-detail-label">Tool</div>
          <pre className="cwyc-tool-detail-body">{toolName}</pre>
        </div>
        {argsText ? (
          <div className="cwyc-tool-detail-block">
            <div className="cwyc-tool-detail-label">Input</div>
            <pre className="cwyc-tool-detail-body">{argsText}</pre>
          </div>
        ) : null}
        {finished && typedResult?.summary ? (
          <div className="cwyc-tool-detail-block">
            <div className="cwyc-tool-detail-label">{isError ? "Error" : "Result"}</div>
            <pre className="cwyc-tool-detail-body">{typedResult.summary}</pre>
          </div>
        ) : null}
      </details>
    </div>
  );
}

function CaretIcon() {
  return (
    <svg
      className="cwyc-tool-caret"
      viewBox="0 0 8 8"
      width="8"
      height="8"
      aria-hidden="true"
    >
      <path d="M2.5 1l3 3-3 3" stroke="currentColor" strokeWidth="1.4" fill="none" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}
