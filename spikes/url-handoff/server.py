"""Proposal service spike: a card proposal is a durable resource with an id,
a revision, and a state machine. Any client renders a view of it and posts a
verb back by id. Nothing here touches an Anki collection.

Run standalone:
    python3 server.py --serve                # just the HTTP service
    python3 server.py --propose              # create one, print URL, poll (the "floor")
"""

from __future__ import annotations

import argparse
import json
import secrets
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

PENDING = "pending"
APPROVED = "approved"
REJECTED = "rejected"
REVISED = "revised"

TERMINAL = {APPROVED, REJECTED, REVISED}


@dataclass
class Proposal:
    id: str
    deck: str
    front: str
    back: str
    rev: int = 1
    state: str = PENDING
    event: threading.Event = field(default_factory=threading.Event)

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "deck": self.deck,
            "front": self.front,
            "back": self.back,
            "rev": self.rev,
            "state": self.state,
        }


class Store:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: dict[str, Proposal] = {}

    def create(self, deck: str, front: str, back: str) -> Proposal:
        with self._lock:
            pid = secrets.token_hex(8)
            p = Proposal(id=pid, deck=deck, front=front, back=back)
            self._items[pid] = p
            return p

    def get(self, pid: str) -> Proposal | None:
        with self._lock:
            return self._items.get(pid)

    def resolve(self, pid: str, verb: str, rev: int, payload: dict[str, Any]) -> tuple[int, dict]:
        """Gerrit's lesson: a decision binds to the revision it was made against."""
        with self._lock:
            p = self._items.get(pid)
            if p is None:
                return 404, {"error": "no such proposal"}
            if p.state in TERMINAL:
                return 409, {"error": f"already {p.state}", "state": p.state}
            if rev != p.rev:
                return 409, {"error": "stale revision", "expected": p.rev, "got": rev}

            if verb == "approve":
                p.state = APPROVED
            elif verb == "reject":
                p.state = REJECTED
            elif verb == "revise":
                p.front = payload.get("front", p.front)
                p.back = payload.get("back", p.back)
                p.rev += 1
                p.state = REVISED
            else:
                return 400, {"error": "unknown verb"}

            p.event.set()
            return 200, p.public()

    def wait(self, pid: str, timeout: float) -> Proposal | None:
        p = self.get(pid)
        if p is None:
            return None
        p.event.wait(timeout)
        return p


STORE = Store()

PAGE = """<!doctype html>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Proposal {id}</title>
<style>
  :root {{ color-scheme: light dark; --fg:#1a1a18; --bg:#faf9f5; --dim:#6b6a65; --line:#dcdad2; --card:#fff; }}
  @media (prefers-color-scheme:dark) {{
    :root {{ --fg:#e8e6e0; --bg:#1a1a18; --dim:#93918a; --line:#3a3936; --card:#232320; }}
  }}
  body {{ font:15px/1.6 ui-sans-serif,system-ui,sans-serif; background:var(--bg); color:var(--fg);
         margin:0; padding:2rem 1rem; display:flex; justify-content:center; }}
  main {{ width:100%; max-width:640px; }}
  .card {{ background:var(--card); border:1px solid var(--line); border-radius:12px; padding:1.25rem 1.5rem; }}
  .row {{ display:flex; justify-content:space-between; align-items:center; gap:1rem; }}
  .meta {{ font:12px/1.5 ui-monospace,monospace; color:var(--dim); margin:.5rem 0 1rem; }}
  .chip {{ font-size:12px; padding:3px 10px; border-radius:6px; border:1px solid var(--line); }}
  h1 {{ font-size:16px; font-weight:500; margin:0; }}
  label {{ display:block; font-size:12px; color:var(--dim); margin:1rem 0 .3rem; }}
  textarea {{ width:100%; box-sizing:border-box; font:14px/1.6 ui-sans-serif,system-ui,sans-serif;
              background:transparent; color:var(--fg); border:1px solid var(--line);
              border-radius:6px; padding:.6rem .7rem; resize:vertical; }}
  .actions {{ display:flex; gap:.5rem; margin-top:1.25rem; flex-wrap:wrap; }}
  button {{ flex:1; min-width:110px; font:14px ui-sans-serif,system-ui,sans-serif; cursor:pointer;
            padding:.6rem 1rem; border-radius:6px; border:1px solid var(--line);
            background:transparent; color:var(--fg); }}
  button:hover {{ border-color:var(--dim); }}
  #done {{ display:none; margin-top:1.25rem; font-size:14px; color:var(--dim); }}
</style>
<main>
  <div class="card">
    <div class="row">
      <h1>Proposed note</h1>
      <span class="chip" id="chip">{state} &middot; rev {rev}</span>
    </div>
    <div class="meta">proposal {id} &middot; deck: {deck}</div>
    <div id="form">
      <label for="front">Front</label>
      <textarea id="front" rows="2">{front}</textarea>
      <label for="back">Back</label>
      <textarea id="back" rows="4">{back}</textarea>
      <div class="actions">
        <button onclick="send('approve')">Approve</button>
        <button onclick="send('revise')">Save edit</button>
        <button onclick="send('reject')">Reject</button>
      </div>
    </div>
    <div id="done"></div>
  </div>
</main>
<script>
  const ID = {id_json}, REV = {rev};
  async function send(verb) {{
    const body = {{ rev: REV, front: document.getElementById('front').value,
                    back: document.getElementById('back').value }};
    const r = await fetch(`/p/${{ID}}/${{verb}}`, {{
      method: 'POST', headers: {{'content-type': 'application/json'}}, body: JSON.stringify(body) }});
    const j = await r.json();
    const done = document.getElementById('done');
    if (!r.ok) {{ done.textContent = 'Rejected by server: ' + (j.error || r.status); }}
    else {{
      document.getElementById('chip').textContent = j.state + ' \\u00b7 rev ' + j.rev;
      done.textContent = 'Recorded. You can return to your agent \\u2014 it is already unblocked.';
    }}
    document.getElementById('form').style.display = 'none';
    done.style.display = 'block';
  }}
</script>
"""


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_a):  # keep stdio clean; MCP uses it
        pass

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj: dict) -> None:
        self._send(code, json.dumps(obj).encode(), "application/json")

    def do_GET(self) -> None:
        parts = [p for p in self.path.split("?")[0].split("/") if p]
        if len(parts) == 2 and parts[0] == "p":
            p = STORE.get(parts[1])
            if p is None:
                return self._json(404, {"error": "no such proposal"})
            html = PAGE.format(
                id=p.id, id_json=json.dumps(p.id), deck=p.deck, rev=p.rev, state=p.state,
                front=p.front.replace("<", "&lt;"), back=p.back.replace("<", "&lt;"),
            )
            return self._send(200, html.encode(), "text/html; charset=utf-8")
        if len(parts) == 3 and parts[0] == "p" and parts[2] == "state":
            p = STORE.get(parts[1])
            return self._json(200, p.public()) if p else self._json(404, {"error": "no such proposal"})
        self._json(404, {"error": "not found"})

    def do_POST(self) -> None:
        parts = [p for p in self.path.split("?")[0].split("/") if p]
        if len(parts) != 3 or parts[0] != "p":
            return self._json(404, {"error": "not found"})
        n = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(n) or b"{}")
        except json.JSONDecodeError:
            return self._json(400, {"error": "bad json"})
        code, body = STORE.resolve(parts[1], parts[2], int(payload.get("rev", -1)), payload)
        self._json(code, body)


def start(port: int = 8787) -> tuple[ThreadingHTTPServer, str]:
    """Bind loopback only. Try `port`, then the next few."""
    last: OSError | None = None
    for candidate in range(port, port + 8):
        try:
            httpd = ThreadingHTTPServer(("127.0.0.1", candidate), Handler)
        except OSError as exc:
            last = exc
            continue
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        return httpd, f"http://127.0.0.1:{candidate}"
    raise last or OSError("no free port")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--serve", action="store_true")
    ap.add_argument("--propose", action="store_true")
    ap.add_argument("--timeout", type=float, default=300.0)
    args = ap.parse_args()

    _, base = start()
    if args.serve:
        print(f"listening on {base}")
        while True:
            time.sleep(3600)

    if args.propose:
        p = STORE.create(
            deck="Quantitative reasoning",
            front="What does a Sharpe ratio of 0 imply about an asset's excess return?",
            back="Its returns equal the risk-free rate.",
        )
        print(f"Review this proposal, then come back:\n\n    {base}/p/{p.id}\n")
        STORE.wait(p.id, args.timeout)
        print(json.dumps(STORE.get(p.id).public(), indent=2))  # type: ignore[union-attr]


if __name__ == "__main__":
    main()
