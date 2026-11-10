"""Exercise the real MCP settings form against a disposable Anki config."""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Any, Callable


def run(
    mw: Any, dock: Any, addon: Any, evaluate: Callable[..., Any], wait: Callable[..., Any]
) -> dict[str, Any]:
    original = copy.deepcopy(mw.addonManager.getConfig("chat_with_your_cards") or {})
    before = (mw.col.note_count(), mw.col.card_count())

    def js(code: str) -> Any:
        return evaluate(dock.web, code, 5000, "MCP form")

    def click(selector: str) -> None:
        js(f"document.querySelector({json.dumps(selector)}).click(); true;")

    def ready(selector: str) -> None:
        wait(lambda: js(f"!!document.querySelector({json.dumps(selector)})"), 5000, "MCP control")

    def fill(selector: str, value: str) -> None:
        js(
            "(function(){var el=document.querySelector(" + json.dumps(selector) + ");"
            "var proto=el.tagName==='TEXTAREA'?HTMLTextAreaElement.prototype:HTMLInputElement.prototype;"
            "Object.getOwnPropertyDescriptor(proto,'value').set.call(el," + json.dumps(value) + ");"
            "el.dispatchEvent(new Event('input',{bubbles:true}));return true;})();"
        )

    def config() -> dict[str, Any]:
        return mw.addonManager.getConfig("chat_with_your_cards") or {}

    def saved(name: str) -> None:
        wait(
            lambda: (
                name in config().get("mcp_servers", {})
                and not js("!!document.querySelector('[data-testid=mcp-editor]')")
            ),
            5000,
            "MCP server saved",
        )

    def shot(name: str) -> None:
        directory = os.environ.get("CWYC_MCP_SCREENSHOT_DIR")
        if directory:
            target = Path(directory) / name
            target.parent.mkdir(parents=True, exist_ok=True)
            js(
                "document.querySelector('[data-testid=mcp-settings]').scrollIntoView({block:'start'}); true;"
            )
            mw.grab().save(str(target), "PNG")

    try:
        # Every run starts from synthetic settings, never a user's profile.
        base = {**original, "mcp_servers": {}, "mcp_disabled": [], "mcp_inherit_user": False}
        mw.addonManager.writeConfig("chat_with_your_cards", base)
        addon.state.config.update(base)
        if not js("!!document.querySelector('[data-testid=settings-panel]')"):
            click("[data-testid=settings]")
        ready("[data-testid=mcp-add]")
        click("[data-testid=mcp-add]")
        ready("[data-testid=mcp-name]")
        fill("[data-testid=mcp-name]", "reference-demo")
        fill("[data-testid=mcp-url]", "https://example.com/mcp")
        click(".cwyc-mcp-editor summary")
        click(".cwyc-mcp-editor .cwyc-mcp-values > button")
        ready('input[aria-label="Headers name 1"]')
        fill('input[aria-label="Headers name 1"]', "Authorization")
        fill('input[aria-label="Headers value 1"]', "synthetic-demo-value")
        shot("mcp-editor-light.png")
        click("[data-testid=mcp-save]")
        saved("reference-demo")

        click("[data-testid=mcp-server-reference-demo] input")
        wait(lambda: "reference-demo" in config().get("mcp_disabled", []), 5000, "MCP disabled")
        # Wait for the response to re-enable controls after persistence.
        wait(
            lambda: js("!document.querySelector('[data-testid=mcp-settings] fieldset').disabled"),
            5000,
            "MCP response",
        )
        click("[data-testid=mcp-server-reference-demo] button")
        ready("[data-testid=mcp-editor]")
        if js("document.querySelector('input[aria-label=\"Headers value 1\"]').value") != "":
            raise AssertionError("Saved header was exposed in the editor")
        fill("[data-testid=mcp-url]", "https://example.org/mcp")
        click("[data-testid=mcp-save]")
        saved("reference-demo")
        spec = config()["mcp_servers"]["reference-demo"]
        if spec["headers"] != {"Authorization": "synthetic-demo-value"}:
            raise AssertionError("Editing a URL lost the saved header")

        click("[data-testid=mcp-add]")
        ready("[data-testid=mcp-name]")
        fill("[data-testid=mcp-name]", "local-demo")
        js(
            "var el=document.querySelector('[data-testid=mcp-transport]'); el.value='stdio'; el.dispatchEvent(new Event('change',{bubbles:true})); true;"
        )
        ready("[data-testid=mcp-command]")
        fill("[data-testid=mcp-command]", "example-mcp")
        fill("[data-testid=mcp-args]", "--folder\npath with spaces")
        click("[data-testid=mcp-save]")
        saved("local-demo")
        if config()["mcp_servers"]["local-demo"]["args"] != ["--folder", "path with spaces"]:
            raise AssertionError("Command argument boundaries were changed")

        click("[data-testid=setting-mcp-inherit]")
        wait(lambda: config().get("mcp_inherit_user") is True, 5000, "MCP inheritance")
        wait(
            lambda: js("!document.querySelector('[data-testid=mcp-settings] fieldset').disabled"),
            5000,
            "MCP response",
        )
        shot("mcp-list-light.png")
        js(
            "document.documentElement.classList.add('nightMode'); document.body.classList.add('nightMode'); true;"
        )
        mw.repaint()
        shot("mcp-list-dark.png")
        js(
            "document.documentElement.classList.remove('nightMode'); document.body.classList.remove('nightMode'); true;"
        )

        click("[data-testid=mcp-server-reference-demo] .cwyc-mcp-actions button:last-child")
        ready(".cwyc-mcp-confirm")
        click(".cwyc-mcp-confirm button")
        wait(lambda: "reference-demo" not in config().get("mcp_servers", {}), 5000, "MCP removal")
        if "reference-demo" not in config()["mcp_disabled"]:
            raise AssertionError("Removal unblocked an inherited server")
        if before != (mw.col.note_count(), mw.col.card_count()):
            raise AssertionError("MCP settings changed the collection")
        return {
            "http": True,
            "stdio": True,
            "toggle": True,
            "edit_keeps_header": True,
            "inherit": True,
            "remove": True,
            "collection_unchanged": True,
        }
    finally:
        mw.addonManager.writeConfig("chat_with_your_cards", original)
        addon.state.config.clear()
        addon.state.config.update({**addon.DEFAULT_CONFIG, **original})
        if js("!!document.querySelector('[data-testid=settings-panel]')"):
            click("[data-testid=settings]")
