# Atlanta holdout, second set: Gemini ON (Stage F, protocol section 11)

Recorded: 2026-11-21

## What this is

The same 45 leads and the same frozen worksheet as `eval/HOLDOUT_RESULT.md`, run with Gemini ON (Brave plus Gemini plus reconcile, 4 workers) into `data/atlanta/c2/` on commit 03ea550. The scorer `eval/score_holdout_c2.py` was committed as 03ea550 before the run and was written from synthetic fixtures only. The recorded Brave-only line in `eval/HOLDOUT_RESULT.md` is unchanged and stays the headline.

## Scorer output (`data/atlanta/c2_score/score_c2.txt`, verbatim)

```
Atlanta holdout, c2 combined, Gemini ON: second set of figures beside the recorded Brave-only line; counts only (no names).

Population: the same lead list and frozen worksheet as the Brave-only holdout; leads in the sample 45.
Excluded from every denominator (in this order): errors 0, skipped at intake 0, Ambiguous 0, Not determined 2

All leads (45)
--------------
Delivered (combined): Atlanta holdout, c2 combined, Gemini ON, commit 03ea550, 2026-11-20: named 23/43 (53%) | lenient 17/23 = 73.9% [54%-87%] | strict 16/23 = 69.6% [49%-84%] | wrong 6 | correct blanks 4/5 | coverage 17/38 lenient, 16/38 strict | errors 0, excluded 2
Brave alone, same run: Atlanta holdout, c2 combined, Gemini ON | Brave alone, commit 03ea550, 2026-11-20: named 18/43 (42%) | lenient 7/18 = 38.9% [20%-61%] | strict 4/18 = 22.2% [9%-45%] | wrong 11 | correct blanks 4/5 | coverage 7/38 lenient, 4/38 strict | errors 0, excluded 2
Gemini alone, same run: Atlanta holdout, c2 combined, Gemini ON | Gemini alone, commit 03ea550, 2026-11-20: named 26/43 (60%) | lenient 20/26 = 76.9% [58%-89%] | strict 19/26 = 73.1% [54%-86%] | wrong 6 | correct blanks 5/5 | coverage 20/38 lenient, 19/38 strict | errors 0, excluded 2
  (Gemini gave no answer on 0 scored row(s): error, disabled or off; they are left out of the Gemini-alone line above, whose coverage is out of the rows it answered, and counted in the error figures below)
  Gemini alone, coverage against ALL 38 owner-bearing scored rows (an unanswered row is not found): lenient 20/38, strict 19/38
needs_review: 7 row(s), 7 in the scored set; 0 of those sit on no-owner shops and count as correct blanks above (nothing was delivered)
Reachable-correct (delivered correct, plus a correct name among the review_candidates): lenient 23/38, strict 22/38; delivered alone lenient 17/38, strict 16/38
Evidence URL on named rows: 23 of 23 (0 named row(s) without one)
Gemini errors: 0 of 45 lead(s) run = 0%; vendor-side 0 (none); other 0 (none); disabled after a 402: 0
  Brave fallback delivered on 0 of 0 error row(s). Rule: PASS (15% at most, every error vendor-side, fallback delivered on every error, no 402 or disabled row, 5xx under about 5% of the run)
Gemini cost (gemini_cost_usd, summed over 45 result row(s); a list-price estimate that ignores the free search tier): $1.5743
Brave + Anthropic cost, WHOLE RUN (run log, priced as src/cost.py prices it: 225 Brave calls, known to over-count timeouts): Anthropic $0.4974 + Brave $1.1250
Whole-run cost estimate: $3.1967

WITHOUT the 2 chain-overlap lead(s) (43)
----------------------------------------
Delivered (combined): Atlanta holdout, c2 combined, Gemini ON, without 2 chain-overlap, commit 03ea550, 2026-11-20: named 23/41 (56%) | lenient 17/23 = 73.9% [54%-87%] | strict 16/23 = 69.6% [49%-84%] | wrong 6 | correct blanks 4/5 | coverage 17/36 lenient, 16/36 strict | errors 0, excluded 2
Brave alone, same run: Atlanta holdout, c2 combined, Gemini ON, without 2 chain-overlap | Brave alone, commit 03ea550, 2026-11-20: named 17/41 (41%) | lenient 7/17 = 41.2% [22%-64%] | strict 4/17 = 23.5% [10%-47%] | wrong 10 | correct blanks 4/5 | coverage 7/36 lenient, 4/36 strict | errors 0, excluded 2
Gemini alone, same run: Atlanta holdout, c2 combined, Gemini ON, without 2 chain-overlap | Gemini alone, commit 03ea550, 2026-11-20: named 26/41 (63%) | lenient 20/26 = 76.9% [58%-89%] | strict 19/26 = 73.1% [54%-86%] | wrong 6 | correct blanks 5/5 | coverage 20/36 lenient, 19/36 strict | errors 0, excluded 2
  (Gemini gave no answer on 0 scored row(s): error, disabled or off; they are left out of the Gemini-alone line above, whose coverage is out of the rows it answered, and counted in the error figures below)
  Gemini alone, coverage against ALL 36 owner-bearing scored rows (an unanswered row is not found): lenient 20/36, strict 19/36
needs_review: 7 row(s), 7 in the scored set; 0 of those sit on no-owner shops and count as correct blanks above (nothing was delivered)
Reachable-correct (delivered correct, plus a correct name among the review_candidates): lenient 23/36, strict 22/36; delivered alone lenient 17/36, strict 16/36
Evidence URL on named rows: 23 of 23 (0 named row(s) without one)
Gemini errors: 0 of 43 lead(s) run = 0%; vendor-side 0 (none); other 0 (none); disabled after a 402: 0
  Brave fallback delivered on 0 of 0 error row(s). Rule: PASS (15% at most, every error vendor-side, fallback delivered on every error, no 402 or disabled row, 5xx under about 5% of the run)
Gemini cost (gemini_cost_usd, summed over 43 result row(s); a list-price estimate that ignores the free search tier): $1.4970
Brave + Anthropic cost, WHOLE RUN (run log, priced as src/cost.py prices it: 225 Brave calls, known to over-count timeouts): Anthropic $0.4974 + Brave $1.1250

git diff stage-f-run..<sealed commit> -- src/: empty; git diff stage-f-run..HEAD -- src/: empty. Empty: these count as holdout figures (protocol section 11).
Seal facts: git_commit 03ea5504b9ceb2ad2c2a79cd2b885f354f5b5c57; frozen_source_check PASS; denylist_lines_added 7; src_uncommitted_changes no; gemini_status ok 45
Scored on 2026-11-20. The worksheet is the one frozen for the Brave-only score; the recorded Brave-only line is unchanged by this set.
```

## Notes

(a) `git diff stage-f-run..HEAD -- src/` is empty, so these count as holdout figures under section 11.

(b) The Gemini-on and Brave-alone columns come from the same run. Brave results drift between days, so compare within the run, not with the 11-18 run.

(c) Real Gemini cost, from the AI Studio credit balance: $9.98 before the run, $9.59 the next morning, about $0.39. The cost lines in the scorer output are list-price estimates that ignore the free search tier.

(d) One run, one city, 45 leads. The noise band is about 2 answers, and Gemini alone swung 4 to 5 answers between runs on the Charlotte dev set.

(e) Charlotte numbers are not quoted here.

(f) No `src/` change was made before this commit.
