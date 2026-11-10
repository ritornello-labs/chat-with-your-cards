import { useEffect, useState } from "react";
import { useChatState } from "../ChatRuntimeProvider";
import type { ChatStore } from "../store";
import type { McpEditor, McpValue } from "../mcpSettings";

function Values({ label, rows, onChange }: {
  label: string;
  rows: McpValue[];
  onChange: (rows: McpValue[]) => void;
}) {
  return (
    <details className="cwyc-mcp-values" open={rows.length > 0 || undefined}>
      <summary>{label}{rows.length ? ` (${rows.length})` : " (optional)"}</summary>
      {rows.map((row, i) => (
        <div className="cwyc-mcp-value" key={i}>
          <label>Name
            <input value={row.key} aria-label={`${label} name ${i + 1}`} readOnly={row.keep}
              onChange={(e) => onChange(rows.map((r, j) => j === i ? { ...r, key: e.target.value } : r))} />
          </label>
          <label>Value
            <input type="password" autoComplete="new-password" value={row.value}
              aria-label={`${label} value ${i + 1}`}
              placeholder={row.keep ? "Saved · leave unchanged" : "Enter value"}
              onChange={(e) => onChange(rows.map((r, j) => j === i ? { ...r, value: e.target.value, keep: false } : r))} />
          </label>
          <button type="button" className="cwyc-chip" aria-label={`Remove ${label.toLowerCase()} ${i + 1}`}
            onClick={() => onChange(rows.filter((_, j) => j !== i))}>Remove</button>
        </div>
      ))}
      <button type="button" className="cwyc-chip" onClick={() => onChange([...rows, { key: "", value: "", keep: false }])}>
        Add {label === "Headers" ? "header" : "variable"}
      </button>
    </details>
  );
}

export function McpSettings({ store }: { store: ChatStore }) {
  const response = useChatState(store).ui.mcp;
  const snapshot = response?.snapshot;
  const [pending, setPending] = useState<{ id: string; action: string } | null>(null);
  const [draft, setDraft] = useState<McpEditor | null>(null);
  const [editRevision, setEditRevision] = useState("");
  const [existing, setExisting] = useState(false);
  const [removing, setRemoving] = useState<string | null>(null);
  const [blockedName, setBlockedName] = useState("");
  const [error, setError] = useState("");
  const [status, setStatus] = useState("");

  const request = (action: string, data: Record<string, unknown> = {}) => {
    setError("");
    setStatus("");
    setPending({ id: store.mcpSettings(action, { revision: snapshot?.revision, ...data }), action });
  };
  useEffect(() => {
    setPending({ id: store.mcpSettings("read"), action: "read" });
  }, [store]);
  useEffect(() => {
    if (!pending || response?.request_id !== pending.id) return;
    setPending(null);
    if (response.error) {
      setError(response.error);
      return;
    }
    if (response.editor) {
      setDraft(response.editor);
      setExisting(true);
      setEditRevision(response.snapshot.revision);
    } else if (pending.action !== "read") {
      if (pending.action === "save") setDraft(null);
      setRemoving(null);
      setBlockedName("");
      setStatus("Saved. Applies to your next new chat.");
    }
  }, [response, pending]);

  const patch = (changes: Partial<McpEditor>) => setDraft(draft ? { ...draft, ...changes } : null);
  const busy = pending !== null;
  return (
    <section className="cwyc-mcp" data-testid="mcp-settings" aria-label="MCP servers">
      <div className="cwyc-setting-footnote">
        Connect tools and data to your assistant. Changes apply to new chats.
        External tools can access data and take actions outside Anki.
      </div>
      {error ? <div className="cwyc-mcp-feedback" role="alert">{error}</div> : null}
      {status ? <div className="cwyc-mcp-feedback" role="status">{status}</div> : null}
      {!snapshot ? <div className="cwyc-panel-empty">Loading servers…</div> : (
        <>
          {!draft ? <>
          <div className="cwyc-setting-row cwyc-mcp-server">
            <span><strong>Anki</strong><small>Built in</small></span>
            <span className="cwyc-mcp-badge" data-testid="mcp-builtin">Always on</span>
          </div>
          <fieldset disabled={busy || draft !== null} className="cwyc-mcp-controls">
            {snapshot.servers.map((server) => (
              <div className="cwyc-mcp-server" key={server.name} data-testid={`mcp-server-${server.name}`}>
                <label className="cwyc-setting-row">
                  <span className="cwyc-mcp-name"><strong>{server.name}</strong><small>
                    {server.transport === "stdio" ? "Local command" : server.transport.toUpperCase()}
                    {server.enabled ? " · Enabled for new chats" : " · Disabled"}
                  </small></span>
                  <input type="checkbox" className="cwyc-setting-check" checked={server.enabled}
                    aria-label={`Enable ${server.name}`} onChange={(e) => request("toggle", { name: server.name, enabled: e.target.checked })} />
                </label>
                <div className="cwyc-mcp-actions">
                  <button type="button" className="cwyc-chip" onClick={() => request("edit", { name: server.name })}>Edit</button>
                  <button type="button" className="cwyc-chip" onClick={() => setRemoving(server.name)}>Remove</button>
                </div>
                {removing === server.name ? <div className="cwyc-mcp-confirm">
                  <p>Remove {server.name}? Its saved connection settings will be deleted. The name stays blocked from inheritance.</p>
                  <button type="button" className="cwyc-chip" onClick={() => request("remove", { name: server.name })}>Remove server</button>{" "}
                  <button type="button" className="cwyc-chip" onClick={() => setRemoving(null)}>Cancel</button>
                </div> : null}
              </div>
            ))}
            {!snapshot.servers.length ? <div className="cwyc-setting-footnote">No custom servers yet.</div> : null}
            <div className="cwyc-mcp-actions">
              <button type="button" className="cwyc-chip" data-testid="mcp-add" onClick={() => {
                setExisting(false);
                setEditRevision(snapshot.revision);
                setError(""); setStatus(""); setRemoving(null);
                setDraft({ name: "", transport: "http", url: "", command: "", args: [], env: [], headers: [] });
              }}>Add server</button>
            </div>
            <label className="cwyc-setting-row">
              <span className="cwyc-setting-label">Use my Claude Code MCP servers</span>
              <input type="checkbox" className="cwyc-setting-check" data-testid="setting-mcp-inherit"
                checked={snapshot.inherit} onChange={(e) => request("inherit", { enabled: e.target.checked })} />
            </label>
            <div className="cwyc-setting-footnote">
              Other servers are managed by Claude Code. Block names below to prevent their tools being called here;
              Claude Code may still connect to inherited servers.
            </div>
            <details className="cwyc-mcp-values">
              <summary>Blocked server names{snapshot.blocked.length ? ` (${snapshot.blocked.length})` : ""}</summary>
              {snapshot.blocked.map((name) => <div className="cwyc-setting-row" key={name}>
                <span className="cwyc-mcp-name">{name}</span>
                <button type="button" className="cwyc-chip" onClick={() => request("toggle", { name, enabled: true })}>Unblock</button>
              </div>)}
              <form className="cwyc-mcp-block" onSubmit={(e) => { e.preventDefault(); request("toggle", { name: blockedName.trim(), enabled: false }); }}>
                <label>Block an inherited server by name
                  <input value={blockedName} onChange={(e) => setBlockedName(e.target.value)} placeholder="server-name" required />
                </label>
                <button type="submit" className="cwyc-chip">Block</button>
              </form>
            </details>
          </fieldset>
          </> : null}
          {draft ? <form data-testid="mcp-editor" className="cwyc-mcp-editor" onSubmit={(e) => {
            e.preventDefault();
            request("save", { revision: editRevision, name: draft.name.trim(), existing, server: draft });
          }}>
            <fieldset disabled={busy} className="cwyc-mcp-controls">
              <div className="cwyc-setting-subhead">{existing ? `Edit ${draft.name}` : "New server"}</div>
              <label>Server name
                <input data-testid="mcp-name" value={draft.name} readOnly={existing} autoFocus={!existing} required maxLength={100}
                  placeholder="my-server" onChange={(e) => patch({ name: e.target.value })} />
              </label>
              <label>Connection
                <select data-testid="mcp-transport" value={draft.transport} onChange={(e) => patch({ transport: e.target.value as McpEditor["transport"] })}>
                  <option value="http">HTTP</option><option value="sse">SSE</option><option value="stdio">Local command (stdio)</option>
                </select>
              </label>
              {draft.transport === "stdio" ? <>
                <label>Command
                  <input data-testid="mcp-command" value={draft.command} placeholder="/path/to/executable" autoFocus={existing} required onChange={(e) => patch({ command: e.target.value })} />
                </label>
                <label>Arguments · one per line
                  <textarea data-testid="mcp-args" rows={3} value={draft.args.join("\n")} onChange={(e) => patch({ args: e.target.value ? e.target.value.split("\n") : [] })} />
                </label>
                <Values label="Environment variables" rows={draft.env} onChange={(env) => patch({ env })} />
              </> : <>
                <label>Server URL
                  <input data-testid="mcp-url" type="url" value={draft.url} placeholder="https://example.com/mcp" autoFocus={existing} required onChange={(e) => patch({ url: e.target.value })} />
                </label>
                <Values label="Headers" rows={draft.headers} onChange={(headers) => patch({ headers })} />
              </>}
              <div className="cwyc-setting-footnote">Saved header and environment values stay hidden. Leave their fields unchanged to keep them. Saving a local command does not run it.</div>
              <div className="cwyc-mcp-actions">
                <button type="submit" className="cwyc-chip" data-testid="mcp-save">{busy ? "Saving…" : "Save server"}</button>
                <button type="button" className="cwyc-chip" onClick={() => { setDraft(null); setError(""); }}>Cancel</button>
                {existing && error ? <button type="button" className="cwyc-chip" onClick={() => request("edit", { name: draft.name })}>Reload saved</button> : null}
              </div>
            </fieldset>
          </form> : null}
        </>
      )}
    </section>
  );
}
