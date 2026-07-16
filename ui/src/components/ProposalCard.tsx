import { useMemo, useRef, useState } from "react";
import { InteractionCard } from "@elvis-labs/interaction-ui";
import "@elvis-labs/interaction-ui/styles.css";
import type { ChatStore, ProposalCardData } from "../store";
import { createProposalInteraction } from "../interactionAdapter";

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
 * Only "create" and "edit" get field-level editing here, matching the
 * task's explicit Approve/Edit/Reject scope. Other kinds (bulk/delete/
 * change_set/deck_op/skill_update) still render - rationale, warnings,
 * count - and can still be accepted/rejected, just without per-field
 * editing or the word-diff/preview-iframe/tag-editor treatment app.js gives
 * them. That parity gap is intentional; see ui/README.md.
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
  const wireScrollPersistence = (index: 0 | 1) => (frame: HTMLIFrameElement | null) => {
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
  };
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

function SharedCreateProposalCard({ data, store }: ProposalCardProps) {
  const interaction = useMemo(() => createProposalInteraction(data), [data]);
  const previews = asPreviews(data.previews);
  return (
    <InteractionCard
      interaction={interaction}
      className="cwyc-interaction-card"
      error={data.errorMessage}
      renderBlock={(block) => block.type === "card_preview" && previews ? <PreviewFlip previews={previews} /> : undefined}
      onRevise={({ interactionId, expectedRevision, fields }) => store.reviseProposal(interactionId, expectedRevision, fields)}
      onAction={({ interactionId, revision, actionId }) => {
        if (actionId === "approve") store.acceptProposalRevision(interactionId, revision);
        else if (actionId === "reject") store.rejectProposal(interactionId);
      }}
    />
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

  const pending = data.status === "pending";
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

      {data.rationale ? <div className="cwyc-proposal-rationale">{data.rationale}</div> : null}
      {warnings.map((warning, i) => (
        <div className="cwyc-proposal-warning" key={i}>
          {warning}
        </div>
      ))}

      {previews ? <PreviewFlip previews={previews} /> : null}

      {editableFields ? (
        <div className="cwyc-proposal-fields">
          {fields.map((field) => (
            <div className="cwyc-field" key={field.name}>
              <div className="cwyc-field-name">{field.name}</div>
              {editing ? (
                <textarea
                  className="cwyc-field-input"
                  value={values[field.name] ?? field.new}
                  onChange={(e) => setValues((prev) => ({ ...prev, [field.name]: e.target.value }))}
                  disabled={!pending}
                  rows={3}
                />
              ) : (
                <div className="cwyc-field-value">
                  {data.kind === "edit" && field.old ? (
                    <div className="cwyc-field-old">{field.old}</div>
                  ) : null}
                  <div className="cwyc-field-new">{values[field.name] ?? field.new}</div>
                </div>
              )}
            </div>
          ))}
        </div>
      ) : (
        <div className="cwyc-proposal-count">{data.count} item(s)</div>
      )}

      {data.errorMessage ? <div className="cwyc-proposal-error">{data.errorMessage}</div> : null}

      {pending ? (
        <div className="cwyc-proposal-actions">
          {editableFields ? (
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
