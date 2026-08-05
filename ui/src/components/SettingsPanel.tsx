import { useEffect, useRef, useState } from "react";
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

function ShortcutInput(props: {
  value: string;
  testid: string;
  onCommit: (chord: string) => void;
}) {
  const [draft, setDraft] = useState(props.value);
  // Re-seed when Python re-pushes the snapshot - including the case where it
  // REJECTED an unparseable chord and pushed the old one back.
  useEffect(() => setDraft(props.value), [props.value]);
  const commit = () => {
    if (draft.trim() && draft !== props.value) props.onCommit(draft.trim());
    else setDraft(props.value);
  };
  return (
    <input
      type="text"
      className="cwyc-setting-input"
      data-testid={props.testid}
      value={draft}
      size={10}
      onChange={(e) => setDraft(e.target.value)}
      onBlur={commit}
      onKeyDown={(e) => {
        if (e.key === "Enter") commit();
        else if (e.key === "Escape") setDraft(props.value);
      }}
    />
  );
}

function NumberInput(props: {
  value: number;
  testid: string;
  min: number;
  max: number;
  onCommit: (value: number) => void;
}) {
  const [draft, setDraft] = useState(String(props.value));
  useEffect(() => setDraft(String(props.value)), [props.value]);
  const commit = () => {
    const parsed = Number.parseInt(draft, 10);
    if (Number.isFinite(parsed)) {
      const value = Math.max(props.min, Math.min(props.max, parsed));
      if (value !== props.value) props.onCommit(value);
      setDraft(String(value));
    } else {
      setDraft(String(props.value));
    }
  };
  return (
    <input
      type="number"
      className="cwyc-setting-number"
      data-testid={props.testid}
      value={draft}
      min={props.min}
      max={props.max}
      inputMode="numeric"
      onChange={(e) => setDraft(e.target.value)}
      onBlur={commit}
      onKeyDown={(e) => {
        if (e.key === "Enter") commit();
        else if (e.key === "Escape") setDraft(String(props.value));
      }}
    />
  );
}

export function SettingsPanel({
  store,
  focusSection = null,
  focusRequest = 0,
}: {
  store: ChatStore;
  focusSection?: "learning" | null;
  focusRequest?: number;
}) {
  const ui = useChatState(store).ui;
  const settings = ui.settings;
  const [doctorRequested, setDoctorRequested] = useState(false);
  const learningSectionRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (focusSection !== "learning") return;
    const frame = window.requestAnimationFrame(() => {
      learningSectionRef.current?.scrollIntoView({ block: "start" });
    });
    return () => window.cancelAnimationFrame(frame);
  }, [focusSection, focusRequest]);

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
          <Toggle
            label="Inline widgets (sandboxed)"
            testid="setting-widget-rendering"
            checked={settings.widgetRendering}
            onChange={(v) => store.setSetting("widget_rendering", v)}
          />
          <div className="cwyc-setting-footnote">
            Lets the agent draw charts and small interactive views in the chat. They run
            in a strict sandbox: no internet, no access to your collection or this app.
          </div>
          <div
            ref={learningSectionRef}
            className="cwyc-panel-title cwyc-panel-title-gap"
            data-testid="settings-learning-section"
          >
            Learning from your edits
          </div>
          <div className="cwyc-setting-footnote cwyc-setting-explanation">
            When you correct a card the agent wrote, CWYC keeps the difference as
            evidence about your writing preferences.
          </div>
          <div className="cwyc-setting-subhead">Analyze when either happens</div>
          <div className="cwyc-setting-row">
            <span className="cwyc-setting-label">Corrected cards reach</span>
            <NumberInput
              value={settings.learningNudgeThreshold}
              min={1}
              max={10_000}
              testid="setting-learning-threshold"
              onCommit={(value) => store.setSetting("learning_nudge_threshold", value)}
            />
          </div>
          <div className="cwyc-setting-row">
            <span className="cwyc-setting-label">Oldest correction waits (days)</span>
            <NumberInput
              value={settings.learningNudgeDays}
              min={1}
              max={3_650}
              testid="setting-learning-days"
              onCommit={(value) => store.setSetting("learning_nudge_days", value)}
            />
          </div>
          <div className="cwyc-setting-row cwyc-setting-choice-row">
            <span className="cwyc-setting-label">When due</span>
            <div className="cwyc-seg" role="radiogroup" aria-label="Run learning analysis">
              <button
                type="button"
                role="radio"
                aria-checked={settings.learningRunMode === "chat"}
                className={"cwyc-seg-btn" + (settings.learningRunMode === "chat" ? " cwyc-active" : "")}
                data-testid="setting-learning-chat"
                onClick={() => store.setSetting("learning_run_mode", "chat")}
              >
                Offer a chat
              </button>
              <button
                type="button"
                role="radio"
                aria-checked={settings.learningRunMode === "background"}
                className={"cwyc-seg-btn" + (settings.learningRunMode === "background" ? " cwyc-active" : "")}
                data-testid="setting-learning-background"
                onClick={() => store.setSetting("learning_run_mode", "background")}
              >
                Background
              </button>
            </div>
          </div>
          <div className="cwyc-setting-footnote">
            Offer a chat waits for you to click Review patterns. Background uses
            an isolated agent session while Anki is open and never replaces the
            chat you are reading.
          </div>
          <div className="cwyc-setting-row cwyc-setting-choice-row">
            <span className="cwyc-setting-label">Update writing guidance</span>
            <div className="cwyc-seg" role="radiogroup" aria-label="Apply writing guidance updates">
              <button
                type="button"
                role="radio"
                aria-checked={settings.skillUpdatePolicy === "review"}
                className={"cwyc-seg-btn" + (settings.skillUpdatePolicy === "review" ? " cwyc-active" : "")}
                data-testid="setting-skill-update-review"
                onClick={() => store.setSetting("skill_update_policy", "review")}
              >
                Ask me
              </button>
              <button
                type="button"
                role="radio"
                aria-checked={settings.skillUpdatePolicy === "automatic"}
                className={"cwyc-seg-btn" + (settings.skillUpdatePolicy === "automatic" ? " cwyc-active" : "")}
                data-testid="setting-skill-update-automatic"
                onClick={() => store.setSetting("skill_update_policy", "automatic")}
              >
                Automatically
              </button>
            </div>
          </div>
          {settings.skillUpdatePolicy === "automatic" ? (
            <div className="cwyc-setting-footnote cwyc-setting-warn">
              Guidance updates change how future cards are written. The previous
              version is archived before each automatic update.
            </div>
          ) : null}
          <div className="cwyc-panel-title cwyc-panel-title-gap">Reviewing</div>
          <Toggle
            label="Show the Set aside button"
            testid="setting-defer-button"
            checked={settings.deferButton}
            onChange={(v) => store.setSetting("defer_button", v)}
          />
          <Toggle
            label="Set the card aside when I send"
            testid="setting-defer-on-send"
            checked={settings.deferOnSend}
            onChange={(v) => store.setSetting("defer_on_send", v)}
          />
          <div className="cwyc-setting-row">
            <span className="cwyc-setting-label">Set-aside shortcut</span>
            <ShortcutInput
              value={settings.deferShortcut}
              testid="setting-defer-shortcut"
              onCommit={(chord) => store.setSetting("defer_shortcut", chord)}
            />
          </div>
          <div className="cwyc-setting-footnote">
            A set-aside card leaves today&rsquo;s queue and comes back on its own
            tomorrow — or sooner via Undo (the chip, or Anki&rsquo;s own Cmd+Z).
            Its scheduling is untouched.
          </div>
          <div className="cwyc-panel-title cwyc-panel-title-gap">MCP tools (advanced)</div>
          <Toggle
            label="Use my Claude Code MCP servers"
            testid="setting-mcp-inherit"
            checked={settings.mcpInheritUser}
            onChange={(v) => store.setSetting("mcp_inherit_user", v)}
          />
          <div className="cwyc-setting-row">
            <span className="cwyc-setting-label">
              Custom servers (<code>mcp_servers</code>)
            </span>
            <button
              type="button"
              className="cwyc-chip"
              data-testid="open-mcp-config"
              title="Add MCP servers, or disable inherited ones, in the add-on config"
              onClick={() => store.openAddonConfig()}
            >
              Edit servers…
            </button>
          </div>
          <div className="cwyc-setting-footnote cwyc-setting-warn">
            Off by default: card content is untrusted, so the agent only sees this
            add-on&rsquo;s <code>anki</code> tools. Widening lets a booby-trapped
            deck reach whatever those servers expose — enable only for collections
            you trust. Takes effect on your next new chat.
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
          <div className="cwyc-setting-row">
            <span className="cwyc-setting-label">Set card aside</span>
            <span className="cwyc-setting-kbd">{settings.deferShortcut}</span>
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
