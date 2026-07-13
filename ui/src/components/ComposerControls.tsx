import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { useChatState } from "../ChatRuntimeProvider";
import { PERMISSION_MODES, type ChatStore, type PinsState } from "../store";

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
    panelClass: "cwyc-panel cwyc-panel-composer" + (down ? " cwyc-panel-composer-down" : ""),
    panelStyle: maxHeight !== undefined ? { maxHeight } : undefined,
  };
}

function useDismiss(open: boolean, close: () => void) {
  const ref = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) close();
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") close();
    };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open, close]);
  return ref;
}

export function ModeChip({ store }: { store: ChatStore }) {
  const { ui } = useChatState(store);
  const [open, setOpen] = useState(false);
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
        <div className={panel.panelClass} style={panel.panelStyle} ref={panel.panelRef} role="menu">
          <div className="cwyc-panel-title">Permission mode</div>
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
              <span className="cwyc-menu-label">{mode.label}</span>
              <span className="cwyc-menu-hint">{mode.hint}</span>
            </button>
          ))}
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
      <button
        type="button"
        className={"cwyc-chip" + (pinCount ? " cwyc-chip-on" : "")}
        title="Pin the deck, note type, tags, or field defaults every proposal must use"
        data-testid="pins-button"
        onClick={() => {
          setDraft(null); // re-seed from authoritative pins on open
          setOpen((o) => !o);
        }}
      >
        Pins{pinCount ? ` · ${pinCount}` : ""}
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
            <select value={pins.deck} onChange={(e) => update({ deck: e.target.value })}>
              <option value="">(not pinned)</option>
              {ui.meta.decks.map((deck) => (
                <option key={deck} value={deck}>
                  {deck}
                </option>
              ))}
            </select>
          </label>
          <label className="cwyc-pin-row">
            <span>Note type</span>
            <select
              value={pins.note_type}
              onChange={(e) => update({ note_type: e.target.value, fields: {} })}
            >
              <option value="">(not pinned)</option>
              {ui.meta.noteTypes.map((nt) => (
                <option key={nt.name} value={nt.name}>
                  {nt.name}
                </option>
              ))}
            </select>
          </label>
          <label className="cwyc-pin-row">
            <span>Tags</span>
            <input
              type="text"
              placeholder="comma, separated"
              value={pins.tags.join(", ")}
              onChange={(e) =>
                update({
                  tags: e.target.value
                    .split(",")
                    .map((t) => t.trim())
                    .filter(Boolean),
                })
              }
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
