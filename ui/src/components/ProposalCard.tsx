import { useMemo, useState } from "react";
import type { ChatStore, ProposalCardData } from "../store";

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

export function ProposalCard({ data, store }: ProposalCardProps) {
  const editableFields = data.kind === "create" || data.kind === "edit";
  const initialValues = useMemo(
    () => Object.fromEntries(data.fields.map((f) => [f.name, f.new])),
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

  return (
    <div className={"cwyc-proposal" + (pending ? "" : " cwyc-proposal-resolved")}>
      <div className="cwyc-proposal-head">
        <span className="cwyc-proposal-kind">{kindLabel}</span>
        <span className="cwyc-proposal-where">{where}</span>
        <span className={"cwyc-proposal-status cwyc-status-" + data.status}>{statusLabel}</span>
      </div>

      {data.rationale ? <div className="cwyc-proposal-rationale">{data.rationale}</div> : null}
      {data.warnings.map((warning, i) => (
        <div className="cwyc-proposal-warning" key={i}>
          {warning}
        </div>
      ))}

      {editableFields ? (
        <div className="cwyc-proposal-fields">
          {data.fields.map((field) => (
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
            >
              {editing ? "Preview" : "Edit"}
            </button>
          ) : null}
          <button type="button" className="cwyc-btn-reject" onClick={() => store.rejectProposal(data.id)}>
            Reject
          </button>
          <button
            type="button"
            className="cwyc-btn-accept cwyc-primary"
            onClick={() => store.acceptProposal(data.id, values, data.kind)}
          >
            {data.kind === "delete" ? "Delete" : data.kind === "deck_op" ? "Apply" : "Accept"}
          </button>
        </div>
      ) : null}
    </div>
  );
}
