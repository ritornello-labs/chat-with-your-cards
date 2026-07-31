import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { useChatState } from "../ChatRuntimeProvider";
import { PERMISSION_MODES, type AgentTools, type ChatStore, type PinsState } from "../store";
import { useDismiss } from "../hooks/useDismiss";
import { ComboBox } from "./ComboBox";
import { TagChips } from "./TagChips";

/**
 * The composer control row (DESIGN.md section 9): permission-mode chip and
 * Pins bottom-left, model/effort picker bottom-right (send/stop lives next to
 * it in Thread.tsx). Each control posts an existing bridge command
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
    chip: "Accept edits",
    label: "Accept edits",
    hint: "Shell + file edits, auto-approved.",
  },
  {
    id: "auto",
    chip: "Auto",
    label: "Auto — classifier",
    hint: "Shell + files; a safety classifier vets each call. Needs Opus/Sonnet.",
  },
  { id: "full", chip: "Full tools", label: "Full — auto-approve", hint: "Shell + file writes, no checks." },
];

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
    // (bottom-anchored, the common case) a caller that reveals content on hover
    // must add it on the anchored side, or the items shift out from under the
    // cursor and flicker - see ToolsChip's risk line.
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

export function ModeChip({ store }: { store: ChatStore }) {
  const { ui } = useChatState(store);
  const [open, setOpen] = useState(false);
  const [detail, setDetail] = useState(false);
  const ref = useDismiss(open, () => setOpen(false));
  const panel = useSmartPanel(open);
  const current = PERMISSION_MODES.find((m) => m.id === ui.agent.mode) ?? PERMISSION_MODES[2];

  return (
    <div className="cwyc-ctl" ref={ref}>
      <button
        type="button"
        className="cwyc-chip"
        title={`Permission mode: ${current.label} — ${current.hint} (Shift+Tab cycles)`}
        data-testid="mode-chip"
        onClick={() => setOpen((o) => !o)}
      >
        {current.label}
      </button>
      {open ? (
        <div
          className={panel.panelClass + " cwyc-panel-mode"}
          style={panel.panelStyle}
          ref={panel.panelRef}
          role="menu"
        >
          <div className="cwyc-panel-title">Permission mode</div>
          {/* One ladder, most restrictive at the top (#26). The rail marks
              the ordering; the matrix below explains the hardcoded per-class
              carve-outs that used to be invisible. */}
          <div className="cwyc-mode-ladder">
            {PERMISSION_MODES.map((mode) => (
              <button
                key={mode.id}
                type="button"
                className={"cwyc-menu-item" + (mode.id === ui.agent.mode ? " cwyc-active" : "")}
                onClick={() => {
                  store.setPermissionMode(mode.id);
                  setOpen(false);
                }}
              >
                <span className="cwyc-ladder-dot" aria-hidden="true" />
                <span className="cwyc-menu-label">{mode.label}</span>
                <span className="cwyc-menu-hint">{mode.hint}</span>
              </button>
            ))}
          </div>
          <button
            type="button"
            className="cwyc-mode-detail-toggle"
            data-testid="mode-detail-toggle"
            aria-expanded={detail}
            onClick={() => setDetail((d) => !d)}
          >
            {detail ? "Hide details" : `What happens under ${current.label}?`}
          </button>
          {detail ? (
            <div className="cwyc-mode-matrix" data-testid="mode-matrix">
              {OPERATION_MATRIX.map((row) => (
                <div className="cwyc-mode-matrix-row" key={row.name}>
                  <span className="cwyc-mode-matrix-op">{row.name}</span>
                  <span className="cwyc-mode-matrix-val">
                    {row.by[current.id] ?? "Review card"}
                  </span>
                </div>
              ))}
              <div className="cwyc-mode-matrix-note">
                Shell &amp; file access is a separate axis — the agent-tools
                chip next to this one.
              </div>
            </div>
          ) : null}
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

/**
 * The agent-tools axis, split out of the model/effort menu into its own chip
 * (DESIGN.md section 5 / dogfood follow-up): sandbox vs full shell/file
 * access is a materially different risk decision than model/effort, and
 * burying it made the "full" state easy to miss. The chip itself carries the
 * warning color when full tools are active, so the risky state is visible
 * without opening anything.
 */
export function ToolsChip({ store }: { store: ChatStore }) {
  const { ui } = useChatState(store);
  const [open, setOpen] = useState(false);
  const ref = useDismiss(open, () => setOpen(false));
  const panel = useSmartPanel(open);
  const [hoverRisky, setHoverRisky] = useState(false);
  const [riskDismissed, setRiskDismissed] = useState(false);
  const [modalOpen, setModalOpen] = useState(false);
  const current = AGENT_TOOLS.find((t) => t.id === ui.agent.tools) ?? AGENT_TOOLS[0];
  // Every non-sandbox tier hands the agent a real shell on your machine, so the
  // warning accent and risk line ride on "not sandbox", not just "full".
  const isRisky = ui.agent.tools !== "sandbox";
  // The risk line rides on a risky tier being the selected OR hovered choice;
  // dismissing it hides it until you drop back to sandbox.
  useEffect(() => {
    if (!isRisky) setRiskDismissed(false);
  }, [isRisky]);
  const showRisk = (isRisky || hoverRisky) && !riskDismissed;
  // Rendered on the panel's ANCHORED side so revealing it on hover never moves
  // the menu items: above them when the panel opens upward (bottom-anchored),
  // below them when it opens downward. Otherwise the hovered risky item slides
  // out from under the cursor and flickers (dogfood 2026-07-15).
  const riskLine = (
    <div className="cwyc-risk-line" role="note">
      <span className="cwyc-risk-line-text">
        These tiers auto-run shell commands — including any hidden in untrusted
        card content.{" "}
        <button
          type="button"
          className="cwyc-risk-link"
          data-testid="risk-modal-open"
          onClick={() => setModalOpen(true)}
        >
          What&rsquo;s the risk?
        </button>
      </span>
      <button
        type="button"
        className="cwyc-risk-dismiss"
        aria-label="Dismiss warning"
        title="Dismiss"
        onClick={() => setRiskDismissed(true)}
      >
        ×
      </button>
    </div>
  );

  return (
    <div className="cwyc-ctl" ref={ref}>
      <button
        type="button"
        className={"cwyc-chip" + (isRisky ? " cwyc-chip-warn" : "")}
        title="Agent tools: shell/file access (applies from your next message)"
        data-testid="tools-chip"
        onClick={() => setOpen((o) => !o)}
      >
        {current.chip}
      </button>
      {open ? (
        <div className={panel.panelClass} style={panel.panelStyle} ref={panel.panelRef}>
          <div className="cwyc-panel-title">Agent tools</div>
          {showRisk && !panel.down ? riskLine : null}
          {AGENT_TOOLS.map((tool) => {
            const risky = tool.id !== "sandbox";
            // Auto's safety classifier only runs on premium models; the CLI
            // silently downgrades `--permission-mode auto` to a no-classifier
            // mode on Haiku (verified 2026-07-14), so offering it there would
            // promise a safety net that isn't active. Disable it instead.
            const disabled = tool.id === "auto" && ui.agent.model === "haiku";
            return (
              <button
                key={tool.id}
                type="button"
                disabled={disabled}
                className={
                  "cwyc-menu-item" +
                  (tool.id === ui.agent.tools ? " cwyc-active" : "") +
                  (disabled ? " cwyc-menu-item-disabled" : "")
                }
                data-testid={`agent-tools-${tool.id}`}
                title={
                  disabled
                    ? "Auto needs Opus or Sonnet — the safety classifier isn't available on Haiku"
                    : undefined
                }
                onMouseEnter={risky && !disabled ? () => setHoverRisky(true) : undefined}
                onMouseLeave={risky && !disabled ? () => setHoverRisky(false) : undefined}
                onClick={disabled ? undefined : () => store.setAgentTools(tool.id)}
              >
                <span className="cwyc-menu-label">
                  {tool.label}
                  {disabled ? " · needs Opus/Sonnet" : ""}
                </span>
                <span className="cwyc-menu-hint">{tool.hint}</span>
              </button>
            );
          })}
          {showRisk && panel.down ? riskLine : null}
        </div>
      ) : null}
      {modalOpen ? <RiskModal onClose={() => setModalOpen(false)} /> : null}
    </div>
  );
}
