import { useState } from "react";
import { useChatState } from "../ChatRuntimeProvider";
import { THEME_NAMES, type ChatStore, type DoctorRow, type ThemeName } from "../store";

const THEME_META: Record<ThemeName, { label: string; swatch: string }> = {
  teal: { label: "Teal", swatch: "#0e7c7b" },
  indigo: { label: "Indigo", swatch: "#4a4fb0" },
  evergreen: { label: "Evergreen", swatch: "#2f6b4f" },
};

/**
 * The Settings panel behind the header cog: the add-on's user-facing
 * configuration surface (dogfood 2026-07-13: "no cog icon, nowhere to
 * configure its behavior"). Each control posts set_setting; Python persists
 * it via writeConfig and re-pushes the authoritative "settings" snapshot.
 * The Setup check (previously squatting on the cog) now lives at the bottom
 * of this panel, run on demand. Advanced/rare options stay in Anki's add-on
 * config JSON (Tools > Add-ons > Config) - this panel is for the handful of
 * choices worth one click.
 */

function DoctorRows({ rows }: { rows: readonly DoctorRow[] | null }) {
  if (rows === null) return <div className="cwyc-panel-empty">Checking…</div>;
  return (
    <>
      {rows.map((row, i) => (
        <div key={i} className={"cwyc-doctor-row cwyc-doctor-" + row.status}>
          <span className="cwyc-doctor-dot" aria-hidden="true" />
          <span className="cwyc-doctor-label">{row.label}</span>
          <span className="cwyc-doctor-detail">{row.detail}</span>
        </div>
      ))}
    </>
  );
}

function Toggle(props: { label: string; checked: boolean; testid: string; onChange: (v: boolean) => void }) {
  return (
    <label className="cwyc-setting-row">
      <span className="cwyc-setting-label">{props.label}</span>
      <input
        type="checkbox"
        className="cwyc-setting-check"
        data-testid={props.testid}
        checked={props.checked}
        onChange={(e) => props.onChange(e.target.checked)}
      />
    </label>
  );
}

export function SettingsPanel({ store }: { store: ChatStore }) {
  const ui = useChatState(store).ui;
  const settings = ui.settings;
  const [doctorRequested, setDoctorRequested] = useState(false);

  return (
    <div
      className="cwyc-panel cwyc-panel-header cwyc-panel-settings"
      role="dialog"
      aria-label="Settings"
      data-testid="settings-panel"
    >
      <div className="cwyc-panel-title">Settings</div>
      {settings === null ? (
        <div className="cwyc-panel-empty">Loading…</div>
      ) : (
        <>
          <Toggle
            label="Reopen last chat on launch"
            testid="setting-restore-last-chat"
            checked={settings.restoreLastChat}
            onChange={(v) => store.setSetting("restore_last_chat", v)}
          />
          <Toggle
            label="Vim keys in composer"
            testid="setting-vim-mode"
            checked={settings.vimMode}
            onChange={(v) => store.setSetting("vim_mode", v)}
          />
          {settings.vimMode ? (
            <div className="cwyc-setting-row">
              <span className="cwyc-setting-label">
                Key mappings (<code>vim_mappings</code>)
              </span>
              <button
                type="button"
                className="cwyc-chip"
                data-testid="open-addon-config"
                title="Your vimrc equivalent: [keys, mapped-to, mode] triples in the add-on config"
                onClick={() => store.openAddonConfig()}
              >
                Edit config…
              </button>
            </div>
          ) : null}
          <div className="cwyc-setting-row">
            <span className="cwyc-setting-label">Theme</span>
            <div className="cwyc-seg cwyc-seg-theme" role="radiogroup" aria-label="Colour theme">
              {THEME_NAMES.map((name) => (
                <button
                  key={name}
                  type="button"
                  role="radio"
                  aria-checked={settings.theme === name}
                  className={"cwyc-seg-btn" + (settings.theme === name ? " cwyc-active" : "")}
                  data-testid={`setting-theme-${name}`}
                  title={THEME_META[name].label}
                  onClick={() => store.setSetting("theme", name)}
                >
                  <span
                    className="cwyc-theme-swatch"
                    style={{ background: THEME_META[name].swatch }}
                    aria-hidden="true"
                  />
                  {THEME_META[name].label}
                </button>
              ))}
            </div>
          </div>
          <div className="cwyc-setting-row">
            <span className="cwyc-setting-label">Dock side</span>
            <div className="cwyc-seg" role="radiogroup" aria-label="Dock side">
              {(["left", "right"] as const).map((side) => (
                <button
                  key={side}
                  type="button"
                  role="radio"
                  aria-checked={settings.dockSide === side}
                  className={"cwyc-seg-btn" + (settings.dockSide === side ? " cwyc-active" : "")}
                  data-testid={`setting-dock-${side}`}
                  onClick={() => store.setSetting("dock_side", side)}
                >
                  {side === "left" ? "Left" : "Right"}
                </button>
              ))}
            </div>
          </div>
          <div className="cwyc-panel-title cwyc-panel-title-gap">Shortcuts</div>
          <div className="cwyc-setting-row">
            <span className="cwyc-setting-label">Toggle chat</span>
            <span className="cwyc-setting-kbd">{settings.toggleShortcut}</span>
          </div>
          <div className="cwyc-setting-row">
            <span className="cwyc-setting-label">New chat</span>
            <span className="cwyc-setting-kbd">{settings.newChatShortcut}</span>
          </div>
        </>
      )}
      <div className="cwyc-panel-title cwyc-panel-title-gap">Setup check</div>
      {doctorRequested ? (
        <DoctorRows rows={ui.doctor} />
      ) : (
        <div className="cwyc-setting-row">
          <span className="cwyc-setting-label">Claude CLI, tools, skills…</span>
          <button
            type="button"
            className="cwyc-chip"
            data-testid="run-doctor"
            onClick={() => {
              setDoctorRequested(true);
              store.runDoctor();
            }}
          >
            Run check
          </button>
        </div>
      )}
      <div className="cwyc-setting-footnote">
        More options: Tools &rsaquo; Add-ons &rsaquo; Chat With Your Cards &rsaquo; Config
      </div>
    </div>
  );
}
