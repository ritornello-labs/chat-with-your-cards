# Spike: URL handoff for proposal review

**Question this answers:** can an agent running in a text-only host (Claude Code, Codex)
gate a card on human approval by handing off to a browser, and does that feel good enough
to build on?

Nothing here touches an Anki collection. Proposals live in memory. Kill the process, they
are gone. This is a spike, not a component.

## The shape

A proposal is a durable resource with an `id`, a `rev`, and a state machine
(`pending → approved | rejected | revised`). Clients render a *view* of it and post *verbs*
back by id. The agent never sees a card; it sees a URL and a verdict.

Two lessons are wired in deliberately, because they are the ones that are expensive to
retrofit:

- **Decisions bind to a revision** (Gerrit). Approving `rev 1` after the agent has moved to
  `rev 2` returns `409 stale revision` instead of silently applying to changed content.
- **Resolution is idempotent.** A second verb on a resolved proposal returns `409 already
  approved`, so a double-tap or a replayed request can't write twice.

`propose_card` blocks until a human resolves the proposal in the browser, or times out and
hands back the id. That is the OAuth device-grant pattern (RFC 8628): the constrained host
delegates the rich UI to a browser and waits.

## Try it without any agent

```bash
python3 server.py --propose
```

Prints a URL, blocks. Open it, hit a button, watch the process print the verdict and exit.
That is the entire mechanism, and it works in any host that can display a line of text.

## Try it in Claude Code

```bash
claude mcp add anki-proposals -s local -- python3 "$PWD/mcp_stdio.py"
```

Restart Claude Code, then ask it to propose a card. Check: does it show the URL as a
clickable link, does it visibly wait, does it get the verdict?

## Try it in Codex

Add to `~/.codex/config.toml`:

```toml
[mcp_servers.anki-proposals]
command = "python3"
args = ["<absolute path to this repo>/spikes/url-handoff/mcp_stdio.py"]
startup_timeout_sec = 30
```

## What to watch for

The point of running it in both hosts is to compare *feel*, not function — function is
already verified. Specifically:

- Is the URL clickable, or do you have to select and copy it?
- Does a blocking tool call read as "working" or as "hung"? Some hosts cap tool duration
  well below the 180s default; if it times out, `check_proposal` is the recovery path.
- Having approved in the browser, does returning to the terminal feel like one workflow or
  two?

If the answer to the last one is "two," that is the finding, and it argues for the review
queue living somewhere you already are — a phone, a dock — rather than for a better URL.

## Known limits (deliberate)

- In-memory store: every MCP server launch starts empty.
- Loopback only (`127.0.0.1`). No auth, because nothing outside this machine can reach it.
  The moment that changes, it needs real auth — this is exactly the seam where a Funnel or
  a Telegram `initData` HMAC would go.
- The revision guard is enforced server-side, but the page reads its `rev` at render time,
  so a stale tab surfaces the 409 rather than preventing the click. That is the correct
  place to catch it.
