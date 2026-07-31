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
import type { ChatEvent, GradingPayload, ProposalPayload } from "../events";

type Step =
  | { kind: "think" | "text"; text: string }
  | { kind: "think_tokens"; tokens: readonly number[] }
  | { kind: "tool"; tool: string; summary: string; result: string; ok: boolean; durationMs: number }
  | { kind: "proposal"; proposal: ProposalPayload }
  | { kind: "grading"; grading: GradingPayload }
  | { kind: "image"; src: string; caption: string }
  | { kind: "widget"; html: string; title: string }
  | { kind: "widget_offer"; title: string }
  | {
      kind: "tool_approval";
      approvalId: string;
      tool: string;
      summary: string;
      /** Seconds until the prompt stops being answerable; omit for "never". */
      expiresInS?: number;
    }
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
let devReviewCard = 424242;

// The set-aside tray (task #33): a persistent dev list, pre-seeded with the
// shapes that broke pickers before (deep deck paths, long fronts, media
// markers), so the tray is visually testable the moment the page loads.
interface DevAsideEntry {
  card_id: number;
  deck: string;
  front: string;
  back: string;
}
const devAside: DevAsideEntry[] = [
  {
    card_id: 424001,
    deck: "Geography::World::Regions::South America::Brazil::GeoTrainer",
    front: "Locate on the blank map:\nRio Grande do Norte",
    back: "[image]\nNortheastern tip of Brazil — capital Natal.",
  },
  {
    card_id: 424002,
    deck: "Math::Analysis",
    front:
      "Why does the order of quantifiers matter in the epsilon-delta definition of a limit, and what would swapping them assert instead?",
    back: "Delta may depend on epsilon. Swapped, one delta would have to work for every epsilon — forcing f to be locally constant.",
  },
  {
    card_id: 424003,
    deck: "Chinese::Hanzi",
    front: "[audio] 学习",
    back: "xuéxí — to study, to learn",
  },
];
function pushDeferredList(): void {
  window.chatUI?.dispatch({ type: "deferred_list", entries: [...devAside] });
}
function pushReviewState(): void {
  window.chatUI?.dispatch({
    type: "review_state",
    reviewing: true,
    card_id: devReviewCard,
    set_aside_count: devAside.length,
  });
}
// The dev preview always "has a review card on screen" so the Set aside chip
// and the undo flow are reachable by hand.
window.setTimeout(() => {
  pushReviewState();
  pushDeferredList();
}, 400);
const devGradings = new Map<string, GradingPayload>();

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
    } else if (step.kind === "grading") {
      devGradings.set(step.grading.id, step.grading);
      timeline.push([delay(), { type: "grading", grading: step.grading }]);
    } else if (step.kind === "image") {
      timeline.push([delay(), { type: "inline_image", src: step.src, caption: step.caption }]);
    } else if (step.kind === "widget") {
      timeline.push([delay(), { type: "inline_widget", html: step.html, title: step.title }]);
    } else if (step.kind === "widget_offer") {
      timeline.push([delay(), { type: "widget_offer", title: step.title }]);
    } else if (step.kind === "tool_approval") {
      // Mirrors approvals.py: push the request, then STOP. Nothing further is
      // scheduled - the real MCP thread is blocked here, and the rest of the
      // turn only resumes once `tool_approval_response` comes back (handled in
      // the command switch below), so the dev harness reproduces the stall
      // that made this bug invisible.
      timeline.push([
        delay(),
        {
          type: "tool_approval",
          id: step.approvalId,
          tool: step.tool,
          summary: step.summary,
          // approvals.py sends a wall-clock deadline (approval_timeout_minutes);
          // the dev harness uses a short one so the countdown and the expired
          // state are both reachable by hand.
          ...(step.expiresInS === undefined
            ? {}
            : { expires_at_ms: Date.now() + step.expiresInS * 1000 }),
        },
      ]);
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

/** One side out of a proposal's `previews: unknown`, without pretending to
 *  know more about the shape than the dev harness needs. */
function asSide(
  previews: unknown,
  side: "before" | "after"
): { question?: string; answer?: string; css?: string } | null {
  if (!previews || typeof previews !== "object") return null;
  const value = (previews as Record<string, unknown>)[side];
  return value && typeof value === "object" ? (value as { question?: string }) : null;
}

let proposalCounter = 0;
function nextProposalId(prefix: string): string {
  proposalCounter += 1;
  return `${prefix}${proposalCounter}`;
}

/** A genuinely playable 0.3s 440Hz beep (8-bit PCM WAV) built at runtime, so
 *  the proposal demo's schema-1.1 media strip plays real audio with no asset
 *  files. Deterministic - same bytes every call. */
function demoWavDataUri(): string {
  const rate = 8000;
  const n = Math.floor(rate * 0.3);
  const bytes = new Uint8Array(44 + n);
  const view = new DataView(bytes.buffer);
  const ascii = (offset: number, text: string) => {
    for (let i = 0; i < text.length; i++) bytes[offset + i] = text.charCodeAt(i);
  };
  ascii(0, "RIFF");
  view.setUint32(4, 36 + n, true);
  ascii(8, "WAVE");
  ascii(12, "fmt ");
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true); // PCM
  view.setUint16(22, 1, true); // mono
  view.setUint32(24, rate, true);
  view.setUint32(28, rate, true); // byte rate (8-bit mono)
  view.setUint16(32, 1, true);
  view.setUint16(34, 8, true);
  ascii(36, "data");
  view.setUint32(40, n, true);
  for (let i = 0; i < n; i++) {
    bytes[44 + i] = 128 + Math.round(100 * Math.sin((2 * Math.PI * 440 * i) / rate));
  }
  let bin = "";
  bytes.forEach((b) => {
    bin += String.fromCharCode(b);
  });
  return "data:audio/wav;base64," + btoa(bin);
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
        media: [
          {
            id: "m-demo-1",
            kind: "audio",
            name: "epsilon-delta.wav",
            mime: "audio/wav",
            bytes: 2444,
            src: demoWavDataUri(),
          },
          {
            // #10: a staged image draws in the CWYC visual strip.
            id: "m-demo-2",
            kind: "image",
            name: "epsilon-band.png",
            mime: "image/png",
            bytes: 68,
            src:
              "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAgAAAAICAYAAADED76LAAAAG0lEQVR4nGNgYGD4z8DAwMDEgAaGgADjfwYGBgB50QIDU9zC1gAAAABJRU5ErkJggg==",
          },
        ],
        // task #25: a [sound:...] ref to media already in the collection,
        // resolved server-side to a data: URI - draws on the same strip.
        preview_media: [
          {
            id: "pv-demo-1",
            kind: "audio",
            name: "existing-tone.wav",
            mime: "audio/wav",
            bytes: 2444,
            src: demoWavDataUri(),
          },
        ],
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
        previews: {
          before: { question: "Define the limit of f at a.", answer: oldBack, css: DEMO_CSS },
          after: { question: "Define the limit of f at a.", answer: newBack, css: DEMO_CSS },
        },
      }),
    },
  ];
}

/** A TAG-ONLY edit: no field changes, no preview change. Before task #20 this
 *  rendered as a card with literally nothing on it to review. */
function tagEditProposalScript(): Step[] {
  const id = nextProposalId("t");
  return [
    { kind: "text", text: "This one is mis-tagged - the content is fine:\n\n" },
    {
      kind: "proposal",
      proposal: baseProposal({
        id,
        kind: "edit",
        note_type: "Basic",
        deck: "Math::Analysis",
        tags: ["analysis", "todo", "imported"],
        note_id: 1234568,
        fields: [],
        add_tags: ["definitions", "analysis"],
        remove_tags: ["todo"],
        rationale: "It is a definition, and the todo tag is left over from the import.",
      }),
    },
  ];
}

/** bulk find_replace: op + per-note old/new samples + the affected notes. */
function bulkProposalScript(): Step[] {
  const id = nextProposalId("b");
  const labels = [
    "Cauchy sequence", "Uniform continuity", "Compactness", "Heine-Borel",
    "Bolzano-Weierstrass", "Monotone convergence", "Squeeze theorem",
    "Nested intervals", "Cantor set", "Lebesgue measure",
  ];
  return [
    { kind: "text", text: "Ten notes still spell it the old way:\n\n" },
    {
      kind: "proposal",
      proposal: baseProposal({
        id,
        kind: "bulk",
        op: "find_replace",
        op_args: { search: "epsilon", replacement: "\u03b5", field: "Back", query: "deck:Math::Analysis" },
        rationale: "Use the symbol rather than the word, matching the rest of the deck.",
        count: labels.length,
        samples: labels.slice(0, 4).map((label) => ({
          label,
          old: `For every epsilon > 0 there is a delta > 0 (${label}).`,
          new: `For every \u03b5 > 0 there is a delta > 0 (${label}).`,
        })),
        items: labels.map((label, i) => ({ note_id: 1500000 + i, label, fields: ["Back"] })),
      }),
    },
  ];
}

/** scheduling write (#6): before/after diff samples, submit_scheduling's shape. */
function schedulingProposalScript(): Step[] {
  const id = nextProposalId("sd");
  return [
    { kind: "text", text: "Your exam is on the 14th - pushing these past it:\n\n" },
    {
      kind: "proposal",
      proposal: baseProposal({
        id,
        kind: "bulk",
        op: "set_due_date",
        op_args: { days: "16-20", query: 'deck:"Math::Analysis" is:due' },
        rationale: "Spreads the backlog across the four days after the exam.",
        count: 41,
        samples: [
          { text: "Set due date to 16-20 — 41 card(s) matching 'deck:\"Math::Analysis\" is:due'" },
          { label: "Cauchy sequence", old: "review · overdue 3d · ivl 21d", new: "review · due in 16-20d" },
          { label: "Uniform continuity", old: "review · due in 0d · ivl 4d", new: "review · due in 16-20d" },
          { label: "Heine-Borel", old: "new · position 12", new: "review · due in 16-20d" },
        ],
        warnings: ["7 new card(s) become review cards (revert restores their exact new-card state)"],
      }),
    },
  ];
}

/** bulk tags (#4): headline sample + note labels, submit_bulk_tags's shape. */
function tagsProposalScript(): Step[] {
  const id = nextProposalId("t");
  return [
    { kind: "text", text: "All ten mention the epsilon-delta pattern - tagging them:\n\n" },
    {
      kind: "proposal",
      proposal: baseProposal({
        id,
        kind: "bulk",
        op: "add_tags",
        op_args: { tags: ["analysis::epsilon-delta"], query: '"epsilon" deck:"Math::Analysis"' },
        tags: ["analysis::epsilon-delta"],
        rationale: "One tag makes the whole pattern retrievable as a cram deck later.",
        count: 10,
        samples: [
          { text: "Add analysis::epsilon-delta — 10 note(s) matching '\"epsilon\" deck:\"Math::Analysis\"'" },
          { text: "Cauchy sequence" },
          { text: "Uniform continuity" },
          { text: "Squeeze theorem" },
        ],
        warnings: ["3 of these note(s) already carry all of those tags (no change)"],
      }),
    },
  ];
}

/** card-state bulk op (#3): headline sample + card labels + honest warnings,
 *  exactly the payload shape proposals.py's submit_card_state pushes. */
function suspendProposalScript(): Step[] {
  const id = nextProposalId("q");
  return [
    { kind: "text", text: "These leeches are eating your reviews - park them:\n\n" },
    {
      kind: "proposal",
      proposal: baseProposal({
        id,
        kind: "bulk",
        op: "suspend_cards",
        op_args: { query: 'deck:"Math::Analysis" tag:leech' },
        rationale: "These leeches have 8+ lapses each; suspending stops the churn while we rework them.",
        count: 12,
        samples: [
          { text: "Suspend 12 card(s) matching 'deck:\"Math::Analysis\" tag:leech'" },
          { text: "Cauchy sequence" },
          { text: "Uniform continuity" },
          { text: "Heine-Borel" },
        ],
        warnings: [
          "2 of these card(s) are already suspended (no change)",
          "1 card(s) sit in a filtered deck; this returns them to their home deck, and undo will not re-add them to the filtered deck",
        ],
      }),
    },
  ];
}

/** delete: the blast radius, by name. */
function deleteProposalScript(): Step[] {
  const id = nextProposalId("d");
  const labels = ["Duplicate: Cauchy sequence", "Duplicate: Compactness", "Empty note"];
  return [
    { kind: "text", text: "These three are duplicates or empty:\n\n" },
    {
      kind: "proposal",
      proposal: baseProposal({
        id,
        kind: "delete",
        rationale: "Duplicates of notes you already have, plus one with no content.",
        count: labels.length,
        samples: labels.map((text) => ({ text })),
        op_args: { note_ids: [1500001, 1500002, 1500003] },
        warnings: [
          "Deleting notes cannot be undone from the chat ledger. A backup checkpoint is created first (File > Switch Profile restores it).",
        ],
      }),
    },
  ];
}

/** skill_update: patterns plus the unified diff proposals.py already builds. */
/** A change set the assistant is STILL adding to: `open` is true, so the card
 *  must say so and must not offer Accept (task #19). */
function openChangeSetScript(): Step[] {
  const id = nextProposalId("c");
  return [
    { kind: "text", text: "Working through the deck - I will keep adding to this set:\n\n" },
    {
      kind: "proposal",
      proposal: baseProposal({
        id,
        kind: "change_set",
        title: "Tidy analysis answers",
        rationale: "Shorten answers to a single recallable fact.",
        count: 3,
        open: true,
        items: [
          { note_id: 1500001, label: "Cauchy sequence", fields: ["Back"] },
          { note_id: 1500002, label: "Compactness", fields: ["Back"] },
          { note_id: 1500003, label: "Heine-Borel", fields: ["Back"] },
        ],
      }),
    },
  ];
}

function skillProposalScript(): Step[] {
  const id = nextProposalId("s");
  const diff = [
    "--- skill (current)",
    "+++ skill (proposed)",
    "@@ -4,7 +4,9 @@",
    " ## Card style",
    " ",
    "-Keep answers short.",
    "+Keep answers to one recallable fact.",
    "+Put the shortest useful label in the answer zone.",
    " ",
    " ## Tagging",
  ].join("\n");
  return [
    { kind: "text", text: "Your last ten edits share a pattern worth writing down:\n\n" },
    {
      kind: "proposal",
      proposal: baseProposal({
        id,
        kind: "skill_update",
        title: "Update the card-authoring skill",
        rationale: "You consistently shorten answer text and add a label to the answer zone.",
        count: 10,
        samples: [
          { text: "Answers trimmed to a single recallable fact (7 of 10 edits)" },
          { text: "A short label added above the answer (5 of 10 edits)" },
        ],
        op_args: { diff, new_content: "", observation_ids: [] },
      }),
    },
  ];
}

function gradingScript(): Step[] {
  const grading: GradingPayload = {
    id: "g-dev-1",
    action: "fail",
    status: "pending",
    card_ids: [1700000000001, 1700000000002],
    cards: [
      {
        card_id: 1700000000001,
        note_id: 1600000000001,
        deck: "Math::Analysis",
        current_deck: "Math::Analysis",
        template: "Card 1",
        prompt_field: "Front",
        prompt: "Why does the order of quantifiers matter in the epsilon–delta definition?",
        queue: -3,
        hidden_state: "manually buried",
        preview_filtered: false,
        rescheduling_filtered: false,
      },
      {
        card_id: 1700000000002,
        note_id: 1600000000002,
        deck: "Math::Analysis",
        current_deck: "Preview missed concepts",
        template: "Card 1",
        prompt_field: "Front",
        prompt: "State the punctured-neighborhood clause in the definition of a limit.",
        queue: 2,
        hidden_state: null,
        preview_filtered: true,
        rescheduling_filtered: false,
      },
    ],
    rationale: "Both atomics were explicitly missed in the learner’s explanation.",
    warnings: [
      "The failure will be recorded, but existing manually buried state will remain. You can make the cards available afterward.",
      "Preview-filtered targets will leave preview individually before Anki records Again in their home deck.",
    ],
    result: null,
    availability: null,
    available_card_ids: [],
    automatic_mode: null,
  };
  return [
    { kind: "text", text: "I resolved the two missed atomics to these exact cards:\n\n" },
    { kind: "grading", grading },
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

/** Ask-each-read (task #17): the turn STALLS on the approval, exactly as the
 *  real MCP thread does, and only continues when the chip answers. */
let approvalCounter = 0;
const pendingApproval: { id: string | null; late: boolean } = { id: null, late: false };

/** `approve slow` models the grace expiring before the user answers: the turn
 *  ends with the "prompt is waiting" message the tool now returns, and the
 *  chip stays answerable. Answering it late marks the chip accordingly. */
function slowApprovalScript(): Step[] {
  approvalCounter += 1;
  const id = `a${approvalCounter}`;
  pendingApproval.id = id;
  pendingApproval.late = true;
  return [
    { kind: "text", text: "Let me look at the cards in that deck.\n\n" },
    {
      kind: "tool_approval",
      approvalId: id,
      tool: "search_notes",
      summary: '{"query": "deck:\\"Math::Analysis\\"", "limit": 20}',
      expiresInS: 15,
    },
    {
      kind: "text",
      text:
        "You have an approval prompt waiting above — answer it and ask me again " +
        "and I'll pick this up.",
    },
  ];
}

function approvalScript(): Step[] {
  approvalCounter += 1;
  const id = `a${approvalCounter}`;
  pendingApproval.id = id;
  pendingApproval.late = false;
  return [
    { kind: "text", text: "Let me look at the cards in that deck.\n\n" },
    {
      kind: "tool_approval",
      approvalId: id,
      // Faithful to __init__.py: the summary is json.dumps(args)[:120].
      tool: "search_notes",
      summary: '{"query": "deck:\\"Math::Analysis\\"", "limit": 20}',
      expiresInS: 300,
    },
  ];
}

/** What the agent does once the user answers - the half a real backend would
 *  run after `request()` returns. */
function afterApproval(allow: boolean): Step[] {
  if (!allow) {
    return [{ kind: "text", text: "Understood — I'll skip that search. Anything else?" }];
  }
  return [
    {
      kind: "tool",
      tool: "search_notes",
      summary: 'deck:"Math::Analysis"',
      result: "12 notes",
      ok: true,
      durationMs: 240,
    },
    { kind: "text", text: "Found 12 notes in that deck." },
  ];
}

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
  if (text.includes("approve") || text.includes("approval"))
    return text.includes("slow") ? slowApprovalScript() : approvalScript();
  if (text.includes("math") || text.includes("mermaid")) return MATH_SCRIPT;
  if (text.includes("widget") || text.includes("chart")) return widgetScript();
  if (text.includes("image") || text.includes("picture")) return IMAGE_SCRIPT;
  if (text.includes("think") || text.includes("reason")) return REASONING_SCRIPT;
  if (text.includes("grade") || text.includes("fail") || text.includes("again")) return gradingScript();
  if (text.includes("due") || text.includes("sched")) return schedulingProposalScript();
  if (text.includes("tags")) return tagsProposalScript();
  if (text.includes("tag")) return tagEditProposalScript();
  if (text.includes("bulk") || text.includes("replace")) return bulkProposalScript();
  if (text.includes("suspend") || text.includes("flag")) return suspendProposalScript();
  if (text.includes("delete")) return deleteProposalScript();
  if (text.includes("collect") || text.includes("change set")) return openChangeSetScript();
  if (text.includes("skill")) return skillProposalScript();
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
      "**tool**, **propose**, **edit**, **grade**, **think**, **long**, **widget**, **image**, **setup**, or **error** to see each " +
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
      case "send": {
        const text = String(msg.text ?? "");
        // "setup" previews the first-run onboarding card (task #19) instead
        // of running a normal scripted turn: the real card only ever shows
        // on an EMPTY thread (Thread.tsx gates it inside ThreadPrimitive.Empty),
        // so clear the optimistic user/assistant messages sendUserMessage()
        // already added before pushing setup_needed onto the now-empty thread.
        if (text.trim().toLowerCase() === "setup") {
          cancelPending();
          window.chatUI?.dispatch({ type: "reset" });
          window.chatUI?.dispatch({ type: "setup_needed", platform: "darwin" });
          break;
        }
        scheduleAll(compile(selectScript(text)));
        break;
      }
      case "recheck_backend":
        // Dev always "finds" the CLI on the first Re-check click, mirroring
        // the real success path: notice + setup_resolved (dismisses the
        // card). Fire on a short delay so the button's "Checking…" state is
        // visible for a beat, same as a real filesystem check would feel.
        window.setTimeout(() => {
          window.chatUI?.dispatch({ type: "notice", text: "Claude Code found — you're all set." });
          window.chatUI?.dispatch({ type: "setup_resolved" });
        }, 250);
        break;
      // approvals.py's respond(): echo the resolution (the chip already marked
      // itself, so this proves idempotency), then let the blocked turn finish.
      case "tool_approval_response": {
        const id = String(msg.id ?? "");
        if (!id || id !== pendingApproval.id) break; // unknown/stale handle
        pendingApproval.id = null;
        const allow = Boolean(msg.allow);
        const late = pendingApproval.late;
        window.chatUI?.dispatch({ type: "tool_approval_resolved", id, allow, late });
        // A late answer cannot resume the call that gave up - the chip says
        // "ask again to continue" and nothing streams until the user does.
        if (!late) scheduleAll(compile(afterApproval(allow)));
        break;
      }
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
      // proposals.py's preview_request: re-render the card from the draft the
      // user is typing and push it back as `preview_update`. Mirrors the real
      // contract exactly - previews only, never status/fields/revision.
      case "proposal_preview": {
        const current = devProposals.get(String(msg.id));
        if (!current) break;
        const fields = (msg.fields ?? {}) as Record<string, string>;
        const question = fields.Front ?? String(asSide(current.previews, "after")?.question ?? "");
        const answer = Object.entries(fields)
          .filter(([name]) => name !== "Front")
          .map(([, value]) => value)
          .join("<hr id=answer>");
        window.chatUI?.dispatch({
          type: "preview_update",
          id: current.id,
          previews: {
            before: asSide(current.previews, "before"),
            after: { question, answer, css: DEMO_CSS },
          },
        });
        break;
      }
      case "proposal_preview_window":
        // Desktop Anki opens a resizable QDialog here (#2); the browser
        // harness can only prove the command fired.
        window.chatUI?.dispatch({
          type: "notice",
          text: "Desktop Anki opens the large preview window here.",
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
      // proposals.py's supersede(): a pending proposal set aside because the
      // user asked for a different one.
      // task #32: the dev harness plays the reviewer. A fake card is "on
      // screen" from load (review_state below); defer_current serves the next
      // fake card and raises the undo chip, exactly like Python.
      case "defer_current": {
        // Mirror Python's _notify_deferred: the undo chip AND the tray list.
        devAside.unshift({
          card_id: devReviewCard,
          deck: "Math::Analysis",
          front: `Deferred from the dev reviewer (card ${devReviewCard}).`,
          back: "It would come back later in this session.",
        });
        window.chatUI?.dispatch({ type: "card_deferred", card_id: devReviewCard });
        devReviewCard += 1;
        pushReviewState();
        pushDeferredList();
        break;
      }
      case "undo_defer": {
        const backId = Number(msg.card_id) || devReviewCard;
        const at = devAside.findIndex((e) => e.card_id === backId);
        if (at >= 0) devAside.splice(at, 1);
        devReviewCard = backId;
        pushReviewState();
        pushDeferredList();
        break;
      }
      case "get_deferred":
        pushDeferredList();
        break;
      case "unbury_all_deferred":
        devAside.length = 0;
        window.chatUI?.dispatch({ type: "notice", text: "All set-aside cards brought back." });
        pushReviewState();
        pushDeferredList();
        break;
      case "proposal_supersede": {
        const current = devProposals.get(String(msg.id));
        if (!current || current.status !== "pending") break;
        devProposals.set(current.id, { ...current, status: "superseded" });
        window.chatUI?.dispatch({ type: "proposal_resolved", id: current.id, status: "superseded" });
        break;
      }
      case "proposal_reject":
        window.chatUI?.dispatch({ type: "proposal_resolved", id: msg.id, status: "rejected" });
        break;
      case "grading_accept": {
        const current = devGradings.get(String(msg.id));
        if (!current) break;
        const applied: GradingPayload = {
          ...current,
          status: "accepted",
          result: {
            card_ids: current.card_ids,
            preview_exits: [1700000000002],
            rescheduling_filtered: [],
            preserved_hidden_state: {
              suspended: [],
              user_buried: [1700000000001],
              scheduler_buried: [],
            },
            newly_suspended: [],
            warnings: [],
          },
          available_card_ids: [1700000000001],
        };
        devGradings.set(applied.id, applied);
        window.chatUI?.dispatch({ type: "grading", grading: applied });
        break;
      }
      case "grading_reject": {
        const current = devGradings.get(String(msg.id));
        if (!current) break;
        const rejected = { ...current, status: "rejected" as const };
        devGradings.set(rejected.id, rejected);
        window.chatUI?.dispatch({ type: "grading", grading: rejected });
        break;
      }
      case "grading_make_available": {
        const current = devGradings.get(String(msg.id));
        if (!current) break;
        const available: GradingPayload = {
          ...current,
          cards: current.cards.map((card) =>
            card.card_id === 1700000000001 ? { ...card, hidden_state: null, queue: 2 } : card
          ),
          available_card_ids: [],
          availability: {
            card_ids: [1700000000001],
            restored: { suspended: [], user_buried: [1700000000001], scheduler_buried: [] },
          },
        };
        devGradings.set(available.id, available);
        window.chatUI?.dispatch({ type: "grading", grading: available });
        break;
      }
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
