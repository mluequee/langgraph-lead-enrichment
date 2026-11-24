# LangGraph Lead Enrichment Pipeline

Finds the owner's name for a local storefront business from public web sources. Every name it delivers comes with a source link. When the evidence is weak or two sources disagree, it leaves the field blank or flags the row for review instead of guessing.

This is a portfolio project. The point is the engineering and the honest measurement, not a data product.

---

## Measured accuracy (sealed holdout, Atlanta, November 2026)

The code was frozen, then run once on 45 Atlanta-area barbershops it had never seen. The owners were researched by hand before the run, and the answers stayed sealed until scoring.

| Configuration | Names delivered | Correct, lenient | Correct, strict | Wrong | Correct blanks |
|---|---|---|---|---|---|
| Brave search only | 21 of 43 | 8/21 = 38.1% (21% to 59%) | 6/21 = 28.6% (14% to 50%) | 13 | 4 of 5 |
| Brave + Gemini, reconciled | 23 of 43 | 17/23 = 73.9% (54% to 87%) | 16/23 = 69.6% (49% to 84%) | 6 | 4 of 5 |

Ranges are 95% intervals. Lenient counts a partial name (first name only, or a name the owner goes by) as correct. Strict needs the full name. Two of the 45 leads could not be determined by hand and are excluded from every denominator.

What these numbers do and do not say:
- The sample is small (23 delivered names), from one metro and one industry. Read it as a range, not a single figure.
- Every delivered name in the combined run had an evidence URL (23 of 23).
- Cost of the combined run: about $2.00 for 45 leads, about $0.045 a lead, read from billing.

Full scorer output: [`eval/HOLDOUT_RESULT.md`](eval/HOLDOUT_RESULT.md) and [`eval/HOLDOUT_RESULT_C2.md`](eval/HOLDOUT_RESULT_C2.md). Protocol: [`eval/HOLDOUT_PROTOCOL.md`](eval/HOLDOUT_PROTOCOL.md).

### Correction to earlier versions of this README

Earlier versions reported a hit rate against the old n8n workflow and counted about 40 leads as "LangGraph-only wins." That measured how often a name was filled in, not whether it was right. A blinded, hand-verified audit in September 2026 found that when the pipeline wrote a name it was right 17 of 41 times (41%), against 18 of 27 (67%) for the n8n workflow. The rebuild that followed (evidence-or-blank rules, location anchoring, a second Gemini path and reconciliation) is what the Atlanta holdout above measures.

---

## How it works

```
prep_lead
  |-- Brave branch
  |     build_queries          5 query angles, deterministic
  |     search_q1 .. search_q5 parallel Brave searches (fan-out, reducer merges)
  |     combine_and_score      location anchor, dedupe, ranking (no LLM)
  |     extract_owner          Claude Haiku, structured output, evidence check
  |       uncertain   -> verify_owner (Claude Sonnet)
  |       still low   -> fetch_about_page -> extract_owner_from_about
  |       still low   -> flag_for_review
  |
  |-- Gemini branch (optional, --gemini)
  |     gemini_search          Gemini with Google Search grounding, one call per lead
  |
reconcile                      waits for both branches, pure Python
finalize
```

**Reconcile rules** ([`src/reconcile.py`](src/reconcile.py)):
- Both paths name the same person: deliver the fuller name.
- Only one path names someone: deliver it only with a surname (and, for Gemini, only with an evidence URL).
- The paths name different people: deliver nobody, mark the row for review and keep both candidates with their sources.
- Gemini fails or is off: the Brave answer goes through unchanged. A Gemini failure never costs an answer.

**Key design decisions:**
- **Parallel fan-out.** Five concurrent searches through a list reducer replaced the old n8n chain of sequential searches and fixed waits.
- **Deterministic scoring.** Ranking, dedupe and the location gate are plain Python, so they are cheap, repeatable and testable without API calls.
- **Evidence or blank.** A name is delivered only if the cited source supports it, a name that only repeats the business name is rejected, and confidence is capped when the evidence is thin.
- **Cheap model first.** Haiku extracts; Sonnet runs only when Haiku is unsure; the About-page fetch runs only when both are.
- **Error isolation.** Every external call is wrapped. A failed call downgrades the row instead of crashing the batch.
- **Cost tracking.** Every run records per-lead cost.

---

## Known limits

- **Same-name shop in another city.** The location gate discards results that name another state or a full city-and-state address. A source that names only a bare city passes. This caused two of the real errors in the holdout. The fix would change behavior and is not built.
- **Employee taken as owner.** One holdout error was a barber who works at the shop, not the owner.
- **Website reading was measured, not built.** On the Charlotte development set, reading shop websites would add at most 0 to 2 correct names out of 47 ([`eval/diag_ceiling.py`](eval/diag_ceiling.py), [`eval/diag_fetch_status.py`](eval/diag_fetch_status.py)).
- **Brave-only is weak.** Without the Gemini path, under 4 in 10 delivered names were right.

---

## Build history

| Stage | What |
|---|---|
| Phases 1-7 | Port of the n8n "Get Persons" workflow: structured output with confidence, parallel fan-out, deterministic scoring, Sonnet verification, About-page fallback, SQLite batch runs, error isolation and cost tracking |
| September 2026 audit | Blinded hand-verified audit found 41% precision; the README's earlier accuracy claim was withdrawn |
| Phase 8 | Identity anchor: location-anchored search, deterministic location gate, regression harness |
| Phase 9 | Evidence or blank: citation gate, anti-echo, calibration floor |
| Accuracy rebuild | Evidence and source records, Gemini grounding as a second path, pure-Python reconcile |
| Holdout | Frozen code, blind hand-researched worksheet, sealed answers, scorers written and committed before the runs |

**Reliability check (Phase 7):** 3 full runs of 107 Charlotte leads, 321 lead-runs, 0 crashes, including a batch where every API call failed because the account was out of credits.

### Bugs found and fixed
1. **LangGraph parallel-write conflict.** All parallel search nodes returned `"cost"`; only serial nodes can. Fix: parallel nodes update cost locally and do not return it.
2. **Windows cp1252 encoding.** Unicode arrows threw UnicodeEncodeError in the log file handler. Fix: `FileHandler(..., encoding='utf-8')`.
3. **Token capture silent failure.** `hasattr(response, 'usage')` was always False. LangChain exposes `usage_metadata` only with `include_raw=True`.
4. **Sonnet content as a list.** Newer models return `response.content` as a list of blocks, not a string. Fix: join text blocks.
5. **Confidence compared as strings.** `"high" < "medium"` is alphabetically true. Fix: rank-map compare.
6. **Placeholder normalization.** `<UNKNOWN>` and `"NA"` are both truthy and were scored as misses. Fix: one shared no-name set.
7. **Dict-call typo.** `n8n_baseline('first', '')` instead of `.get()`.
8. **About-page token bloat.** A sentence filter split raw HTML on periods, producing 25K-token blobs. Fix: character caps.
9. **The non-code bug.** Out of API credits. Error isolation caught every failure and the batch finished with flagged rows.

---

## Setup and run

### Requirements
- Python 3.10+
- Anthropic API key (Claude Haiku and Sonnet)
- Brave Search API key
- Gemini API key, only if you run with `--gemini`

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

### Run a batch
```bash
python src/setup_database.py  # Initialize leads.db
python src/run.py             # Process all leads (Gemini is off; --gemini opts in and costs money)
# Output: langgraph_debug.log (DEBUG), console (INFO), enriched_leads.csv, skipped_leads.csv
```

---

## What I learned

- **Fill rate is not accuracy.** The first version looked strong because it filled in names. A blinded check against hand-verified owners showed most of them were wrong. Nothing counts until it is tested against verified data.
- **Freeze before you measure.** Tuning on the same leads you score inflates the number. The holdout was run once, on frozen code, with the scorer committed first.
- **Two independent paths beat one.** Agreement between Brave and Gemini is a stronger signal than either model's own confidence label.
- **Blank is a valid answer.** Holding back a weak name is worth more to an outreach list than a confident wrong one.
- **Error isolation and cost tracking from day one.** Both paid for themselves the first time an API ran out of credits.

---

## License

MIT
