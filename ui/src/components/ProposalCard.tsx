import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useThreadRuntime } from "@assistant-ui/react";
import { InteractionCard, wordDiff } from "@elvis-labs/interaction-ui-react";
import "@elvis-labs/interaction-ui-react/styles.css";
import type { ChatStore, ProposalCardData } from "../store";
import { useChatState } from "../ChatRuntimeProvider";
import { createProposalInteraction } from "../interactionAdapter";
import { ComboBox } from "./ComboBox";
import { TagChips } from "./TagChips";
import { ProposalBody } from "./ProposalBody";
import { ProposalTagDiff } from "./ProposalTags";
import { ProposalActions } from "./ProposalActions";
import { VimTextArea } from "./VimTextArea";

/**
 * "Suggest change": seed the composer with a reference to this proposal and
 * arm the supersede, so the old card is set aside the moment the user actually
 * asks for something different. Prefilled rather than sent, because the user
 * has to say what they want changed - and until they do, the proposal is still
 * the live offer.
 */
function useSuggestChange(store: ChatStore) {
  // The THREAD composer, explicitly. useComposerRuntime() resolves to the
  // nearest composer, and inside a message part that is the message's own
  // EDIT composer - setText there updates a composer nobody is looking at.
  const thread = useThreadRuntime();
  return (data: ProposalCardData) => {
    const what =
      data.kind === "create" || data.kind === "edit"
        ? data.fields?.[0]?.new || data.note_type
        : data.title || KIND_LABELS[data.kind] || data.kind;
    thread.composer.setText(`About the proposed ${KIND_LABELS[data.kind] ?? data.kind} ("${String(what).slice(0, 60)}"), please `);
    // ComposerRuntime has setText but no focus(); the same selector main.tsx
    // uses covers both composer faces (plain textarea and the vim editor).
    document
      .querySelector<HTMLElement>(".cwyc-composer-input, .cwyc-vim-editor .cm-content")
      ?.focus();
    store.markForSupersede(data.id);
  };
}

/** Debounce before asking Python to re-render the preview from a draft. Same
 *  400ms the classic card used: long enough that typing a word does not fire
 *  a render per keystroke, short enough to feel live. */
const PREVIEW_DEBOUNCE_MS = 400;

const KIND_LABELS: Record<string, string> = {
  create: "New note",
  edit: "Edit note",
  bulk: "Bulk operation",
  delete: "Delete notes",
  change_set: "Change set",
  deck_op: "Deck change",
  skill_update: "Skill update",
};

const STATUS_LABELS: Record<string, string> = {
  pending: "Pending review",
  accepted: "Accepted",
  "auto-accepted": "Applied",
  rejected: "Rejected",
  undone: "Undone",
  superseded: "Superseded",
};

/**
 * Renders {type:"data", name:"proposal"} parts (the `proposal` ChatEvent -
 * proposals.py's Proposal.to_payload(), the same dict app.js's
 * renderProposal() consumes). Approve/Edit/Reject post the identical bridge
 * commands app.js sends (proposal_accept / proposal_reject), so the Python
 * side (ProposalManager.accept/reject) needs no changes.
 *
 * Only "create" and "edit" get field-level EDITING; the other kinds
 * (bulk/delete/change_set/deck_op/skill_update) have no per-field concept to
 * edit. They are not silent about what they do, though - ProposalBody.tsx
 * renders the operation, the affected notes, and the diffs (task #20d, which
 * replaced the "{count} item(s)" placeholder).
 */
interface PreviewSide {
  question?: string | null;
  answer?: string | null;
  css?: string | null;
}

interface PreviewsPayload {
  before?: PreviewSide | null;
  after?: PreviewSide | null;
}

/** Narrow ProposalPayload's `previews: unknown` (proposals.py's shape). */
function asPreviews(value: unknown): PreviewsPayload | null {
  if (!value || typeof value !== "object") return null;
  const previews = value as PreviewsPayload;
  return previews.before || previews.after ? previews : null;
}

/**
 * Same srcdoc app.js's previewSrcdoc() builds: the real card CSS in a
 * sandboxed (script-less) iframe, with Anki's night-mode classes mirrored
 * onto the preview body so night-aware templates render correctly.
 */
function previewSrcdoc(side: string, css: string | null | undefined): string {
  const night =
    document.documentElement.classList.contains("night-mode") ||
    document.body.classList.contains("nightMode") ||
    document.body.classList.contains("night-mode");
  return (
    "<!doctype html><html><head><meta charset='utf-8'><style>" +
    (css || "") +
    "\nhtml{overflow:auto;}body{margin:10px;}" +
    '</style></head><body class="card' +
    (night ? " nightMode night_mode" : "") +
    '">' +
    side +
    "</body></html>"
  );
}

/**
 * The rendered-card preview as a physical flashcard: two faces (Front/Back
 * for creations, Before/After answer sides for edits - same face semantics
 * as app.js's buildPreviewTabs) on a 3D CSS flip (perspective + rotateY,
 * 350ms; instant under prefers-reduced-motion via styles.css's global
 * reduced-motion guard). Edits default to the interesting side (After);
 * creations start front-up like a real card on the desk.
 */
function PreviewFlip({ previews }: { previews: PreviewsPayload }) {
  const faces = useMemo(() => {
    if (previews.before && previews.after) {
      return {
        labels: ["Before", "After"] as const,
        front: previewSrcdoc(previews.before.answer ?? "", previews.before.css),
        back: previewSrcdoc(previews.after.answer ?? "", previews.after.css),
        defaultFlipped: true,
      };
    }
    if (previews.after) {
      return {
        labels: ["Front", "Back"] as const,
        front: previewSrcdoc(previews.after.question ?? "", previews.after.css),
        back: previewSrcdoc(previews.after.answer ?? "", previews.after.css),
        defaultFlipped: false,
      };
    }
    return null;
  }, [previews]);
  const [flippedOverride, setFlippedOverride] = useState<boolean | null>(null);
  // Per-face scroll positions, preserved across tab toggles AND across iframe
  // reloads (a proposal update replaces srcDoc, which resets the document).
  // Captured continuously via a scroll listener installed on load; restored on
  // every load. Requires sandbox="allow-same-origin" - still script-LESS (no
  // allow-scripts), so the card HTML cannot run code; same-origin only lets
  // *us* reach the document to save/restore scrollTop. (dogfood 2026-07-11)
  const scrollPos = useRef<[number, number]>([0, 0]);
  // Stable ref callbacks: an inline one changes identity every render, so
  // React detaches (ref(null)) and re-attaches on each re-render for no gain.
  const wireScrollPersistence = useCallback(
    (index: 0 | 1) => (frame: HTMLIFrameElement | null) => {
      if (!frame) return;
      frame.onload = () => {
        const doc = frame.contentDocument;
        if (!doc) return;
        const el = doc.scrollingElement ?? doc.documentElement;
        el.scrollTop = scrollPos.current[index];
        doc.addEventListener(
          "scroll",
          () => {
            scrollPos.current[index] = el.scrollTop;
          },
          { passive: true }
        );
      };
    },
    []
  );
  if (!faces) return null;
  const flipped = flippedOverride ?? faces.defaultFlipped;

  return (
    <div className="cwyc-preview">
      <div className="cwyc-preview-tabs">
        <button
          type="button"
          className={"cwyc-preview-tab" + (flipped ? "" : " cwyc-active")}
          onClick={() => setFlippedOverride(false)}
        >
          {faces.labels[0]}
        </button>
        <button
          type="button"
          className={"cwyc-preview-tab" + (flipped ? " cwyc-active" : "")}
          onClick={() => setFlippedOverride(true)}
        >
          {faces.labels[1]}
        </button>
      </div>
      <div className="cwyc-flip">
        <div className={"cwyc-flip-inner" + (flipped ? " cwyc-flipped" : "")}>
          <div className="cwyc-flip-face">
            <iframe
              sandbox="allow-same-origin"
              title={faces.labels[0]}
              srcDoc={faces.front}
              ref={wireScrollPersistence(0)}
            />
          </div>
          <div className="cwyc-flip-face cwyc-flip-face-back">
            <iframe
              sandbox="allow-same-origin"
              title={faces.labels[1]}
              srcDoc={faces.back}
              ref={wireScrollPersistence(1)}
            />
          </div>
        </div>
      </div>
    </div>
  );
}

interface ProposalCardProps {
  /**
   * Deliberately NOT typed via assistant-ui's DataMessagePartProps<T>: that
   * type intersects MessagePartState (whose ThreadAssistantMessagePart union
   * already contains an untyped `DataMessagePart<any>` member) with our own
   * `DataMessagePart<ProposalCardData>`, which collapses `data` back to
   * `any` instead of `ProposalCardData`. The `data`/`store` fields below are
   * all this component actually needs, spread in from the `by_name.proposal`
   * wrapper registered in Thread.tsx.
   */
  data: ProposalCardData;
  store: ChatStore;
}

/**
 * Where a new note lands: deck and tags, both editable on the card.
 *
 * Both were read-only text after the migration, which quietly made the card
 * worse than the tool it replaced - the assistant guesses a deck, and the one
 * moment you can cheaply correct that guess is while you are already looking
 * at the note. `_accept_create` has always honoured a `deck`/`tags` override on
 * the accept message; only the control was missing.
 *
 * Held locally rather than round-tripped through `proposal_revise`: neither
 * value changes what the card renders, so a revision bump would cost a
 * re-render and a preview refresh to show nothing new.
 */
function ProposalDestination({
  deck,
  tags,
  noteType,
  decks,
  tagSuggestions,
  editable,
  onDeckChange,
  onTagsChange,
}: {
  deck: string;
  tags: readonly string[];
  noteType: string;
  decks: readonly string[];
  tagSuggestions: readonly string[];
  editable: boolean;
  onDeckChange: (deck: string) => void;
  onTagsChange: (tags: string[]) => void;
}) {
  if (!editable) {
    return (
      <div className="cwyc-proposal-dest" data-testid="proposal-destination">
        <div className="cwyc-proposal-dest-row">
          <span className="cwyc-proposal-dest-label">Deck</span>
          <span className="cwyc-proposal-dest-value">{deck}</span>
        </div>
        <div className="cwyc-proposal-dest-row">
          <span className="cwyc-proposal-dest-label">Note type</span>
          <span className="cwyc-proposal-dest-value">{noteType}</span>
        </div>
        {tags.length ? (
          <div className="cwyc-proposal-dest-row">
            <span className="cwyc-proposal-dest-label">Tags</span>
            <span className="cwyc-proposal-dest-value">{tags.join(", ")}</span>
          </div>
        ) : null}
      </div>
    );
  }
  return (
    <div className="cwyc-proposal-dest" data-testid="proposal-destination">
      <label className="cwyc-proposal-dest-row">
        <span className="cwyc-proposal-dest-label">Deck</span>
        {/* allowFreeText is required, not a nicety: a proposal routinely
            targets a deck that does not exist yet ("deck X does not exist;
            it will be created"). Without it, ComboBox's blur handler snaps
            any non-matching text back - which would silently erase the
            assistant's own choice the moment you clicked away. */}
        <ComboBox
          value={deck}
          onChange={onDeckChange}
          options={[...decks]}
          placeholder="Deck…"
          testid="proposal-deck"
          allowFreeText
        />
      </label>
      {/* Note type stays fixed: it decides which fields exist, so changing it
          here would invalidate the very values under review. */}
      <div className="cwyc-proposal-dest-row">
        <span className="cwyc-proposal-dest-label">Note type</span>
        <span className="cwyc-proposal-dest-value">{noteType}</span>
      </div>
      <label className="cwyc-proposal-dest-row">
        <span className="cwyc-proposal-dest-label">Tags</span>
        <TagChips
          tags={tags}
          onChange={onTagsChange}
          suggestions={tagSuggestions}
          testid="proposal-tag-input"
        />
      </label>
    </div>
  );
}

function SharedCreateProposalCard({ data, store }: ProposalCardProps) {
  const interaction = useMemo(() => createProposalInteraction(data), [data]);
  const previews = asPreviews(data.previews);
  const { ui } = useChatState(store);
  const suggestChange = useSuggestChange(store);
  const pending = data.status === "pending";
  const vimMode = !!ui.settings?.vimMode;
  // Seeded once per proposal identity: a status/warning re-push must not
  // discard a deck the user just picked.
  const [deck, setDeck] = useState(data.deck);
  const [tags, setTags] = useState<readonly string[]>(data.tags ?? []);
  const seeded = useRef(data.id);
  if (seeded.current !== data.id) {
    seeded.current = data.id;
    setDeck(data.deck);
    setTags(data.tags ?? []);
  }

  return (
    <>
    <InteractionCard
      interaction={interaction}
      className="cwyc-interaction-card"
      error={data.errorMessage}
      renderBlock={(block) => {
        if (block.type === "card_preview" && previews) return <PreviewFlip previews={previews} />;
        // The schema's key_value block is read-only by definition; swap in the
        // editable destination rather than pushing editability upstream into a
        // vocabulary that is shared with other hosts.
        if (block.type === "key_value") {
          return (
            <ProposalDestination
              deck={deck}
              tags={tags}
              noteType={data.note_type}
              decks={ui.meta.decks}
              tagSuggestions={ui.meta.tags}
              editable={pending}
              onDeckChange={setDeck}
              onTagsChange={setTags}
            />
          );
        }
        return undefined;
      }}
      // The renderer echoes `revision` as an OPAQUE string token (schema
      // contract); the bridge protocol speaks numbers, so Number() exactly at
      // this boundary - the same place the Mini App converts for its broker.
      onRevise={({ interactionId, revision, fields }) => store.reviseProposal(interactionId, Number(revision), fields)}
      // The renderer's click-to-edit control, supplied by us so vim keys reach
      // the create card's fields too (#31). The package stays free of any
      // editor dependency: it owns the draft, the host owns the control.
      renderFieldEditor={
        vimMode
          ? ({ name, value, onChange }) => (
              <VimTextArea
                value={value}
                onChange={onChange}
                testid={`field-input-vim-${name}`}
              />
            )
          : undefined
      }
      onAction={({ interactionId, revision, actionId }) => {
        if (actionId === "approve") {
          store.acceptProposalRevision(interactionId, Number(revision), { deck, tags });
        } else if (actionId === "reject") store.rejectProposal(interactionId);
        else if (actionId === "discuss") suggestChange(data);
      }}
    />
    {/* The shared renderer has no footer slot and drops its own action row
        once a proposal leaves `pending` (we pass no actions), so the
        post-resolution controls sit just below the card. */}
    {!pending ? <ProposalActions data={data} store={store} /> : null}
    </>
  );
}

function LegacyProposalCard({ data, store }: ProposalCardProps) {
  // Tolerate partial/legacy payloads: a replayed transcript or a schema drift
  // must never let `.map` on a missing array throw and blank the whole app
  // (the error boundary in App.tsx is the backstop; this is the near guard).
  const fields = Array.isArray(data.fields) ? data.fields : [];
  const warnings = Array.isArray(data.warnings) ? data.warnings : [];
  const editableFields = data.kind === "create" || data.kind === "edit";
  const initialValues = useMemo(
    () => Object.fromEntries(fields.map((f) => [f.name, f.new])),
    // Re-seed only when the proposal identity changes, not on every
    // status/warning update (that would clobber in-progress edits).
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [data.id]
  );
  const [values, setValues] = useState<Record<string, string>>(initialValues);
  const [editing, setEditing] = useState(false);
  const suggestChange = useSuggestChange(store);
  const vimMode = !!useChatState(store).ui.settings?.vimMode;

  // `open` = a change set still collecting edits. Python refuses to accept
  // one, so the row must not offer it (#19).
  const collecting = !!data.open;
  const pending = data.status === "pending" && !collecting;

  // Live preview while typing (proposals.py preview_request). Without it an
  // edit is reviewed against the preview the ASSISTANT proposed, so the card
  // silently disagrees with the textareas right above it the moment you touch
  // them. Debounced, and only while actually editing a pending proposal.
  const previewValues = editing && pending && data.kind === "edit" ? values : null;
  useEffect(() => {
    if (!previewValues) return;
    const timer = window.setTimeout(
      () => store.previewProposal(data.id, previewValues),
      PREVIEW_DEBOUNCE_MS
    );
    return () => window.clearTimeout(timer);
  }, [previewValues, data.id, store]);
  const kindLabel = KIND_LABELS[data.kind] ?? data.kind;
  const statusLabel = STATUS_LABELS[data.status] ?? data.status;
  const where =
    data.kind === "create" || data.kind === "edit"
      ? [data.note_type, data.kind === "edit" ? data.deck : null].filter(Boolean).join(" · ")
      : data.kind === "deck_op"
        ? data.deck
        : data.title;

  const previews = asPreviews(data.previews);

  return (
    <div
      className={"cwyc-proposal" + (pending ? "" : " cwyc-proposal-resolved")}
      data-testid="proposal-card"
    >
      <div className="cwyc-proposal-head">
        <span className="cwyc-proposal-kind">{kindLabel}</span>
        <span className="cwyc-proposal-where">{where}</span>
        <span className={"cwyc-proposal-status cwyc-status-" + data.status}>{statusLabel}</span>
      </div>

      {collecting ? (
        <div className="cwyc-proposal-rationale" data-testid="proposal-collecting">
          Collecting edits… {data.count} note(s) so far.
        </div>
      ) : data.rationale ? (
        <div className="cwyc-proposal-rationale">{data.rationale}</div>
      ) : null}
      {warnings.map((warning, i) => (
        <div className="cwyc-proposal-warning" key={i}>
          {warning}
        </div>
      ))}

      {previews ? <PreviewFlip previews={previews} /> : null}

      {!editableFields ? (
        <ProposalBody data={data} />
      ) : fields.length > 0 ? (
        <div className="cwyc-proposal-fields">
          {fields.map((field) => (
            <div className="cwyc-field" key={field.name}>
              <div className="cwyc-field-name">{field.name}</div>
              {editing ? (
                // Vim keys reach the field editor too, not just the composer:
                // this is where the real text work happens (#31). Same
                // onChange either way, so the 400ms live-preview debounce
                // fires identically.
                vimMode ? (
                  <VimTextArea
                    value={values[field.name] ?? field.new}
                    onChange={(text) =>
                      setValues((prev) => ({ ...prev, [field.name]: text }))
                    }
                    disabled={!pending}
                    testid={`field-input-vim-${field.name}`}
                  />
                ) : (
                  <textarea
                    className="cwyc-field-input"
                    value={values[field.name] ?? field.new}
                    onChange={(e) => setValues((prev) => ({ ...prev, [field.name]: e.target.value }))}
                    disabled={!pending}
                    rows={3}
                  />
                )
              ) : (
                <div className="cwyc-field-value">
                  {data.kind === "edit" && field.old ? (
                    // Word-level diff, same marks the shared renderer uses on
                    // create cards: a one-word typo fix must not read as a
                    // whole-field rewrite (dogfood 2026-07-23).
                    <div className="cwyc-field-new eui-field-diff">
                      {wordDiff(field.old, values[field.name] ?? field.new).map((part, i) => {
                        if (part.op === "eq") return <span key={i}>{part.text}</span>;
                        const Tag = part.op === "del" ? "del" : "ins";
                        return (
                          <Tag key={i} className={`eui-diff-${part.op}`}>
                            {part.text}
                          </Tag>
                        );
                      })}
                    </div>
                  ) : (
                    <div className="cwyc-field-new">{values[field.name] ?? field.new}</div>
                  )}
                </div>
              )}
            </div>
          ))}
        </div>
      ) : null}

      {/* A tag-only edit changes no field and no preview, so without this the
          card shows nothing at all to review. */}
      {data.kind === "edit" ? (
        <ProposalTagDiff
          tags={data.tags ?? []}
          addTags={data.add_tags ?? []}
          removeTags={data.remove_tags ?? []}
        />
      ) : null}

      {data.errorMessage ? <div className="cwyc-proposal-error">{data.errorMessage}</div> : null}

      {/* Everything you can still do once it is applied: undo, re-apply, put
          back for review (task #18). */}
      {!pending ? <ProposalActions data={data} store={store} /> : null}

      {pending ? (
        <div className="cwyc-proposal-actions">
          {/* No fields = nothing this button could open (a tag-only edit). */}
          {editableFields && fields.length > 0 ? (
            <button
              type="button"
              className="cwyc-btn-suggest"
              onClick={() => setEditing((e) => !e)}
              data-testid="proposal-edit"
            >
              {editing ? "Preview" : "Edit"}
            </button>
          ) : null}
          <button
            type="button"
            className="cwyc-btn-suggest"
            onClick={() => suggestChange(data)}
            data-testid="proposal-suggest"
          >
            Suggest change
          </button>
          <button
            type="button"
            className="cwyc-btn-reject"
            onClick={() => store.rejectProposal(data.id)}
            data-testid="proposal-reject"
          >
            Reject
          </button>
          <button
            type="button"
            className="cwyc-btn-accept cwyc-primary"
            onClick={() => store.acceptProposal(data.id, values, data.kind)}
            data-testid="proposal-approve"
          >
            {data.kind === "delete" ? "Delete" : data.kind === "deck_op" ? "Apply" : "Accept"}
          </button>
        </div>
      ) : null}
    </div>
  );
}

export function ProposalCard(props: ProposalCardProps) {
  return props.data.kind === "create" ? <SharedCreateProposalCard {...props} /> : <LegacyProposalCard {...props} />;
}
