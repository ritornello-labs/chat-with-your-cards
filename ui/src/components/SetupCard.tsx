import { useState } from "react";
import type { ChatStore } from "../store";

/**
 * First-run onboarding (task #19). Renders in the thread's empty state
 * (Thread.tsx's ThreadPrimitive.Empty) when Python has pushed "setup_needed"
 * because Claude Code couldn't be found - see controller.py's
 * `_build_backend`. The chat still works via the built-in demo backend, so
 * this is an invitation, not a blocker: it explains the one thing missing,
 * gives copy-pasteable next steps, and offers a one-click "Re-check" that
 * needs NO Anki restart (ChatController.recheck_backend() rebuilds the
 * backend/session in-process - see DESIGN.md section 9's "no restart"
 * contract, same mechanism new_chat() already relies on).
 */

const INSTALL_COMMANDS: Record<string, { label: string; command: string }> = {
  darwin: { label: "macOS (Terminal)", command: "curl -fsSL https://claude.ai/install.sh | bash" },
  linux: { label: "Linux (Terminal)", command: "curl -fsSL https://claude.ai/install.sh | bash" },
  windows: { label: "Windows (PowerShell)", command: "irm https://claude.ai/install.ps1 | iex" },
};

function installFor(platform: string): { label: string; command: string } {
  return INSTALL_COMMANDS[platform] ?? INSTALL_COMMANDS.linux;
}

/** Best-effort clipboard copy: the async Clipboard API first, an
 *  execCommand fallback (older WebEngine builds) second, silent no-op if
 *  neither works - the code is still plainly selectable either way. */
function copyText(text: string): Promise<boolean> {
  if (navigator.clipboard?.writeText) {
    return navigator.clipboard.writeText(text).then(
      () => true,
      () => legacyCopy(text)
    );
  }
  return Promise.resolve(legacyCopy(text));
}

function legacyCopy(text: string): boolean {
  try {
    const el = document.createElement("textarea");
    el.value = text;
    el.style.position = "fixed";
    el.style.opacity = "0";
    document.body.appendChild(el);
    el.focus();
    el.select();
    const ok = document.execCommand("copy");
    document.body.removeChild(el);
    return ok;
  } catch {
    return false;
  }
}

function CopyableCommand({ command }: { command: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <div className="cwyc-setup-code-row">
      <code className="cwyc-setup-code">{command}</code>
      <button
        type="button"
        className="cwyc-chip cwyc-setup-copy"
        data-testid="setup-copy"
        onClick={() => {
          copyText(command).then((ok) => {
            if (ok) {
              setCopied(true);
              window.setTimeout(() => setCopied(false), 1800);
            }
          });
        }}
      >
        {copied ? "Copied" : "Copy"}
      </button>
    </div>
  );
}

export function SetupCard({ platform, store }: { platform: string; store: ChatStore }) {
  const install = installFor(platform);
  const [checking, setChecking] = useState(false);

  const onRecheck = () => {
    setChecking(true);
    store.recheckBackend();
    // No explicit success/failure round-trip signal to key off here - Python
    // replies with either "setup_resolved" (card unmounts) or a fresh
    // "notice" (still missing). Re-enable the button either way after a
    // moment so a slow/failed check doesn't leave it stuck.
    window.setTimeout(() => setChecking(false), 1500);
  };

  return (
    <div className="cwyc-setup-card" data-testid="setup-card">
      <div className="cwyc-setup-title">Let's get real answers flowing</div>
      <p className="cwyc-setup-lede">
        This chat is running on a built-in demo right now. For real answers about your cards, it
        needs <strong>Claude Code</strong>, a command-line tool from Anthropic that this add-on
        talks to. The tool itself is a free download; using the AI requires a Claude
        account with Claude Code access.
      </p>

      <ol className="cwyc-setup-steps">
        <li>
          <div className="cwyc-setup-step-title">Install Claude Code — {install.label}</div>
          <CopyableCommand command={install.command} />
          <div className="cwyc-setup-step-note">
            <a href="https://code.claude.com/docs/en/setup" target="_blank" rel="noreferrer">
              Full install instructions ↗
            </a>
          </div>
        </li>
        <li>
          <div className="cwyc-setup-step-title">Sign in to Claude Code</div>
          <div className="cwyc-setup-step-note">
            Open a terminal, run <code>claude</code>, and follow the link it shows. CWYC uses
            that same official CLI login; it does not accept or store API keys.
          </div>
        </li>
        <li>
          <div className="cwyc-setup-step-title">You're set — no restart needed</div>
          <div className="cwyc-setup-step-note">
            Once Claude Code is installed and signed in, hit Re-check below. Anki does not need to
            restart.
          </div>
          <button
            type="button"
            className="cwyc-chip cwyc-chip-primary"
            data-testid="setup-recheck"
            disabled={checking}
            onClick={onRecheck}
          >
            {checking ? "Checking…" : "Re-check"}
          </button>
        </li>
      </ol>
    </div>
  );
}
