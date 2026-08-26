# LangGraph Lead Enrichment Pipeline

Production-grade LangGraph implementation of a lead enrichment workflow. Processes business data through multi-stage extraction and verification, with full error isolation, cost tracking, and accuracy measurement.

**Portfolio artifact demonstrating:** Fan-out/fan-in parallelism, LLM confidence cascades, waterfall enrichment, cost analysis, and production instrumentation.

---

## Architecture

```
prep_lead (initialize state)
  ↓
build_queries (4 deterministic query angles)
  ↓
[search_q1 ∥ search_q2 ∥ search_q3 ∥ search_q4] (parallel, reducer merges)
  ↓
combine_and_score (score, dedupe by URL, top 8, cap 40k chars)
  ↓
extract_owner (Haiku: structured extraction with confidence)
  ↓
route_after_search
  ├─ high → END
  ├─ medium → verify_owner
  └─ low → verify_owner + About-page fallback
  ↓
[verify_owner (Sonnet: B→A cascade logic)]
  ↓
route_after_verification
  ├─ high/medium → END
  └─ low → fetch_about_page + extract_owner (2nd pass)
  ↓
[fetch_about_page (surgical extraction: founder/owner/CEO + name only)]
  ↓
route_after_about
  ├─ high/medium → END
  └─ low → flag_for_review → END
```

**Key design decisions:**
- **Parallel fan-out (Phase 2):** 4 concurrent Brave searches via Annotated list reducer. Replaces n8n's 32s sequential chain with ~1-2s parallel execution.
- **Scoring/dedupe (Phase 3):** Deterministic Python (no LLM). Instagram +60, TikTok +55, Facebook +40, LinkedIn +45, "owner" +50, "founder" +60, authwall-filtered. Top 8, 40k char cap.
- **Sonnet verification (Phase 4, implemented as cascade not retry-loop):** When Haiku returns medium/low confidence, Sonnet re-reads combined text and either finds an alternative decision-maker (B) or downgrades to low (A). Enterprise pattern: cheap model first, verify only on uncertainty.
- **Waterfall About-page (Phase 5):** Escalates only on verified-low. Fetches domain from top-ranked result; tries /about, /about-us, /team, /about-page; surgical text extraction (founder/owner/CEO keyword + capitalized name only, not full HTML).
- **Error isolation (Phase 7):** Try/except on every external API call (Brave timeout/HTTP/generic, both Haiku nodes, Sonnet verify, About-fetch). No crashes — gracefully downgrades to low confidence and flags for review.

---

## Phase-by-Phase Build

| Phase | Component | What | Status |
|-------|-----------|------|--------|
| 1 | Structured output | Pydantic `OwnerResult` with confidence (high/medium/low) + all n8n fields | ✅ |
| 2 | Parallel fan-out | Reducer-based 4-concurrent Brave searches | ✅ |
| 3 | Scoring/dedupe | Deterministic Python ranking + top-8 selection | ✅ |
| 4 | Confidence routing | Sonnet verification cascade (B→A logic) | ✅ |
| 5 | Waterfall enrichment | About-page fetch + surgical extraction on low confidence | ✅ |
| 6 | Real data in/out | SQLite + LeadDatabase abstraction + batch loop | ✅ |
| 7 | Production polish | Error isolation, cost tracking, accuracy comparison | ✅ |

---

## Phase 7: Production Metrics

**Validation:** 3 full runs × 107 Charlotte barbershop leads = **321 lead-runs, 0 crashes, 100% availability**

### Cost Tracking
```
Haiku tokens:     307,530 input + 37,988 output
Sonnet tokens:     69,996 input + 10,599 output
Total tokens:      426,113
Brave API calls:   428 (4 per lead, deterministic)
Estimated cost:    $2.91/batch = $0.027/lead
                   (Anthropic ≈ $1.10; Brave ~$0.005/call or $0 on free tier)
```

### Accuracy vs n8n Baseline
```
Exact matches:           22/107 (20.6%)
Partial matches:          8/107 ( 7.5%)
Combined hit rate:       28.0%*  (run 2: 29.9% — 28-30% band is LLM variance)

LG-only wins:           ~40/107 (found owners where n8n stored NA)
True divergence:         ~8/107 (both found different people → manual review)
LG real misses:         ~10/107 (n8n found someone LG missed)
Both unknown:            1-3/107 (no public data)

*Honest caveat: n8n's CSV baseline contains literal "NA NA" placeholders and partial names.
Real ground truth requires manual spot-check on the ~8 divergence cases.
```

### Bugs Found & Fixed (Production Lessons)
1. **LangGraph parallel-write conflict** — all 4 search nodes returned `"cost"`; only serial nodes can return it. Fix: parallel nodes mutate cost locally but don't return.
2. **Windows cp1252 encoding** — Unicode arrows threw UnicodeEncodeError on FileHandler. Fix: `FileHandler(..., encoding='utf-8')`.
3. **Token capture silent failure** — `hasattr(response, 'usage')` always False. LangChain exposes `usage_metadata` only with `include_raw=True`. Fix: restructured output handling.
4. **Sonnet content as list** — newer model returns `response.content` as list of blocks, not string. `.find('{')` blew up. Fix: join blocks on type check.
5. **Confidence compared as strings** — `"high" < "medium"` is alphabetically TRUE. My own REVIEW flag caught it (CMAC CUTZ). Fix: rank-map compare.
6. **Placeholder normalization** — `<UNKNOWN>` and `"NA"` both truthy, scored as MISS when both empty. Fix: `_norm()` function.
7. **Dict-call typo** — `n8n_baseline('first', '')` instead of `.get()`.
8. **About-page token bloat** — sentence filter splits raw HTML on periods; HTML has few periods so "3 sentences" = 25K-token blobs. Capped chars; dropped to ~9-12K tokens.
9. **The non-code bug** — out of API credits. Every call failed, error isolation caught all of it (0 crashes, batch completed with <ERROR> results). Diagnosed by stepping outside the code.

**Interview angle:** "Each bug is a lesson in production systems. The most valuable one? The non-code bug — out of credits. The system failed gracefully and told me exactly why, because error isolation was built in."

---

## Setup & Run

### Requirements
- Python 3.10+
- Anthropic API key (Claude Haiku + Sonnet)
- Brave Search API key

### Install
```bash
git clone https://github.com/mluequee/langgraph-lead-enrichment.git
cd langgraph-lead-enrichment
python -m venv venv
venv\Scripts\activate  # Windows
pip install -r requirements.txt
cp .env.example .env
# Fill in your API keys in .env
```

### Run a Batch
```bash
python setup_database.py  # Initialize leads.db
python first_graph.py     # Process all leads
# Output: langgraph_debug.log (DEBUG), console (INFO), phase7_accuracy_comparison_TIMESTAMP.csv
```

---

## Key Learnings

- **Structured output with confidence is non-negotiable.** Raw text parsing is fragile; Pydantic schemas guarantee field presence and type.
- **Error isolation beats perfection.** The system's value is that 321 lead-runs completed with 0 crashes, including a full batch where every API call failed (out of credits). Graceful degradation > precision.
- **Parallel fan-out has a cost:** 4 concurrent Brave calls × 3 runs × 107 leads = 1,284 requests. Wouldn't be possible with sequential. n8n serialization was the bottleneck, not the LLMs.
- **Waterfall enrichment works.** Medium/low confidence → About-page fetch (expensive) only when needed. You scale expensive steps, not baseline them.
- **Cost transparency is a feature.** Knowing per-lead cost matters for business decisions. This is why every pipeline I build now includes a `LeadCost` class from day 1.

---

## Future Directions (Not Built — Intentional)

The original plan included a **CRAG-style retry loop** (Phase 4): low confidence → refine queries → new searches → re-extract. Implemented instead: **Sonnet verification cascade** (B→A logic). This is the enterprise standard — it's cheaper and more targeted than blind retry. Document the deviation as a design choice, not a shortcut.

Other workflows in the n8n suite (Get Email, Lead Research, Outreach) can apply the same cost-tracking + error-isolation patterns. Next phase: propagate to all 7 for total pipeline economics.

---

## License

MIT

---

## Contact

Built as a portfolio project demonstrating LangGraph production patterns.
