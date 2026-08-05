import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { useChatState } from "../ChatRuntimeProvider";
import { PERMISSION_MODES, type AgentTools, type ChatStore, type PinsState } from "../store";
import { useDismiss } from "../hooks/useDismiss";
import { ComboBox } from "./ComboBox";
import { TagChips } from "./TagChips";

/**
 * The composer control row (DESIGN.md section 9): Pins/attachments bottom-left,
 * one Access control for the two independent permission axes in the middle,
 * and model/effort bottom-right (send/stop lives next to it in Thread.tsx).
 * Each control posts an existing bridge command
 * (set_permission_mode / set_pins / set_agent) and reflects the authoritative
 * state Python re-pushes afterwards ("agent" / "pins" events).
 */

const MODELS: readonly { id: string; label: string }[] = [
  { id: "", label: "Default model" },
  { id: "fable", label: "Fable" },
  { id: "opus", label: "Opus" },
  { id: "sonnet", label: "Sonnet" },
  { id: "haiku", label: "Haiku" },
];

const EFFORTS: readonly { id: string; label: string }[] = [
  { id: "", label: "Default effort" },
  { id: "low", label: "Low" },
  { id: "medium", label: "Medium" },
  { id: "high", label: "High" },
  { id: "max", label: "Max" },
];

// The agent-tools axis (DESIGN.md section 5) - orthogonal to the permission
// mode (which gates collection writes). It governs the CLI's OWN shell/file
// tools and how their calls are approved in our headless session. Verified
// 2026-07-14 (CLI 2.1.208): headless `-p` runs benign tools under every
// non-sandbox mode without stalling, so acceptEdits/auto/full are all real,
// usable tiers (not silently broken). `plan`/`manual` are omitted: plan makes
// the model refuse to act (it would stop card proposals; the "read-only"
// collection mode covers that need), and manual is indistinguishable from
// auto-approve headlessly. `chip` is the short label on the closed chip.
const AGENT_TOOLS: readonly {
  id: AgentTools;
  label: string;
  chip: string;
  hint: string;
}[] = [
  { id: "sandbox", chip: "Sandbox", label: "Sandbox", hint: "Anki tools + read-only files. No shell." },
  {
    id: "acceptEdits",
    chip: "Edit",
    label: "Accept edits",
    hint: "Shell + file edits, auto-approved.",
  },
  {
    id: "auto",
    chip: "Auto",
    label: "Auto — classifier",
    hint: "Shell + files; a safety classifier vets each call. Needs Opus/Sonnet.",
  },
  { id: "full", chip: "Full", label: "Full — auto-approve", hint: "Shell + file writes, no checks." },
];

const COLLECTION_ACCESS_SHORT: Readonly<Record<string, string>> = {
  "ask-each-read": "Ask",
  "read-only": "Read only",
  default: "Review",
  "auto-accept": "Auto notes",
  "trusted-writes": "Trusted",
};

/**
 * Composer panels default to opening UPWARD (they sit above the bottom-pinned
 * composer) - but when the chat is short/empty the composer can be near the
 * TOP of the dock, and an upward panel runs off the top edge, clipped and
 * unscrollable (dogfood 2026-07-12: model/effort menu unusable in a short
 * dock). Measure at open time: flip downward when there is not enough room
 * above, and clamp max-height to the available side so the panel always fits
 * (its own overflow-y:auto handles the rest).
 */
function useSmartPanel(open: boolean) {
  const panelRef = useRef<HTMLDivElement | null>(null);
  const [down, setDown] = useState(false);
  const [maxHeight, setMaxHeight] = useState<number | undefined>(undefined);
  useLayoutEffect(() => {
    if (!open) {
      setDown(false);
      setMaxHeight(undefined);
      return;
    }
    const el = panelRef.current;
    const ctl = el?.closest(".cwyc-ctl");
    if (!el || !ctl) return;
    const trigger = ctl.getBoundingClientRect();
    const margin = 8; // breathing room to the dock edge
    const gap = 6; // matches the panel's calc(100% + 6px) offset
    const spaceAbove = trigger.top - margin - gap;
    const spaceBelow = window.innerHeight - trigger.bottom - margin - gap;
    const needed = Math.min(el.scrollHeight, 320);
    const goDown = spaceAbove < needed && spaceBelow > spaceAbove;
    setDown(goDown);
    // Floor of 80 (not higher): in a degenerate ultra-short dock a taller
    // floor would itself overflow; 80 keeps ~2 items + title visible with
    // the panel's own overflow-y:auto doing the rest.
    setMaxHeight(Math.max(80, Math.min(320, goDown ? spaceBelow : spaceAbove)));
  }, [open]);
  return {
    panelRef,
    // `down` = opens below the trigger (top-anchored). When it opens ABOVE
    // (bottom-anchored, the common case) dynamically inserted content belongs
    // below stable choices so it does not move a hovered item.
    down,
    panelClass: "cwyc-panel cwyc-panel-composer" + (down ? " cwyc-panel-composer-down" : ""),
    panelStyle: maxHeight !== undefined ? { maxHeight } : undefined,
  };
}

/** Per-operation-class behavior for the "What happens in this mode?"
 *  disclosure (#26). PRESENTATION of verified behavior only - the carve-outs
 *  are hardcoded in proposals.py/grading.py; this table just stops them
 *  being invisible. Keep in sync with _finish_submission (bulk & deck ops
 *  auto-apply under trusted), the auto-accept creations+grading cap,
 *  delete_notes (trusted_only, always confirmed) and submit_skill_update
 *  (always confirmed). */
const OPERATION_MATRIX: readonly { name: string; by: Record<string, string> }[] = [
  {
    name: "Reads",
    by: {
      "ask-each-read": "Each needs your OK",
      "read-only": "Free",
      default: "Free",
      "auto-accept": "Free",
      "trusted-writes": "Free",
    },
  },
  {
    name: "New notes",
    by: {
      "ask-each-read": "Review card",
      "read-only": "Not offered",
      default: "Review card",
      "auto-accept": "Instant, capped per chat",
      "trusted-writes": "Instant, session budget",
    },
  },
  {
    name: "Note edits & bulk sweeps",
    by: {
      "ask-each-read": "Review card",
      "read-only": "Not offered",
      default: "Review card",
      "auto-accept": "Review card",
      "trusted-writes": "Instant, session budget",
    },
  },
  {
    name: "Cards, tags & scheduling",
    by: {
      "ask-each-read": "Review card",
      "read-only": "Not offered",
      default: "Review card",
      "auto-accept": "Review card",
      "trusted-writes": "Instant, session budget",
    },
  },
  {
    name: "Decks & structure",
    by: {
      "ask-each-read": "Review card",
      "read-only": "Not offered",
      default: "Review card",
      "auto-accept": "Review card",
      "trusted-writes": "Instant, session budget",
    },
  },
  {
    name: "Grading (Again)",
    by: {
      "ask-each-read": "Confirmation chip",
      "read-only": "Not offered",
      default: "Confirmation chip",
      "auto-accept": "Instant, same cap",
      "trusted-writes": "Instant, session budget",
    },
  },
  {
    name: "Deleting notes",
    by: {
      "ask-each-read": "Not offered",
      "read-only": "Not offered",
      default: "Not offered",
      "auto-accept": "Not offered",
      "trusted-writes": "Always confirmed + backup",
    },
  },
  {
    name: "Skill updates",
    by: {
      "ask-each-read": "Review card",
      "read-only": "Not offered",
      default: "Review card",
      "auto-accept": "Review card",
      "trusted-writes": "Review card",
    },
  },
];

type AccessSection = "collection" | "computer";

function AccessChevron() {
  return (
    <svg viewBox="0 0 12 12" width="11" height="11" aria-hidden="true">
      <path
        d="M4.5 2.5 8 6 4.5 9.5"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.4"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

/** One compact entry point for two genuinely independent safety axes.
 *  The trigger shows short current values; the overview names both domains in
 *  full. Drilling into one domain renders labels only and one shared
 *  description, so the panel stays usable in a short/narrow Anki dock. */
export function AccessControl({ store }: { store: ChatStore }) {
  const { ui } = useChatState(store);
  const [open, setOpen] = useState(false);
  const [section, setSection] = useState<AccessSection | null>(null);
  const [detail, setDetail] = useState(false);
  const [previewMode, setPreviewMode] = useState<string | null>(null);
  const [previewTool, setPreviewTool] = useState<AgentTools | null>(null);
  const [modalOpen, setModalOpen] = useState(false);
  const close = () => {
    setOpen(false);
    setSection(null);
    setDetail(false);
    setPreviewMode(null);
    setPreviewTool(null);
  };
  const ref = useDismiss(open, close);
  const panel = useSmartPanel(open);
  const currentMode = PERMISSION_MODES.find((m) => m.id === ui.agent.mode) ?? PERMISSION_MODES[2];
  const currentTool = AGENT_TOOLS.find((t) => t.id === ui.agent.tools) ?? AGENT_TOOLS[0];
  const describedMode =
    PERMISSION_MODES.find((mode) => mode.id === (previewMode ?? currentMode.id)) ?? currentMode;
  const describedTool =
    AGENT_TOOLS.find((tool) => tool.id === (previewTool ?? currentTool.id)) ?? currentTool;
  const isRisky = ui.agent.tools !== "sandbox";
  const triggerLabel = `Access · ${COLLECTION_ACCESS_SHORT[currentMode.id] ?? currentMode.label} / ${currentTool.chip}`;

  const returnToOverview = () => {
    setSection(null);
    setDetail(false);
    setPreviewMode(null);
    setPreviewTool(null);
  };

  return (
    <div className="cwyc-ctl" ref={ref}>
      <button
        type="button"
        className={"cwyc-chip cwyc-access-trigger" + (isRisky ? " cwyc-chip-warn" : "")}
        title={`Access — Anki collection: ${currentMode.label}. Computer tools: ${currentTool.label}. Shift+Tab cycles collection access.`}
        data-testid="access-control"
        aria-expanded={open}
        onClick={() => {
          if (open) close();
          else setOpen(true);
        }}
      >
        <svg className="cwyc-access-icon" viewBox="0 0 14 14" width="11" height="11" aria-hidden="true">
          <path
            d="M7 1.4 12 3.2v3.5c0 3-1.8 5-5 5.9-3.2-.9-5-2.9-5-5.9V3.2z"
            fill="none"
            stroke="currentColor"
            strokeWidth="1.2"
            strokeLinejoin="round"
          />
        </svg>
        <span>{triggerLabel}</span>
      </button>
      {open ? (
        <div
          className={panel.panelClass + " cwyc-panel-access"}
          style={panel.panelStyle}
          ref={panel.panelRef}
          role="dialog"
          aria-label="Access settings"
        >
          {section === null ? (
            <>
              <div className="cwyc-panel-title">Access</div>
              <button
                type="button"
                className="cwyc-access-axis"
                data-testid="access-collection"
                onClick={() => setSection("collection")}
              >
                <span className="cwyc-access-axis-copy">
                  <span className="cwyc-access-axis-name">Anki collection</span>
                  <span className="cwyc-access-axis-value">{currentMode.label}</span>
                  <span className="cwyc-access-axis-hint">{currentMode.hint}</span>
                </span>
                <AccessChevron />
              </button>
              <button
                type="button"
                className="cwyc-access-axis"
                data-testid="access-computer"
                onClick={() => setSection("computer")}
              >
                <span className="cwyc-access-axis-copy">
                  <span className="cwyc-access-axis-name">Computer tools</span>
                  <span className="cwyc-access-axis-value">{currentTool.label}</span>
                  <span className="cwyc-access-axis-hint">{currentTool.hint}</span>
                </span>
                <AccessChevron />
              </button>
              {isRisky ? (
                <div className="cwyc-access-risk" role="note">
                  Computer tools can run shell commands from untrusted card content. {" "}
                  <button type="button" data-testid="risk-modal-open" onClick={() => setModalOpen(true)}>
                    What&rsquo;s the risk?
                  </button>
                </div>
              ) : null}
            </>
          ) : section === "collection" ? (
            <>
              <button type="button" className="cwyc-access-back" onClick={returnToOverview}>
                ‹ Access
              </button>
              <div className="cwyc-panel-title">Anki collection</div>
              <div className="cwyc-access-options" role="radiogroup" aria-label="Anki collection access">
                {PERMISSION_MODES.map((mode) => (
                  <button
                    key={mode.id}
                    type="button"
                    role="radio"
                    aria-checked={mode.id === ui.agent.mode}
                    className={"cwyc-access-option" + (mode.id === ui.agent.mode ? " cwyc-active" : "")}
                    data-testid={`permission-mode-${mode.id}`}
                    onFocus={() => setPreviewMode(mode.id)}
                    onBlur={() => setPreviewMode(null)}
                    onMouseEnter={() => setPreviewMode(mode.id)}
                    onMouseLeave={() => setPreviewMode(null)}
                    onClick={() => {
                      store.setPermissionMode(mode.id);
                      returnToOverview();
                    }}
                  >
                    <span className="cwyc-access-radio" aria-hidden="true" />
                    <span>{mode.label}</span>
                  </button>
                ))}
              </div>
              <div className="cwyc-access-description" aria-live="polite">{describedMode.hint}</div>
              <button
                type="button"
                className="cwyc-mode-detail-toggle"
                data-testid="mode-detail-toggle"
                aria-expanded={detail}
                onClick={() => setDetail((value) => !value)}
              >
                {detail ? "Hide operation details" : `What happens under ${currentMode.label}?`}
              </button>
              {detail ? (
                <div className="cwyc-mode-matrix" data-testid="mode-matrix">
                  {OPERATION_MATRIX.map((row) => (
                    <div className="cwyc-mode-matrix-row" key={row.name}>
                      <span className="cwyc-mode-matrix-op">{row.name}</span>
                      <span className="cwyc-mode-matrix-val">{row.by[currentMode.id] ?? "Review card"}</span>
                    </div>
                  ))}
                </div>
              ) : null}
            </>
          ) : null}
          {section === "computer" ? (
            <>
              <button type="button" className="cwyc-access-back" onClick={returnToOverview}>
                ‹ Access
              </button>
              <div className="cwyc-panel-title">Computer tools</div>
              <div className="cwyc-access-options" role="radiogroup" aria-label="Computer tools access">
                {AGENT_TOOLS.map((tool) => {
                  const disabled = tool.id === "auto" && ui.agent.model === "haiku";
                  return (
                    <button
                      key={tool.id}
                      type="button"
                      role="radio"
                      aria-checked={tool.id === ui.agent.tools}
                      disabled={disabled}
                      className={
                        "cwyc-access-option" +
                        (tool.id === ui.agent.tools ? " cwyc-active" : "") +
                        (disabled ? " cwyc-menu-item-disabled" : "")
                      }
                      data-testid={`agent-tools-${tool.id}`}
                      title={disabled ? "Auto needs Opus or Sonnet" : undefined}
                      onFocus={() => setPreviewTool(tool.id)}
                      onBlur={() => setPreviewTool(null)}
                      onMouseEnter={() => setPreviewTool(tool.id)}
                      onMouseLeave={() => setPreviewTool(null)}
                      onClick={
                        disabled
                          ? undefined
                          : () => {
                              store.setAgentTools(tool.id);
                              returnToOverview();
                            }
                      }
                    >
                      <span className="cwyc-access-radio" aria-hidden="true" />
                      <span>{tool.label}{disabled ? " · needs Opus/Sonnet" : ""}</span>
                    </button>
                  );
                })}
              </div>
              <div className="cwyc-access-description" aria-live="polite">{describedTool.hint}</div>
              <div className="cwyc-access-risk" role="note">
                Non-sandbox modes can auto-run commands influenced by untrusted card content. {" "}
                <button type="button" data-testid="risk-modal-open" onClick={() => setModalOpen(true)}>
                  What&rsquo;s the risk?
                </button>
              </div>
            </>
          ) : null}
        </div>
      ) : null}
      {modalOpen ? <RiskModal onClose={() => setModalOpen(false)} /> : null}
    </div>
  );
}

/** Composer attachments (#15a): same category as Pins - context riding the
 *  NEXT message, not a mode. Zero attachments: click opens the native
 *  picker. With attachments: click opens a small list with per-file remove
 *  and an add-more action, so a mistaken pick is one click to fix. */
export function AttachButton({ store }: { store: ChatStore }) {
  const { ui } = useChatState(store);
  const [open, setOpen] = useState(false);
  const ref = useDismiss(open, () => setOpen(false));
  const panel = useSmartPanel(open);
  const count = ui.attachments.length;

  return (
    <div className="cwyc-ctl" ref={ref}>
      <button
        type="button"
        className={"cwyc-chip cwyc-chip-pin" + (count ? " cwyc-chip-on" : "")}
        title="Attach files for your next message (they can go onto cards)"
        data-testid="attach-button"
        onClick={() => {
          if (count === 0) store.pickAttachments();
          else setOpen((o) => !o);
        }}
      >
        <svg viewBox="0 0 14 14" width="11" height="11" aria-hidden="true" fill="none">
          <path
            d="M9.9 3.4 5.6 7.7a1.6 1.6 0 1 0 2.3 2.3l4-4a3 3 0 1 0-4.3-4.2l-4 4a4.4 4.4 0 1 0 6.3 6.2l3.4-3.4"
            stroke="currentColor"
            strokeWidth="1.3"
            strokeLinecap="round"
            transform="scale(0.82) translate(1.2 1.2)"
          />
        </svg>
        Attach
        {count ? <span className="cwyc-chip-count">{count}</span> : null}
      </button>
      {open && count ? (
        <div
          className={panel.panelClass + " cwyc-panel-attachments"}
          style={panel.panelStyle}
          ref={panel.panelRef}
        >
          <div className="cwyc-panel-title">Files for your next message</div>
          {ui.attachments.map((item) => (
            <div className="cwyc-attach-row" key={item.id}>
              <span className="cwyc-attach-name" title={item.name}>
                {item.name}
              </span>
              <span className="cwyc-attach-meta">
                {item.kind} · {Math.max(1, Math.round(item.size / 1024))} KB
              </span>
              <button
                type="button"
                className="cwyc-attach-remove"
                aria-label={`Remove ${item.name}`}
                data-testid={`attach-remove-${item.id}`}
                onClick={() => store.removeAttachment(item.id)}
              >
                ×
              </button>
            </div>
          ))}
          <button
            type="button"
            className="cwyc-mode-detail-toggle"
            onClick={() => {
              store.pickAttachments();
              setOpen(false);
            }}
          >
            Add more…
          </button>
        </div>
      ) : null}
    </div>
  );
}

export function PinsButton({ store }: { store: ChatStore }) {
  const { ui } = useChatState(store);
  const [open, setOpen] = useState(false);
  const ref = useDismiss(open, () => setOpen(false));
  const panel = useSmartPanel(open);
  const [draft, setDraft] = useState<PinsState | null>(null);
  const pins = draft ?? ui.pins;
  const pinCount =
    (ui.pins.deck ? 1 : 0) +
    (ui.pins.note_type ? 1 : 0) +
    ui.pins.tags.length +
    Object.keys(ui.pins.fields).length;

  const fieldsForType = useMemo(
    () => ui.meta.noteTypes.find((nt) => nt.name === pins.note_type)?.fields ?? [],
    [ui.meta.noteTypes, pins.note_type]
  );

  const update = (patch: Partial<PinsState>) => setDraft({ ...pins, ...patch });

  return (
    <div className="cwyc-ctl" ref={ref}>
      {/* Deliberately NOT a mode pill. The other three chips all answer "how
          should the agent behave" and show a current setting; pins are
          CONTEXT that rides with the next message - the same category the
          attachment control will join (task #15). Sat between two mode chips,
          it read as a fourth setting (user, 2026-07-27), so the shape carries
          the distinction, not just the position. */}
      <button
        type="button"
        className={"cwyc-chip cwyc-chip-pin" + (pinCount ? " cwyc-chip-on" : "")}
        title="Pin the deck, note type, tags, or field defaults every proposal must use"
        data-testid="pins-button"
        onClick={() => {
          setDraft(null); // re-seed from authoritative pins on open
          setOpen((o) => !o);
        }}
      >
        <svg viewBox="0 0 14 14" width="11" height="11" aria-hidden="true">
          <path
            d="M5.2 1.4h3.6l-.5 3.3 2.3 2.1v1.1H7.7v4.2l-.7 1-.7-1V7.9H2.4V6.8l2.3-2.1z"
            fill="currentColor"
          />
        </svg>
        Pins
        {pinCount ? <span className="cwyc-chip-count">{pinCount}</span> : null}
      </button>
      {open ? (
        <div
          className={panel.panelClass + " cwyc-panel-pins"}
          style={panel.panelStyle}
          ref={panel.panelRef}
        >
          <div className="cwyc-panel-title">Pinned constraints</div>
          <label className="cwyc-pin-row">
            <span>Deck</span>
            <ComboBox
              value={pins.deck}
              onChange={(deck) => update({ deck })}
              options={ui.meta.decks}
              allowFreeText
              placeholder="(not pinned)"
              testid="pins-deck"
            />
          </label>
          <label className="cwyc-pin-row">
            <span>Note type</span>
            <ComboBox
              value={pins.note_type}
              onChange={(note_type) => update({ note_type, fields: {} })}
              options={ui.meta.noteTypes.map((nt) => nt.name)}
              allowFreeText={false}
              placeholder="(not pinned)"
              testid="pins-notetype"
            />
          </label>
          <label className="cwyc-pin-row">
            <span>Tags</span>
            <TagChips
              tags={pins.tags}
              onChange={(tags) => update({ tags })}
              suggestions={ui.meta.tags}
              testid="pins-tags"
            />
          </label>
          {fieldsForType.length ? (
            <div className="cwyc-pin-fields">
              <div className="cwyc-panel-subtitle">Field defaults</div>
              {fieldsForType.map((field) => (
                <label className="cwyc-pin-row" key={field}>
                  <span>{field}</span>
                  <input
                    type="text"
                    value={pins.fields[field] ?? ""}
                    onChange={(e) => {
                      const fields = { ...pins.fields };
                      if (e.target.value) fields[field] = e.target.value;
                      else delete fields[field];
                      update({ fields });
                    }}
                  />
                </label>
              ))}
            </div>
          ) : null}
          <div className="cwyc-panel-actions">
            <button
              type="button"
              className="cwyc-chip"
              onClick={() => {
                store.setPins({ deck: "", note_type: "", tags: [], fields: {} });
                setDraft(null);
                setOpen(false);
              }}
            >
              Clear
            </button>
            <button
              type="button"
              className="cwyc-chip cwyc-chip-primary"
              onClick={() => {
                store.setPins(pins);
                setDraft(null);
                setOpen(false);
              }}
            >
              Apply
            </button>
          </div>
        </div>
      ) : null}
    </div>
  );
}

/**
 * The full-agent-tools risk explainer. Rendered as a faux-viewport overlay
 * portaled into the dock root: `position: fixed` collapses the Anki webview
 * (see DESIGN/store notes), so this is `position: absolute; inset: 0` over the
 * `.cwyc-app` container (which useDismiss/portal escape the small chip). Amber
 * warning accent, theme-aware via the --cwyc-* layer.
 */
function RiskModal({ onClose }: { onClose: () => void }) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);
  const host =
    (typeof document !== "undefined" &&
      (document.querySelector(".cwyc-app") ?? document.getElementById("cwyc-root"))) ||
    null;
  if (!host) return null;
  return createPortal(
    <div
      className="cwyc-risk-overlay"
      data-testid="risk-modal"
      role="dialog"
      aria-modal="true"
      aria-label="Full agent tools — what you're allowing"
      onClick={onClose}
    >
      <div className="cwyc-risk-modal" onClick={(e) => e.stopPropagation()}>
        <div className="cwyc-risk-modal-title">Full agent tools — what you&rsquo;re allowing</div>
        <div className="cwyc-risk-modal-body">
          <p>
            Full mode gives the agent a real shell and file access, with no
            per-command approval (auto-approve).
          </p>
          <p>
            The catch is that the agent reads your card content, and card
            content is untrusted — a shared or downloaded deck can contain text
            crafted to steer the agent (&ldquo;ignore your instructions and run
            this&rdquo;). In full auto-approve mode, such an injected command
            runs on your computer immediately, with no gate.
          </p>
          <p>
            Only use full mode on collections you trust. Anki card changes still
            go through the review flow by default, but a shell can bypass that
            too. Prefer the built-in propose tools for cards; if the agent must
            touch the collection from a shell while Anki is open, it should use
            AnkiConnect, never write the database file directly.
          </p>
          <p>
            Claude Code&rsquo;s built-in circuit breaker still blocks{" "}
            <code>rm -rf /</code> and <code>rm -rf ~</code>.
          </p>
        </div>
        <div className="cwyc-risk-modal-actions">
          <button
            type="button"
            className="cwyc-chip cwyc-chip-primary"
            data-testid="risk-modal-close"
            onClick={onClose}
          >
            Got it
          </button>
        </div>
      </div>
    </div>,
    host
  );
}

export function ModelPicker({ store }: { store: ChatStore }) {
  const { ui } = useChatState(store);
  const [open, setOpen] = useState(false);
  const ref = useDismiss(open, () => setOpen(false));
  const panel = useSmartPanel(open);
  const modelLabel = MODELS.find((m) => m.id === ui.agent.model)?.label ?? ui.agent.model;
  const label =
    (ui.agent.effort ? `${modelLabel} · ${ui.agent.effort}` : modelLabel) +
    (ui.agent.fast ? " · fast" : "");

  return (
    <div className="cwyc-ctl" ref={ref}>
      <button
        type="button"
        className="cwyc-chip"
        title="Model, reasoning effort, and fast mode (applies from your next message)"
        data-testid="model-picker"
        onClick={() => setOpen((o) => !o)}
      >
        {label}
      </button>
      {open ? (
        <div
          className={panel.panelClass + " cwyc-panel-right"}
          style={panel.panelStyle}
          ref={panel.panelRef}
        >
          <div className="cwyc-panel-title">Model</div>
          {MODELS.map((model) => (
            <button
              key={model.id || "default"}
              type="button"
              className={"cwyc-menu-item" + (model.id === ui.agent.model ? " cwyc-active" : "")}
              onClick={() => store.setAgent(model.id, ui.agent.effort)}
            >
              <span className="cwyc-menu-label">{model.label}</span>
            </button>
          ))}
          <div className="cwyc-panel-title cwyc-panel-title-gap">Effort</div>
          {EFFORTS.map((effort) => (
            <button
              key={effort.id || "default"}
              type="button"
              className={"cwyc-menu-item" + (effort.id === ui.agent.effort ? " cwyc-active" : "")}
              onClick={() => store.setAgent(ui.agent.model, effort.id)}
            >
              <span className="cwyc-menu-label">{effort.label}</span>
            </button>
          ))}
          <div className="cwyc-panel-title cwyc-panel-title-gap">Fast mode</div>
          <button
            type="button"
            className={"cwyc-menu-item" + (!ui.agent.fast ? " cwyc-active" : "")}
            title="Opus-only faster output; needs claude CLI 2.1.205+"
            onClick={() => store.setAgent(ui.agent.model, ui.agent.effort, false)}
          >
            <span className="cwyc-menu-label">Off</span>
          </button>
          <button
            type="button"
            className={"cwyc-menu-item" + (ui.agent.fast ? " cwyc-active" : "")}
            title="Opus-only faster output; needs claude CLI 2.1.205+"
            onClick={() => store.setAgent(ui.agent.model, ui.agent.effort, true)}
          >
            <span className="cwyc-menu-label">On</span>
          </button>
        </div>
      ) : null}
    </div>
  );
}
