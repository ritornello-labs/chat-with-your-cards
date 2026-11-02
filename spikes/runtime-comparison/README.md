# Isolated runtime comparison

`probe.py` exercises real runtimes against scripted loopback model responses and
CWYC's real MCP HTTP transport with a synthetic read-only executor. It never
loads an Anki profile or authenticates to a model provider. This tests adapter
compatibility, not model quality or subscription entitlement.

See the [comparison and recommendation](../../docs/RUNTIME_COMPARISON_2026-11-02.md).

## Run

Use installed, trusted binaries. Keep the output directory **outside Git**;
protocol files include absolute fixture paths and an ephemeral MCP bearer token.
The script creates isolated per-child homes/configs and stops its processes and
servers on completion or error. Each run requires a fresh output directory.

```sh
python3 spikes/runtime-comparison/probe.py \
  --runtime codex --binary /path/to/codex-native-binary \
  --output /absolute/private/codex-evaluation

python3 spikes/runtime-comparison/probe.py \
  --runtime claude --binary /path/to/claude \
  --output /absolute/private/claude-evaluation
```

For a version-manager Node installation, set `CWYC_EVAL_NODE_BIN` to the real
Node executable so the isolated child does not depend on the original home.
A home-dependent Codex wrapper should be resolved to its underlying native
executable. Never copy real auth files into the fixture.

The optional Pi fixture pins version 1.4.0 with a pnpm lockfile and a seven-day
release-age policy. It is separate from the add-on's dependencies and is never
packaged into the `.ankiaddon`.

```sh
sfw pnpm --dir spikes/runtime-comparison/pi install --frozen-lockfile --ignore-scripts
python3 spikes/runtime-comparison/probe.py \
  --runtime pi --binary spikes/runtime-comparison/pi/node_modules/.bin/pi \
  --output /absolute/private/pi-evaluation
```

The probe resolves relative binary paths before changing its child directory.
`result.json` records booleans and fixture call counts; failed checks exit nonzero.
Raw protocol/model
captures remain outside Git. No network is required except to install the pinned
Pi fixture; runtime requests themselves go to loopback.
