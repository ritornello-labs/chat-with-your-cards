/**
 * Dev-mode scripted replayer: installs a fake window.pycmd so main.tsx's real
 * bridge.ts code path - postCommand() -> window.pycmd(...) - runs unmodified
 * against canned data instead of a live Python backend. Only src/dev-main.tsx
 * imports this file, so it never reaches the production bundle
 * (chat_with_your_cards/web/next/).
 *
 * Scripts are mined from chat_with_your_cards/backends/fixtures.py (same demo
 * copy, reimplemented here since this module owns TypeScript
 * timing/event-shape, not Python). Timing
 * mirrors backends/scripted.py: 25-60ms per 2-5 word delta, tool calls take
 * their declared duration_ms; "think_tokens" beats use a slower 200-450ms
 * cadence (mirrors backends/scripted.py's _THINK_MIN_MS/_THINK_MAX_MS) so
 * the rotating "Thinking…" indicator has time to actually rotate during
 * manual preview.
 *
 * thinking_delta mirrors backends/base.py's real ThinkingDelta event
 * (landed 2026-07-11): "think" emits real (non-empty) thinking text, same
 * as before; "think_tokens" emits the empty-text/growing-estimated_tokens
 * shape the real CLI actually produces today (text redacted upstream at
 * every effort level - see claude_cli.py's parser and DESIGN.md section 9),
 * exercising the Reasoning primitive's no-text rotating-indicator path.
 */
import type { ChatEvent } from "../events";
import type { ProposalPayload } from "../events";

type Step =
  | { kind: "think" | "text"; text: string }
  | { kind: "think_tokens"; tokens: readonly number[] }
  | { kind: "tool"; tool: string; summary: string; result: string; ok: boolean; durationMs: number }
  | { kind: "proposal"; proposal: ProposalPayload }
  | { kind: "image"; src: string; caption: string }
  | { kind: "widget"; html: string; title: string }
  | { kind: "widget_offer"; title: string }
  | { kind: "error"; message: string };

const DEMO_CSS =
  ".card{font-family:-apple-system,sans-serif;font-size:18px;text-align:center;color:#111;background:#fff;padding:12px}";

function chopWords(text: string, rand: () => number): string[] {
  const words = text.split(" ");
  const out: string[] = [];
  let i = 0;
  while (i < words.length) {
    const take = 2 + Math.floor(rand() * 4); // 2-5 words, mirrors backends/scripted.py
    let chunk = words.slice(i, i + take).join(" ");
    if (i + take < words.length) chunk += " ";
    out.push(chunk);
    i += take;
  }
  return out;
}

const devProposals = new Map<string, ProposalPayload>();

function compile(steps: readonly Step[]): Array<[number, ChatEvent]> {
  const timeline: Array<[number, ChatEvent]> = [];
  const rand = Math.random;
  const delay = () => 25 + Math.floor(rand() * 35); // 25-60ms
  let callCounter = 0;
  let endedWithError = false;
  for (const step of steps) {
    if (step.kind === "think") {
      for (const chunk of chopWords(step.text, rand)) {
        timeline.push([delay(), { type: "thinking_delta", text: chunk }]);
      }
    } else if (step.kind === "think_tokens") {
      const thinkDelay = () => 200 + Math.floor(rand() * 250); // 200-450ms
      for (const tokens of step.tokens) {
        timeline.push([
          thinkDelay(),
          { type: "thinking_delta", text: "", estimated_tokens: tokens },
        ]);
      }
    } else if (step.kind === "text") {
      for (const chunk of chopWords(step.text, rand)) {
        timeline.push([delay(), { type: "text_delta", text: chunk }]);
      }
    } else if (step.kind === "tool") {
      callCounter += 1;
      const callId = `call-${callCounter}`;
      timeline.push([
        delay(),
        { type: "tool_call_started", call_id: callId, tool: step.tool, summary: step.summary },
      ]);
      timeline.push([
        step.durationMs,
        { type: "tool_call_finished", call_id: callId, ok: step.ok, summary: step.result },
      ]);
    } else if (step.kind === "proposal") {
      devProposals.set(step.proposal.id, step.proposal);
      timeline.push([delay(), { type: "proposal", proposal: step.proposal }]);
    } else if (step.kind === "image") {
      timeline.push([delay(), { type: "inline_image", src: step.src, caption: step.caption }]);
    } else if (step.kind === "widget") {
      timeline.push([delay(), { type: "inline_widget", html: step.html, title: step.title }]);
    } else if (step.kind === "widget_offer") {
      timeline.push([delay(), { type: "widget_offer", title: step.title }]);
    } else if (step.kind === "error") {
      timeline.push([delay(), { type: "error", message: step.message }]);
      endedWithError = true;
    }
  }
  if (!endedWithError) {
    timeline.push([60, { type: "done" }]);
  }
  return timeline;
}

function baseProposal(overrides: Partial<ProposalPayload> & Pick<ProposalPayload, "id" | "kind">): ProposalPayload {
  return {
    revision: 1,
    operation_digest: `dev:${overrides.id}:1`,
    status: "pending",
    note_type: "",
    deck: "",
    tags: [],
    note_id: null,
    fields: [],
    add_tags: [],
    remove_tags: [],
    rationale: "",
    warnings: [],
    previews: null,
    op: "",
    op_args: {},
    title: "",
    count: 0,
    samples: [],
    items: [],
    open: false,
    ...overrides,
  };
}

let proposalCounter = 0;
function nextProposalId(prefix: string): string {
  proposalCounter += 1;
  return `${prefix}${proposalCounter}`;
}

function createProposalScript(): Step[] {
  const id = nextProposalId("p");
  const front = "Analysis: why does the quantifier order in the epsilon-delta definition matter?";
  const back =
    "Because for every epsilon there exists delta lets delta depend on epsilon. Swapping them " +
    "would force f to be locally constant.";
  return [
    { kind: "text", text: "That gap is worth its own card - here is a proposal:\n\n" },
    {
      kind: "proposal",
      proposal: baseProposal({
        id,
        kind: "create",
        note_type: "Basic",
        deck: "Math::Analysis",
        tags: ["analysis"],
        fields: [
          { name: "Front", new: front },
          { name: "Back", new: back },
        ],
        rationale: "You said the quantifier order was the confusing part; this isolates it as one recall step.",
        warnings: ["deck 'Math::Analysis' does not exist yet; it will be created"],
        previews: { before: null, after: { question: front, answer: front + "<hr id=answer>" + back, css: DEMO_CSS } },
      }),
    },
    { kind: "text", text: "Accept it as-is, edit the fields first, or reject it." },
  ];
}

function editProposalScript(): Step[] {
  const id = nextProposalId("e");
  const oldBack = "For every epsilon there is a delta such that |x-a| < delta implies |f(x)-L| < epsilon.";
  const newBack =
    "For every epsilon > 0 there is a delta > 0 such that 0 < |x-a| < delta implies |f(x)-L| < epsilon. " +
    "The puncture (0 <) excludes x = a itself.";
  return [
    { kind: "text", text: "The current back drops the positivity constraints and the puncture - here's a fix:\n\n" },
    {
      kind: "proposal",
      proposal: baseProposal({
        id,
        kind: "edit",
        note_type: "Basic",
        deck: "Math::Analysis",
        tags: ["analysis"],
        note_id: 1234567,
        fields: [{ name: "Back", old: oldBack, new: newBack }],
        add_tags: ["definitions"],
        remove_tags: ["todo"],
        rationale: "The current back drops the positivity constraints and the puncture.",
      }),
    },
  ];
}

const DEFAULT_SCRIPT: Step[] = [
  {
    kind: "text",
    text:
      'This card is about the **epsilon-delta definition of a limit** - the formal way to pin down ' +
      'what "approaches" means.\n\nThe idea in one sentence: you can make `f(x)` land as close to `L` ' +
      "as anyone demands, just by keeping `x` close enough to `a`.\n\nWant me to find related cards in " +
      "this deck?",
  },
];

const TOOL_SCRIPT: Step[] = [
  // Mirrors backends/fixtures.py's TOOL_SCRIPT: a quiet thinking phase
  // (empty text, growing estimated_tokens) before any visible text or tool
  // call, exercising the Reasoning primitive's rotating no-text indicator.
  { kind: "think_tokens", tokens: [40, 95, 160] },
  { kind: "text", text: "Let me look for related cards in your collection first.\n\n" },
  {
    kind: "tool",
    tool: "ToolSearch",
    summary: '{"query":"select:search_notes"}',
    result: "",
    ok: true,
    durationMs: 80,
  },
  {
    kind: "tool",
    tool: "mcp__anki__search_notes",
    summary: '{"query": "deck:current \\"limit\\""}',
    result: '{"total": 12}',
    ok: true,
    durationMs: 900,
  },
  {
    kind: "text",
    text:
      "Found **12 notes** touching on limits. The closest ones:\n\n- *Analysis: define continuity*\n- " +
      "*Analysis: limit laws*\n\nThe continuity card is probably the best follow-up.",
  },
];

const LONG_SCRIPT: Step[] = [
  {
    kind: "text",
    text:
      "Here is the longer story, in parts.\n\n## Where the definition comes from\n\nNineteenth-century " +
      'analysis kept running into paradoxes because "gets closer and closer" was doing unsupervised ' +
      "work.\n\n## Why the order of quantifiers matters\n\n`for every epsilon, there exists delta` is the " +
      "whole content. Swap them and you get a much weaker statement.\n\n## A worked example\n\nTo show " +
      "`lim x->3 of 2x = 6`: given epsilon, pick `delta = epsilon / 2`.",
  },
];

const REASONING_SCRIPT: Step[] = [
  {
    kind: "think",
    text:
      "The user wants to see the reasoning primitive specifically. I should keep the visible answer " +
      "short and let the thinking block carry the detail, so the collapse/expand behavior is obvious.",
  },
  { kind: "text", text: "Here's a short answer - expand \"Thought for a bit\" above to see the reasoning trace." },
];

const ERROR_SCRIPT: Step[] = [
  { kind: "text", text: "Let me check that.\n\n" },
  { kind: "error", message: "the backend process exited unexpectedly (exit code 1)" },
];

// A self-contained SVG data URI so the dev path needs no assets (the real
// show_image tool sends png/jpg/gif/webp; the renderer treats any src alike).
const IMAGE_SCRIPT: Step[] = [
  { kind: "text", text: "Here's that page rendered from the PDF:\n\n" },
  {
    kind: "image",
    src:
      "data:image/svg+xml;utf8," +
      encodeURIComponent(
        "<svg xmlns='http://www.w3.org/2000/svg' width='260' height='150'>" +
          "<rect width='260' height='150' rx='8' fill='#0e7c7b'/>" +
          "<text x='130' y='82' font-size='18' fill='white' text-anchor='middle' " +
          "font-family='sans-serif'>sample image</text></svg>"
      ),
    caption: "The Ultimate Docker Container Book, p. 276",
  },
  { kind: "text", text: "That's the page with the `docker network rm` example." },
];

// The demo widget doubles as a sandbox SELF-TEST: it renders a small
// interactive bar chart (proves inline JS runs), then attempts a network
// fetch and a window.parent DOM read and prints both verdicts inside the
// widget - so a preview screenshot is direct evidence the CSP and the
// opaque-origin sandbox hold. Mirrors the GUI smoke probe's check.
const WIDGET_HTML =
  "<div id='chart' style='display:flex;gap:6px;align-items:flex-end;height:90px'></div>" +
  "<button id='more' style='margin-top:8px'>Add a bar</button>" +
  "<div id='probe' style='margin-top:10px;font-size:11px'></div>" +
  "<script>" +
  "var vals=[42,71,30,88,55];" +
  "function draw(){var c=document.getElementById('chart');c.innerHTML='';" +
  "vals.forEach(function(v){var b=document.createElement('div');" +
  "b.style.cssText='flex:1;background:#0e7c7b;border-radius:3px 3px 0 0;height:'+v+'%';" +
  "b.title=v;c.appendChild(b);});}" +
  "document.getElementById('more').onclick=function(){vals.push(20+Math.floor(Math.random()*70));draw();};" +
  "draw();" +
  "var p=document.getElementById('probe');" +
  "function line(t,ok){var d=document.createElement('div');d.textContent=(ok?'\\u2713 ':'\\u2717 ')+t;" +
  "d.style.color=ok?'#177245':'#b3261e';p.appendChild(d);}" +
  "try{void window.parent.document;line('SANDBOX LEAK: parent DOM reachable',false);}" +
  "catch(e){line('parent DOM blocked (opaque origin)',true);}" +
  "fetch('https://example.com/').then(function(){line('CSP LEAK: network fetch succeeded',false);})" +
  ".catch(function(){line('network fetch blocked (CSP)',true);});" +
  "<\/script>";

function widgetScript(): Step[] {
  if (!devSettings.widget_rendering) {
    return [
      {
        kind: "text",
        text: "I'd like to show this as an interactive chart, but widget rendering is off.\n\n",
      },
      { kind: "widget_offer", title: "Review-load chart" },
      {
        kind: "text",
        text: "Meanwhile in plain text: Mon 42, Tue 71, Wed 30, Thu 88, Fri 55.",
      },
    ];
  }
  return [
    { kind: "text", text: "Here's your review load this week:\n\n" },
    { kind: "widget", html: WIDGET_HTML, title: "Review-load chart (sandbox self-test)" },
    { kind: "text", text: "Bars are hoverable; the button adds data. The two lines below the chart are the sandbox self-test." },
  ];
}

// Exercises the trusted-renderer pipeline: KaTeX inline/display (plus the
// currency string that must NOT become math), a mermaid fence, a GFM table.
const MATH_SCRIPT: Step[] = [
  {
    kind: "text",
    text:
      "The quadratic formula is $x = \\frac{-b \\pm \\sqrt{b^2-4ac}}{2a}$, and " +
      "the Gaussian integral:\n\n$$\\int_{-\\infty}^{\\infty} e^{-x^2}\\,dx = \\sqrt{\\pi}$$\n\n" +
      "Currency stays text: R$ 50 and $5 vs $10 are not math.\n\n" +
      "A quick diagram:\n\n```mermaid\nflowchart LR\n  A[Card shown] --> B{Recalled?}\n  B -- yes --> C[Good]\n  B -- no --> D[Again]\n```\n\n" +
      "| Deck | Due | Ease |\n|---|---|---|\n| Kanji | 42 | 2.4 |\n| Geo | 17 | 2.6 |\n| Quant | 8 | 2.1 |\n",
  },
];

function selectScript(userText: string): Step[] {
  const text = userText.toLowerCase();
  if (text.includes("error")) return ERROR_SCRIPT;
  if (text.includes("math") || text.includes("mermaid")) return MATH_SCRIPT;
  if (text.includes("widget") || text.includes("chart")) return widgetScript();
  if (text.includes("image") || text.includes("picture")) return IMAGE_SCRIPT;
  if (text.includes("think") || text.includes("reason")) return REASONING_SCRIPT;
  if (text.includes("edit")) return editProposalScript();
  if (text.includes("propose") || text.includes("note") || text.includes("card")) return createProposalScript();
  if (text.includes("tool")) return TOOL_SCRIPT;
  if (text.includes("long")) return LONG_SCRIPT;
  return DEFAULT_SCRIPT;
}

const WELCOME_SCRIPT: Step[] = [
  // Fires automatically on load (no typing needed), so `npm run dev` shows
  // both thinking-indicator states in one turn: first the empty-text
  // rotating "Thinking…N tokens" phase (today's real-CLI shape), then real
  // thinking text streaming into the same reasoning part (future-proofing -
  // see ReasoningBlock.tsx), before the visible answer.
  { kind: "think_tokens", tokens: [35, 80] },
  {
    kind: "think",
    text: "No message yet - I'll greet them and mention what the scripted replayer can demo on request.",
  },
  {
    kind: "text",
    text:
      "Hi! This is the assistant-ui scaffold running against the scripted dev replayer.\n\nTry typing " +
      "**tool**, **propose**, **edit**, **think**, **long**, **widget**, **image**, or **error** to see each " +
      "event path, or just send anything else for the default reply.",
  },
];

// Module scope (not inside installDevReplayer): widgetScript() branches on it
// at send time, mirroring how the real tool reads live config per call.
const devSettings: Record<string, unknown> = { restore_last_chat: false, dock_side: "right" };

export function installDevReplayer(): void {
  let generation = 0;
  let pendingTimers: number[] = [];

  function scheduleAll(timeline: Array<[number, ChatEvent]>): void {
    const gen = generation;
    let elapsed = 0;
    for (const [delay, event] of timeline) {
      elapsed += delay;
      const timer = window.setTimeout(() => {
        if (gen !== generation) return;
        window.chatUI?.dispatch(event);
      }, elapsed);
      pendingTimers.push(timer);
    }
  }

  function cancelPending(): void {
    generation += 1;
    pendingTimers.forEach((t) => window.clearTimeout(t));
    pendingTimers = [];
  }

  window.pycmd = (raw: string): void => {
    if (raw.indexOf("cwyc:") !== 0) return;
    let msg: Record<string, unknown>;
    try {
      msg = JSON.parse(raw.slice(5));
    } catch {
      return;
    }
    // eslint-disable-next-line no-console
    console.log("[dev replayer] received command:", msg);

    switch (msg.type) {
      case "ready":
        window.chatUI?.ackReady();
        // Mirror _mark_web_ready (__init__.py): the initial control-state
        // pushes so the header + composer controls are exercisable in dev.
        window.chatUI?.dispatch({
          type: "agent",
          backend: "claude",
          model: "opus",
          effort: "high",
          mode: "default",
          fast: false,
          tools: "sandbox",
        });
        window.chatUI?.dispatch({
          type: "ui_config",
          suggested_questions: true,
          open_in_claude_target: "terminal",
        });
        window.chatUI?.dispatch({
          type: "collection_meta",
          decks: ["Default", "Math::Analysis", "Data Systems", "Chinese::Hanzi"],
          note_types: [
            { name: "Basic", fields: ["Front", "Back"] },
            { name: "Cloze", fields: ["Text", "Extra"] },
            { name: "Basic (source)", fields: ["Front", "Back", "Source"] },
          ],
          tags: ["ai-created", "analysis", "kimball", "probe"],
        });
        window.chatUI?.dispatch({ type: "pins", pins: { deck: "", note_type: "", tags: [], fields: {} } });
        window.chatUI?.dispatch({
          type: "dock_state",
          expanded: true,
          animating: false,
          width: 420,
          side: "right",
        });
        window.chatUI?.dispatch({
          type: "settings",
          restore_last_chat: false,
          dock_side: "right",
          toggle_shortcut: "Ctrl+J",
          new_chat_shortcut: "Ctrl+Shift+J",
          vim_mode: Boolean(devSettings.vim_mode),
          vim_mappings: [
            ["fd", "<Esc>", "insert"],
            ["j", "gj", "normal"],
            ["k", "gk", "normal"],
            ["j", "gj", "visual"],
            ["k", "gk", "visual"],
            ["0", "g0", "normal"],
            ["0", "g0", "visual"],
            ["$", "g$", "normal"],
            ["$", "g$", "visual"],
            ["Y", "y$", "normal"],
          ],
          theme: String(devSettings.theme ?? "teal"),
          widget_rendering: Boolean(devSettings.widget_rendering),
        });
        break;
      case "list_history":
        window.chatUI?.dispatch({
          type: "history",
          sessions: [
            { id: "h1", title: "Conformed dimensions recap", updated_at: Date.now() / 1000 - 3600, events: 24 },
            { id: "h2", title: "Epsilon-delta cards", updated_at: Date.now() / 1000 - 90000, events: 51 },
          ],
        });
        break;
      case "load_history":
        cancelPending();
        // Mirror Python: reset, then a history_load carrying recorded events
        // (the transcripts.py RECORDED_TYPES shapes) for the store to replay.
        window.chatUI?.dispatch({ type: "reset" });
        window.chatUI?.dispatch({
          type: "history_load",
          events: [
            { type: "user_message", text: "Remind me what a conformed dimension is." },
            {
              type: "assistant_text",
              text: "A **conformed dimension** is a dimension table shared across multiple fact tables with identical keys and meaning.",
            },
            { type: "tool_call_started", call_id: "h-1", tool: "search_notes", summary: "conformed dimension" },
            { type: "tool_call_finished", call_id: "h-1", ok: true, summary: "3 notes" },
            { type: "user_message", text: "Add a card for it." },
            {
              type: "proposal",
              proposal: {
                id: "hp-1",
                kind: "create",
                note_type: "Basic",
                deck: "Data Modeling",
                status: "accepted",
                rationale: "Captures the definition as one recall step.",
                warnings: [],
                fields: [
                  { name: "Front", old: "", new: "What is a conformed dimension?" },
                  { name: "Back", old: "", new: "A dimension shared across fact tables with identical keys and meaning." },
                ],
                previews: {
                  after: {
                    question: "What is a conformed dimension?",
                    answer: "A dimension shared across fact tables with identical keys and meaning.",
                    css: ".card{font-family:sans-serif;}",
                  },
                },
              },
            },
            { type: "proposal_resolved", id: "hp-1", status: "accepted" },
            {
              type: "usage",
              cost_usd: null,
              input_tokens: 1840,
              output_tokens: 260,
              cache_read_tokens: 612_000,
              cache_creation_tokens: 15_000,
              // Real per-turn window straight from the CLI (dogfood 2026-07-13):
              // ~629k used / 1M window ~= 63%, not the 100% the 200k default
              // table would falsely show for the unpinned model.
              context_window: 1_000_000,
              fast_mode_state: "on",
            },
          ],
        });
        break;
      case "run_doctor":
        window.setTimeout(() => {
          window.chatUI?.dispatch({
            type: "doctor",
            results: [
              { label: "Claude Code", status: "ok", detail: "2.1.207 at ~/.local/bin/claude" },
              // Long values on purpose: the panel layout must survive real
              // paths/URLs (dogfood 2026-07-12 found side-by-side wrapping).
              {
                label: "Collection tool server (MCP)",
                status: "ok",
                detail: "http://127.0.0.1:61674/mcp",
              },
              {
                label: "Built-in Anki skills",
                status: "ok",
                detail: "4/4 materialized under user_files/agent-home/.claude/skills",
              },
              {
                label: "Edit-pattern learning",
                status: "ok",
                detail:
                  "3 AI-written note(s) watched, 1 pending observation(s), 4 KB on disk (uncapped by design - grows only with AI-written notes)",
              },
              { label: "Anthropic billing", status: "ok", detail: "harness login (no API key configured)" },
              { label: "Codex", status: "missing", detail: "not found on PATH" },
            ],
          });
        }, 350);
        break;
      case "set_agent": {
        const tiers = ["sandbox", "acceptEdits", "auto", "full"];
        const tools = tiers.includes(String(msg.tools)) ? String(msg.tools) : "sandbox";
        window.chatUI?.dispatch({
          type: "agent",
          backend: "claude",
          model: msg.model,
          effort: msg.effort,
          mode: "default",
          fast: Boolean(msg.fast),
          tools,
        });
        const toolLabel: Record<string, string> = {
          acceptEdits: "accept-edits tools",
          auto: "auto tools",
          full: "full tools",
        };
        const bits = [
          String(msg.model || "the default model"),
          msg.fast ? "fast" : "",
          toolLabel[tools] ?? "",
        ].filter(Boolean);
        window.chatUI?.dispatch({
          type: "notice",
          text: `Switched to ${bits[0]}${bits.length > 1 ? ` (${bits.slice(1).join(", ")})` : ""}.`,
        });
        break;
      }
      case "set_permission_mode":
        window.chatUI?.dispatch({
          type: "agent",
          backend: "claude",
          model: "opus",
          effort: "high",
          mode: msg.mode,
          fast: false,
          tools: "sandbox",
        });
        break;
      case "set_pins":
        window.chatUI?.dispatch({ type: "pins", pins: msg.pins });
        break;
      case "set_open_in_claude_target":
        window.chatUI?.dispatch({
          type: "ui_config",
          suggested_questions: true,
          open_in_claude_target: msg.target,
        });
        break;
      case "open_in_claude":
        window.chatUI?.dispatch({ type: "notice", text: `Would open in Claude Code (${msg.target}).` });
        break;
      case "set_dock_expanded": {
        // Mirror dock.py's transition contract: dock_state {animating:true}
        // at the start, one-step frame width change (expand: immediately so
        // the space exists; collapse: at the end, after the page's CSS slide
        // has played), {animating:false} at the end.
        const expanded = Boolean(msg.expanded);
        const frame = document.getElementById("dev-frame");
        const state = { type: "dock_state", expanded, width: 420, side: "right" };
        window.chatUI?.dispatch({ ...state, animating: true });
        if (expanded && frame) frame.style.width = "420px";
        window.setTimeout(() => {
          if (!expanded && frame) frame.style.width = "44px";
          window.chatUI?.dispatch({ ...state, animating: false });
        }, 280);
        break;
      }
      case "set_setting": {
        // Persist-and-echo, like __init__.py's _set_setting.
        devSettings[String(msg.key)] = msg.value;
        window.chatUI?.dispatch({
          type: "settings",
          restore_last_chat: Boolean(devSettings.restore_last_chat),
          dock_side: devSettings.dock_side === "left" ? "left" : "right",
          toggle_shortcut: "Ctrl+J",
          new_chat_shortcut: "Ctrl+Shift+J",
          vim_mode: Boolean(devSettings.vim_mode),
          vim_mappings: [
            ["fd", "<Esc>", "insert"],
            ["j", "gj", "normal"],
            ["k", "gk", "normal"],
            ["j", "gj", "visual"],
            ["k", "gk", "visual"],
            ["0", "g0", "normal"],
            ["0", "g0", "visual"],
            ["$", "g$", "normal"],
            ["$", "g$", "visual"],
            ["Y", "y$", "normal"],
          ],
          theme: String(devSettings.theme ?? "teal"),
          widget_rendering: Boolean(devSettings.widget_rendering),
        });
        break;
      }
      case "send":
        scheduleAll(compile(selectScript(String(msg.text ?? ""))));
        break;
      case "cancel":
        cancelPending();
        window.chatUI?.dispatch({ type: "cancelled" });
        break;
      case "proposal_accept":
        window.chatUI?.dispatch({
          type: "proposal_resolved",
          id: msg.id,
          status: "accepted",
          note_id: 424242,
          warnings: [],
        });
        break;
      case "proposal_revise": {
        const current = devProposals.get(String(msg.id));
        if (!current) break;
        const revision = Number(msg.expected_revision ?? current.revision ?? 1) + 1;
        const fields = Object.entries((msg.fields ?? {}) as Record<string, string>).map(([name, value]) => ({ name, new: value }));
        const revised = { ...current, revision, operation_digest: `dev:${current.id}:${revision}`, fields };
        devProposals.set(current.id, revised);
        window.chatUI?.dispatch({ type: "proposal", proposal: revised });
        break;
      }
      case "proposal_reject":
        window.chatUI?.dispatch({ type: "proposal_resolved", id: msg.id, status: "rejected" });
        break;
      case "new_chat":
        cancelPending();
        window.chatUI?.dispatch({ type: "reset" });
        break;
      default:
        break;
    }
  };

  // Fire once on load so the full mapping (reasoning -> text -> done) is
  // visible without requiring the user to type anything first.
  window.setTimeout(() => scheduleAll(compile(WELCOME_SCRIPT)), 300);
}
