/** Cross-component navigation into a specific part of the header Settings
 * panel. The composer nudge cannot own Header's popover state, so it sends a
 * document event; Header opens the panel and passes the requested section to
 * SettingsPanel for the actual scroll. */

export const OPEN_SETTINGS_EVENT = "cwyc:open-settings";

export type SettingsSection = "learning";

export function openSettingsSection(section: SettingsSection): void {
  if (typeof document === "undefined") return;
  document.dispatchEvent(
    new CustomEvent(OPEN_SETTINGS_EVENT, { detail: { section } })
  );
}
