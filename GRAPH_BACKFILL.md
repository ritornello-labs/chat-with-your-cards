
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
