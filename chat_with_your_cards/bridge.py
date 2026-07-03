"""JS <-> Python messaging for the chat webview.

JS to Python: pycmd("cwyc:" + JSON.stringify({type, ...})). The "cwyc:"
prefix is collision insurance: the global webview_did_receive_js_message
filter sees every message on our webview before our handler does.

Python to JS: bridge.push(payload) evaluates chatUI.dispatch(payload).
Payloads are event_to_dict() events plus UI-directed messages such as
{"type": "reset"} and {"type": "cancelled"}.
"""

from __future__ import annotations

import json
from typing import Any, Callable

PREFIX = "cwyc:"

MessageHandler = Callable[[dict[str, Any]], None]


class Bridge:
    def __init__(self, web: Any) -> None:
        self._web = web
        self._handlers: dict[str, MessageHandler] = {}
        web.set_bridge_command(self._on_bridge_cmd, self)

    def on(self, msg_type: str, handler: MessageHandler) -> None:
        self._handlers[msg_type] = handler

    def push(self, payload: dict[str, Any]) -> None:
        self._web.eval(f"window.chatUI && window.chatUI.dispatch({json.dumps(payload)});")

    def _on_bridge_cmd(self, message: str) -> Any:
        if not message.startswith(PREFIX):
            return None
        try:
            payload = json.loads(message[len(PREFIX) :])
        except json.JSONDecodeError:
            return None
        msg_type = payload.get("type", "")
        handler = self._handlers.get(msg_type)
        if handler is None:
            return None
        handler(payload)
        return True
