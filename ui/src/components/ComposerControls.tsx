import { useEffect, useMemo, useRef, useState } from "react";
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
        <div className="cwyc-panel cwyc-panel-composer" role="menu">
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
        <div className="cwyc-panel cwyc-panel-composer cwyc-panel-pins">
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
  const modelLabel = MODELS.find((m) => m.id === ui.agent.model)?.label ?? ui.agent.model;
  const label = ui.agent.effort ? `${modelLabel} · ${ui.agent.effort}` : modelLabel;

  return (
    <div className="cwyc-ctl" ref={ref}>
      <button
        type="button"
        className="cwyc-chip"
        title="Model and reasoning effort (applies from your next message)"
        data-testid="model-picker"
        onClick={() => setOpen((o) => !o)}
      >
        {label}
      </button>
      {open ? (
        <div className="cwyc-panel cwyc-panel-composer cwyc-panel-right">
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
        </div>
      ) : null}
    </div>
  );
}
