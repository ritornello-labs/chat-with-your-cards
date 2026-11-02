"""Human-operated MCP settings. Never expose these through agent tools/transcripts."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from typing import Any, Callable
from urllib.parse import urlsplit

KEYS = ("mcp_servers", "mcp_disabled", "mcp_inherit_user")
NAME = re.compile(r"[A-Za-z0-9_-]{1,100}\Z")


class McpSettingsError(ValueError):
    """A validation message safe to show in the settings form."""


def _servers(config: dict[str, Any]) -> dict[str, Any]:
    value = config.get("mcp_servers")
    return value if isinstance(value, dict) else {}


def _disabled(config: dict[str, Any]) -> list[str]:
    value = config.get("mcp_disabled")
    return (
        [v for v in value if isinstance(v, str) and v != "anki"] if isinstance(value, list) else []
    )


def _transport(spec: Any) -> str:
    if not isinstance(spec, dict):
        return "unsupported"
    return str(spec.get("type") or ("stdio" if "command" in spec else "http"))


def snapshot(config: dict[str, Any]) -> dict[str, Any]:
    """Only names/transport/state, never endpoints, arguments or credentials."""
    encoded = json.dumps({key: config.get(key) for key in KEYS}, sort_keys=True)
    disabled = _disabled(config)
    return {
        "revision": hashlib.sha256(encoded.encode()).hexdigest(),
        "inherit": bool(config.get("mcp_inherit_user", False)),
        "servers": [
            {"name": name, "transport": _transport(spec), "enabled": name not in disabled}
            for name, spec in sorted(_servers(config).items())
            if name != "anki"
        ],
        "blocked": sorted(set(disabled) - set(_servers(config))),
    }


def _name(value: Any) -> str:
    if not isinstance(value, str) or not NAME.fullmatch(value):
        raise McpSettingsError(
            "Use a server name with letters, numbers, underscores or hyphens (up to 100)."
        )
    if value == "anki":
        raise McpSettingsError("The built-in Anki server is always enabled and cannot be replaced.")
    return value


def _pairs(value: Any, previous: Any, *, env: bool) -> dict[str, str]:
    if not isinstance(value, list) or len(value) > 100:
        raise McpSettingsError("Provide at most 100 named values.")
    old = previous if isinstance(previous, dict) else {}
    result: dict[str, str] = {}
    for row in value:
        if not isinstance(row, dict) or not isinstance(row.get("key"), str):
            raise McpSettingsError("Each value needs a name.")
        key = row["key"].strip()
        pattern = r"[A-Za-z_][A-Za-z0-9_]*" if env else r"[!#$%&'*+.^_`|~0-9A-Za-z-]+"
        if not re.fullmatch(pattern, key) or key.lower() in {k.lower() for k in result}:
            raise McpSettingsError("Use valid, distinct names for environment variables and headers.")
        if row.get("keep") is True:
            if key not in old or not isinstance(old[key], str):
                raise McpSettingsError("A saved value changed. Reopen the server and try again.")
            result[key] = old[key]
        else:
            text = row.get("value")
            if not isinstance(text, str) or len(text) > 16_384 or "\x00" in text:
                raise McpSettingsError("Values must be text, up to 16,384 characters.")
            if not env and ("\r" in text or "\n" in text):
                raise McpSettingsError("Header values cannot contain line breaks.")
            result[key] = text
    return result


def editor(config: dict[str, Any], name: str) -> dict[str, Any]:
    spec = _servers(config).get(name)
    if not isinstance(spec, dict) or _transport(spec) not in ("http", "sse", "stdio"):
        raise McpSettingsError(
            "This server uses an unsupported format. Use Anki's Config editor to edit it."
        )
    return {
        "name": name,
        "transport": _transport(spec),
        "url": spec.get("url", ""),
        "command": spec.get("command", ""),
        "args": spec.get("args", []),
        # Blank password fields mean KEEP, not a round-trip of the real value.
        "env": [{"key": k, "value": "", "keep": True} for k in (spec.get("env") or {})],
        "headers": [{"key": k, "value": "", "keep": True} for k in (spec.get("headers") or {})],
    }


def mutate(config: dict[str, Any], msg: dict[str, Any]) -> dict[str, Any]:
    """Validate the whole edit before returning an isolated new config."""
    if msg.get("revision") != snapshot(config)["revision"]:
        raise McpSettingsError("MCP settings changed elsewhere. Reopen the server and try again.")
    updated = copy.deepcopy(config)
    servers = copy.deepcopy(_servers(config))
    disabled = _disabled(config)
    action = msg.get("action")
    if action == "inherit":
        if not isinstance(msg.get("enabled"), bool):
            raise McpSettingsError("Choose whether to use your Claude Code servers.")
        updated["mcp_inherit_user"] = msg["enabled"]
        return updated
    name = _name(msg.get("name"))
    if action == "toggle":
        if not isinstance(msg.get("enabled"), bool):
            raise McpSettingsError("Choose whether to enable the server.")
        disabled = [v for v in disabled if v != name]
        if not msg["enabled"]:
            disabled.append(name)
    elif action == "remove":
        if name not in servers:
            raise McpSettingsError("The server no longer exists. Refresh settings and try again.")
        del servers[name]
        # Keep it blocked: a same-name inherited server must not become active.
        if name not in disabled:
            disabled.append(name)
    elif action == "save":
        replacing = msg.get("existing") is True
        if (name in servers) != replacing:
            raise McpSettingsError("That server name already exists or was removed. Reopen settings.")
        draft = msg.get("server")
        if not isinstance(draft, dict) or draft.get("transport") not in ("stdio", "http", "sse"):
            raise McpSettingsError("Choose HTTP, SSE or a local command.")
        transport = draft["transport"]
        previous = servers.get(name, {})
        previous = previous if isinstance(previous, dict) else {}
        # Preserve advanced, hand-edited options (OAuth, timeout, etc.) when
        # editing within a transport. Switching transport starts a clean spec.
        spec = copy.deepcopy(previous) if _transport(previous) == transport else {}
        spec["type"] = transport
        if transport == "stdio":
            command, args = draft.get("command"), draft.get("args")
            if (
                not isinstance(command, str)
                or not command.strip()
                or len(command) > 4096
                or "\x00" in command
            ):
                raise McpSettingsError("Enter a command or executable path.")
            if (
                not isinstance(args, list)
                or len(args) > 200
                or any(not isinstance(v, str) or len(v) > 16_384 or "\x00" in v for v in args)
            ):
                raise McpSettingsError("Enter arguments as text, one per line (up to 200).")
            spec.update(command=command.strip(), args=args)
            spec["env"] = _pairs(draft.get("env", []), previous.get("env"), env=True)
        else:
            url = draft.get("url")
            if not isinstance(url, str):
                raise McpSettingsError("Enter a complete HTTP or HTTPS server URL.")
            try:
                parsed = urlsplit(url)
                valid = parsed.scheme in ("http", "https") and bool(parsed.hostname)
            except ValueError:
                valid = False
            if not valid or len(url) > 8192 or any(c.isspace() for c in url):
                raise McpSettingsError("Enter a complete HTTP or HTTPS server URL.")
            spec["url"] = url
            spec["headers"] = _pairs(draft.get("headers", []), previous.get("headers"), env=False)
        servers[name] = spec
    else:
        raise McpSettingsError("Unknown MCP settings action.")
    updated["mcp_servers"] = servers
    updated["mcp_disabled"] = sorted(set(disabled))
    return updated


def handle(
    msg: dict[str, Any],
    *,
    read: Callable[[], dict[str, Any]],
    write: Callable[[dict[str, Any]], None],
    push: Callable[[dict[str, Any]], None],
) -> None:
    """Private dock bridge only; no collection access, process spawn or network."""
    response: dict[str, Any] = {"type": "mcp_settings", "request_id": msg.get("request_id")}
    config: dict[str, Any] = {}
    try:
        config = read()
        if msg.get("action") == "edit":
            response["editor"] = editor(config, _name(msg.get("name")))
        elif msg.get("action") != "read":
            updated = mutate(config, msg)
            write({key: updated[key] for key in KEYS if updated.get(key) != config.get(key)})
            config = updated
    except McpSettingsError as exc:
        response["error"] = str(exc)
    except Exception:
        # Config values and write errors may contain credentials. Never echo.
        response["error"] = "Could not save MCP settings. Reopen settings and try again."
    response["snapshot"] = snapshot(config)
    push(response)
