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
      timeline.push([delay(), { type: "proposal", proposal: step.proposal }]);
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

function selectScript(userText: string): Step[] {
  const text = userText.toLowerCase();
  if (text.includes("error")) return ERROR_SCRIPT;
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
      "**tool**, **propose**, **edit**, **think**, **long**, or **error** to see each event path, or just " +
      "send anything else for the default reply.",
  },
];

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
        break;
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
