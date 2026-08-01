
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
