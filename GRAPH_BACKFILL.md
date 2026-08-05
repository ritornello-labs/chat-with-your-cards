
### §11 addendum: OpenAI Sol/Terra + stability check (run 2026-07-31)

Scratchpad purge forced a full re-run on a regenerated (same-seed) 300-note
sample — which doubles as a benchmark stability check: **the original five
models reproduce their ordering with soft-F1 shifts ≤ .017** (e.g. Opus
.822→.814, Kimi .794→.795). Fable gold dropped one batch this run (non-JSON
reply ×3 retries), so scoring covers 290 notes. Kimi dropped a null-content
batch **again** — two-for-two on flaky batches, a real operational mark.

| Model | soft F1 | soft P | soft R | distinct | cost (300 notes) |
|---|---:|---:|---:|---:|---:|
| Haiku 4.5 | .708 | .789 | .670 | 696 | $0.12 |
| **Terra (gpt-5.6-terra)** | .733 | .732 | **.759** | 771 | **$0.14** |
| Sonnet 5 | .776 | .831 | .746 | 615 | $0.41 |
| Kimi K3 | .795 | .825 | .789 | 643 | $0.81 |
| Opus 4.8 | .814 | **.874** | .784 | 656 | $0.73 |
| **Sol (gpt-5.6-sol)** | .730 | .735 | .749 | 778 | $0.90 |
| Fable 5 (gold) | — | — | — | 652 | $2.07 |

**Findings:**
1. **Terra is the budget surprise**: at Haiku's price it beats Haiku
   outright and its *recall* (.759) — our load-bearing metric — beats
   Sonnet's (.746). Cheapest model with recall above .75. The budget pick
   for any future low-cost tier (SaaS) or recall-oriented pre-pass.
2. **Sol is dominated by Opus**: same price class, .730 vs .814 soft F1.
   Both OpenAI models show the same profile — decent recall, low precision,
   scattered naming (distinct 771–778 vs Anthropic's 615–696).
3. **Family-bias caveat, recorded honestly**: the gold is Fable, so
   Anthropic models may score high partly by sharing naming conventions
   with the judge-family rather than by being better. The family-neutral
   convergence metric still favors Anthropic (fewer distinct names = more
   canonical convergence), so the ordering likely stands, but the
   OpenAI-vs-Anthropic gaps should be read with ~±0.03 skepticism; the
   within-family ordering (Terra > Haiku, Opus > Sonnet) is clean.

**Decision unchanged:** Fable 5 for Phase B backfill + incremental. Updated
secondary picks: Terra replaces Haiku as the budget/pre-pass option; Kimi K3
loses on reliability (2/2 runs with dropped batches) despite decent quality.

### §11 qualitative post-mortem (2026-07-31): reading the divergent judgments

Manual review of the most divergent notes (lowest mean F1 across all six
models vs gold, Haiku-blank cases, and OpenAI-low/Anthropic-high cases).
Five error species, with pipeline consequences:

1. **The degenerate-note underclass.** 5/300 notes have <60 chars of
   stripped text — 4 of them because the real content is an `<img>` (LaTeX
   screenshots) the HTML stripper deletes. On these, models split by
   *policy*, not ability: Haiku/Terra/Opus/Sol blank ~60%, **Fable blanks
   0%** — it salvages ("equação característica" recovered from a mangled
   fragment; a header-only note correctly tagged "de morgan's laws").
   → *Pipeline:* media-only/short notes get routed to a review lane, not
   scored as extractions; production text extraction must at least detect
   image-borne content (and `content_hash` must include media refs, else
   image edits are invisible to the sweep).
2. **Synonym tax / family dialects — the family bias made concrete.**
   Terra/Sol say "mean square convergence"; Anthropic models say
   "convergence in quadratic mean". Both are canonical textbook names;
   word-Jaccard soft-match fails the pair; Terra/Sol eat a false penalty.
   → The benchmark *underestimates* cross-family models; fair scoring would
   re-score after alias normalization. Anthropic-vs-OpenAI gaps shrink;
   within-family orderings unaffected.
3. **Concept-boundary underdetermination.** The Chiswell-Hodges unique-
   readability note yields five *defensible* framings across models
   ("unique readability" / "structural induction" / "induction on formulas"
   / "formula parsing"). The gold is a choice, not a truth — some of the
   .18 gap between Opus and gold is irreducible naming freedom, not error.
4. **Inference vs hallucination frontier.** On a truncated linear-algebra
   fragment, Kimi extracted "rank-nullity theorem" — correct for the full
   note, but inferred from "dim V" + context. Impressive and slightly
   scary; the Phase D judge (which sees full note text) is the check.
   Fable's `presupposes` are the most *curricular* ("cash flow diagram"
   presupposes "time value of money") — exactly the seed quality Phase D
   wants.
5. **Haiku's true signature.** Beyond 3% outright blanks on normal notes
   (other models: 0–1%), when Haiku does answer it invents *descriptive
   phrases* instead of canonical names ("price system interest
   calculation", "financial english terminology"). The differentiator
   across the ladder is naming instinct, not comprehension.
6. *(Bonus)* **Cross-lingual canonicalization works everywhere**: all
   seven models map Portuguese notes ("sistema PRICE", "equação
   característica") to English canonical names unprompted.

### §11 adjudicated re-score (2026-07-31) + grading process rule

Benchmark assets (harness, fixed sample, per-model results, alias tables)
now live in the **private `anki-graph-bench` repo** — collection note text
never enters this repo, which may go public. Run log there is authoritative.

**Process rule (standing):** mechanical soft-match scoring is not quotable.
Every benchmark run ends with a manual adjudication pass — a reviewer reads
all recurring unmatched pred↔gold pairs and classifies each as alias
(merge), distinct (ignore), or trap (document) — and only post-adjudication
numbers count. The adjudication is cumulative and doubles as the seed of
the production alias table.

Run 2 (7 models, regenerated sample) after adjudication: 53 alias groups,
normalizer fixes (accents/apostrophes/stem repairs), 5 degenerate notes
excluded:

| Model | soft F1 | soft P | soft R | Δ vs raw | cost |
|---|---:|---:|---:|---:|---:|
| Haiku 4.5 | .739 | .825 | .698 | +.031 | $0.12 |
| GPT-5.6 Sol | .772 | .779 | .791 | +.042 | $0.90 |
| GPT-5.6 Terra | .787 | .784 | .815 | +.054 | $0.14 |
| Sonnet 5 | .808 | .864 | .776 | +.032 | $0.41 |
| Kimi K3 | .832 | .862 | .825 | +.037 | $0.81 |
| Opus 4.8 | .844 | .908 | .812 | +.030 | $0.73 |

- The dialect tax was real and uneven: cross-family models gained most
  (Terra +.054, Kimi +.037) — the family-bias correction §11 predicted.
- **Terra strictly dominates Haiku** (equal price, +.048 F1, +.117 recall):
  new budget-tier pick. **Sol is poor value** (Opus price, sub-Sonnet
  quality, most over-generated names). Kimi nearly closes on Opus (.832 vs
  .844) but stays slightly dominated and dropped a batch in both runs.
- Run-to-run stability on the shared four models: ±.02 soft F1.
- Decision unchanged: Fable 5 for Phase B; the remaining ~.16 gap at Opus
  includes an unquantified concept-boundary-underdetermination floor.

### §11 the underdetermination floor, measured (2026-07-31)

A second Fable run on the identical sample, scored against the first through
the adjudicated pipeline, gives **Fable self-agreement = .910 soft F1**. That
missing .09 is the *underdetermination floor*: on notes admitting several
defensible framings ("unique readability" vs "structural induction"), even
the gold model disagrees with itself. No model can score 1.0; .910 is the
ceiling. Ceiling-normalized, **Opus 4.8 reaches 93% of achievable
agreement** — half its apparent .156 gap to gold was floor, and its real
quality gap to Fable is ~.07 soft F1.

Cost to close that ~.07 on the full 24k-note backfill (measured ~224 in /
~96 out tokens per note): Fable ~$170 OpenRouter / ~$85 Anthropic-batch vs
Opus ~$85 / ~$42 — a **~$43 one-time premium at batch rates**. Decision
posture updated from "Fable, clearly" to "Fable by preference, Opus fully
defensible": the premium is trivial in absolute dollars, but the measured
delta is the last 7% of achievable agreement, not a categorical gap.
(Also noted: both Fable runs dropped one batch via OpenRouter — provider
flakiness is not Kimi-specific; the production backfill on Anthropic's
batch API sidesteps it.)

### §11 disagreement autopsy (2026-07-31): the .07 gap is not accuracy

Manual classification of **all 184 notes** where Opus and Fable-A disagree
(full report: `anki-graph-bench/opus_fable_classification.md`, Fable-B as
third witness):

- **167 (91%) defensible differences** — both readings valid.
- **7 (4%) scoring artifacts** — residual alias gaps ("BLUE" ↔ "best linear
  unbiased estimator").
- **Opus clear failures: 3** (+2 single-name blemishes) ≈ 1.5% of notes.
- **Fable clear failures: 5** ≈ 1.8% of notes — invisible in the score
  because gold-by-construction can't fail against itself; each was caught
  by Opus and/or Fable-B (e.g. missing "compounding" on the note defining
  it; missing "diagonal matrix" when it's in the prompt).

**Conclusion: on contested judgments, Opus and Fable fail at
indistinguishable rates.** The .07 soft-F1 gap decomposes into Fable's
richer valid-prerequisite output (useful as Phase D seeds), naming freedom,
and alias-table residue — not correctness. Decision updated accordingly:
**Opus 4.8 is the value-rational Phase B default** (~$42 batch,
equally accurate); the Fable premium (~$43) buys denser `presupposes`
seeding and the salvage-don't-blank policy on degenerate notes, not fewer
errors. Either choice remains a cheap watermark re-run away from the other.

## 12. The labeling log and mixture-of-experts (v4, user-directed)

**The change.** v2's sweep semantics were *recompute-and-overwrite*: change
the extractor and every note's stored labeling is invalidated and replaced.
That discards paid-for judgments — and §11's autopsy proved they retain
value: every one of the 8 clear failures found (3 Opus, 5 Fable) was caught
by *another labeling we already owned*. So:

- **`extractions` becomes an append-only log**, keyed by
  `(note_guid, content_hash, model, prompt_version)`: covers, presupposes,
  created_at, cost. Rows are never updated or deleted. Re-running a model
  on changed content adds rows; adding a new model adds rows; nothing is
  lost. (Storage is trivial — labelings are a few hundred bytes.)
- **The graph is a compiled view**: a pure function
  `(extraction log, alias table, policy) → covers/requires edges`.
  Recompiling is free and local; only *labeling* costs money.
- **The sweep rule changes** from "recompute if hash mismatch" to "ensure
  the active policy's required labelings exist for the current
  content_hash". Cache semantics per (content_hash, model, prompt) —
  the watermark machinery survives intact, one level down.

**Aggregation policies** (pluggable, start simple):

1. `single(model)` — v2 behavior, a degenerate policy.
2. `consensus(k-of-n)` — a concept enters the graph as **confirmed** when
   ≥k labelings support it; single-source concepts enter as
   **provisional** (usable for hints, not for gates). Measured on the
   three labelings already in the log (Opus + Fable-A + Fable-B, 285
   notes): union 4.33 concepts/note, **2-of-3 consensus 3.52 (81% of the
   union)**, single-source tail 0.81/note — and majority vote catches all
   8 autopsied clear failures.
3. `router` (true MoE, later) — dispatch by note type: degenerate/media
   notes → a salvage-capable model (Fable), long extracts → high-recall
   model, plain notes → the cheap tier; or weighted voting once per-model
   reliability priors accumulate from adjudications.

**Cost framing.** Ensembling multiplies labeling cost by the number of
voters — but §11 priced a full-corpus pass at $42–85/model on the batch
API, so even a 3-voter ensemble is ~$150–250 one-time, and each new
model's pass is additive forever. The seven benchmark labelings of the
300-note sample are, retroactively, the log's first entries: the benchmark
and production now share one data structure.

**Interaction with §6 phases:** unchanged externally — Phase C compiles
`covers` from the log via policy; Phase D judges over the compiled concept
set. The judge's verdicts should also append to a log of their own
(`judgments`), for the same reasons.

## 13. Chosen configuration & full budget (2026-07-31)

Decided: **Phase D = single voter, Opus 4.8** (neighborhood judge over the
recurrent hub tier, ~2,400 in / ~250 out per concept). Phase B voter set
still open: single Opus vs the 2-of-3 consensus trio (Opus + Fable + Terra).

End-to-end backfill bill (Anthropic batch API where possible; Terra via
OpenRouter or OpenAI batch):

| Item | Minimal (B=Opus solo) | Recommended (B=trio) |
|---|---:|---:|
| A harvest (deterministic) | $0 | $0 |
| B extraction, 24k notes | ~$42 | ~$140 ($42 Opus + $85 Fable + ~$15 Terra) |
| Embeddings (all stages) | ~$0.25 | ~$0.25 |
| Normalization adjudication band | ~$5 | ~$5 |
| C covers confirm pass | ~$5 | ~$5 |
| D judge (Opus, hub tier ~3k) | ~$30 | ~$30 |
| D benchmark first | ~$10 | ~$10 |
| **Total one-time** | **~$90** | **~$190** |

Judging all ~10k raw names in D instead of the hub tier adds ~$60. Steady
state: ~1–2¢ per new/edited note across all stages (the log means re-runs
only ever buy missing labelings). Prerequisite: Anthropic API credits
(balance currently empty; the batch discount requires the direct API, not
OpenRouter).

## 14. Phase B executed (2026-08-03): full-corpus Opus pass complete

The backfill ran on the Max subscription via Opus subagents (marginal cost
~zero; API pricing reserved for the SaaS posture). **23,797 notes labeled**
across 19 waves / 97 shards; `sweep_missing()` returns 0. Full run log and
data: private `anki-graph-bench` repo.

| Metric | Measured |
|---|---:|
| covers/note | 1.57 |
| presupposes/note | 1.86 |
| distinct concept names | **21,083** |
| recurring (≥2 notes) | 11,351 |
| ≥5 notes (Phase D hub tier) | 4,427 |
| ≥10 notes | 1,728 |
| candidate `requires` pairs (free, from `presupposes`) | **71,826** |

**Against §10's projection (8–15k):** 21.1k is ~40% over the top of range.
The Heaps fit was calibrated on a *dense single-domain* sample; the full
corpus spans dozens of domains whose vocabularies barely overlap, so
cross-domain breadth beat the projection's downward biases. The
consequential number was right, though: the recurrent tier is ~11k and the
≥5× band ~4.4k — Phase D judging over that tier stays in the tens of
dollars, exactly as §13 budgeted. Cross-domain sparsity also confirms §10's
finding that the graph will be a union of domain-local clusters.

**Operational validation of v4 (§12):** one subagent died mid-response; its
176 completed notes ingested normally and the remaining 74 re-queued via the
sweep — zero data loss, no manual repair. Idempotent ingest also absorbed
358 duplicate nids from an early exporter bug without corruption.

**Next:** normalization cascade over the 21k names (deterministic →
embedding-cluster → adjudicated aliases) — now clearly load-bearing at this
scale — then Phase D over the hub tier. Adding Fable as a second voter is
purely additive whenever wanted (same shards, new model tag).

## 15. Phase D depth: measured candidate structure (2026-08-03)

User decision: **add Fable as a second voter, and reach deep in Phase D**
(do not stop at the ≥5× hub tier). Sizing the "deep" option requires the
actual candidate structure, not the note-level pair count. Collapsing the
71,826 note-level `covers × presupposes` pairs by concept (naive + alias
normalization, Opus voter) gives **53,085 distinct directed candidate
pairs** — a 26% collapse, because the same prerequisite relation is asserted
by many notes.

| Tier (both endpoints ≥ N notes) | concepts | candidate pairs | pairs w/ support ≥2 | prereqs per head |
|---|---:|---:|---:|---:|
| ≥1 (everything) | 21,083 | 53,085 | 10,163 | 3.4 |
| ≥2 (recurrent) | 11,351 | 37,416 | 10,163 | 4.1 |
| ≥3 | 7,737 | 28,768 | 8,604 | 4.5 |
| ≥5 (the §13 default) | 4,427 | 18,400 | 5,941 | 5.0 |
| ≥10 | 1,728 | 7,370 | 2,725 | 5.1 |

Three things this table settles:

1. **Depth is affordable because the judge is per-head, not per-pair.** The
   unit of work is a concept's neighborhood (head + its 3–5 candidate
   prerequisites, judged together in one call). Full depth is ~15.6k
   neighborhoods vs ~3.7k at ≥5× — roughly 2× the Phase B subagent effort,
   at ~zero marginal cost on the subscription. API-priced (the SaaS number)
   it is ~$110 rather than §13's ~$30.
2. **Support ≥2 is not a usable filter for depth.** Every support-≥2 pair
   already has both endpoints recurrent, so the ≥1 and ≥2 tiers share an
   identical 10,163 corroborated pairs. The extra 15,669 pairs that full
   depth buys are *all* single-note assertions — which is exactly the set
   where a second opinion has the most value, and exactly the set a
   support-threshold would have discarded.
3. **Normalization must precede D, not follow it.** Judging `linear map`
   and `linear transformation` as separate heads both wastes calls and
   splits the resulting graph — and at 21k names the unmerged tail is where
   most of the depth lives.

Phase D therefore runs at **tier ≥1 (full depth)**, after normalization,
single-voter Opus, with its own adjudicated benchmark first per the standing
rule (§11). Judge output stays a *proposal* (gbrain lesson, §7): edges land
with provenance and confidence, and cycle-creating edges are rejected at
write time rather than silently reoriented.

## 16. Two-voter corpus measurement (2026-08-04) — and a reframing of Phase D

The Fable second-voter pass reached 19,725 / 23,702 notes before a
subscription quota wall (the remainder is queued; the log makes the pause
free). That is enough dual coverage to measure the thing the second voter
was bought for.

**Opus vs Fable, full corpus, adjudicated normalizer:** mean per-note soft
F1 **.771**; 30.2% of notes in perfect agreement, 2.4% with zero overlap.
Split by field:

| Field | Cross-voter overlap |
|---|---:|
| `covers` (what the note teaches) | **.601** |
| `presupposes` (what it assumes) | **.427** |

Three consequences for the design:

**a. The benchmark measured the easy end.** §11's ladder (Opus .844 against
Fable gold, .910 self-agreement ceiling) came from a dense single-domain
Math sample. The same two models agree at .771 across the real corpus. The
ranking is probably still ordinally right, but every absolute number in §11
is optimistic, and the underdetermination floor corpus-wide is larger than
the .09 measured on Math. Any public accuracy claim must be re-measured on a
sample stratified across deck groups.

**b. Phase D is not polish — it is what makes the edges usable.** The
53,085 candidate `requires` pairs (§15) are derived entirely from
`presupposes`, which is the *least* reproducible field in the pipeline
(.427). Reading §15 and §16 together: the free candidate edges are
plentiful, cheap, and individually unreliable, and the judge is the step
that converts them into something a scheduler can act on. This retires any
version of the plan where raw `presupposes` ships as a dependency graph, and
it strengthens the full-depth decision — a support threshold would have
concentrated the judge on the pairs that least need judging.

**c. Normalization gets no help from string cleaning.** Stage 1 of the
cascade (deterministic: token-set equality, acronym expansion, parenthetical
flagging) collapses **1.2%** of the 24,290-name vocabulary. 33.2% of names
occur exactly once. The dedup problem is semantic; embeddings plus
adjudication carry all of it. Budget accordingly — this was assumed to be
the cheap stage and it is not.

An implementation note worth keeping: the first acronym rule proposed
`ip ~ italian pasta ~ invasion of poland`. Tightening to ≥3 characters with
a unique expansion fixed it. Stage 1 emits *proposals* into
`norm_candidates.txt` and never auto-merges — the gbrain findings-as-proposals
rule (§7) earning its place on the first real run.
