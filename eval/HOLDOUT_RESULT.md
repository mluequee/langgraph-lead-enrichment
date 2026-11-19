# Atlanta holdout result (Stage F)

Recorded: 2026-11-19

## Population

Funnel: 123 raw, 102 qualified, 21 not qualified (counted only), 1 skipped by the '@' rule, 7 denylist, 94 filtered, 45 drawn (seed 20261011).

City mix of the scored set: Atlanta 22, Decatur 6, Dunwoody 3, Marietta 2, Roswell 2, East Point 2, Brookhaven 1, Doraville 1, Norcross 1, Alpharetta 1, Sandy Springs 1, Chamblee 1.

## Headline

Atlanta holdout, Brave-only, commit 0e0b551, 2026-11-18: named 21/43 (49%) | lenient 8/21 = 38.1% [21%-59%] | strict 6/21 = 28.6% [14%-50%] | wrong 13 | correct blanks 4/5 | coverage 8/38 lenient, 6/38 strict | errors 0, excluded 2

## score.txt (verbatim)

```
Atlanta holdout, Brave-only, commit 0e0b551, 2026-11-18: named 21/43 (49%) | lenient 8/21 = 38.1% [21%-59%] | strict 6/21 = 28.6% [14%-50%] | wrong 13 | correct blanks 4/5 | coverage 8/38 lenient, 6/38 strict | errors 0, excluded 2

Population: the output of Ai Lead Qualification (n8n), Atlanta GA suburbs included; leads in the sample 45.
Excluded from every denominator (mutually exclusive, in this order): errors 0, skipped at intake 0, Ambiguous 0, Not determined 2
Scored set S = 43 (owner-bearing O = 38, no-owner N = 5); named 21; missed (blank on an owner shop) 18
Grades: full 6, partial 2, wrong 13, blank 18, correct blank 4

Precision by the pipeline's own confidence (lenient):
  high   4/9 = 44%
  medium 4/12 = 33%
Named rows without an evidence URL: 0 of 21
needs_review rows (cannot occur with Gemini off): 0
City mix (scored set): Atlanta 22, Decatur 6, Dunwoody 3, Marietta 2, Roswell 2, East Point 2, Brookhaven 1, Doraville 1, Norcross 1, Alpharetta 1, Sandy Springs 1, Chamblee 1

WITHOUT the 2 chain-overlap lead(s), commit 0e0b551, 2026-11-18: named 20/41 (49%) | lenient 8/20 = 40.0% [22%-61%] | strict 6/20 = 30.0% [15%-52%] | wrong 12 | correct blanks 4/5 | coverage 8/36 lenient, 6/36 strict | errors 0, excluded 2

Seal facts: git_commit 0e0b551448dcf7f72299a4f13b8df8fd85db9eac; frozen_source_check PASS; denylist_lines_added 7; src_uncommitted_changes no; gemini_status off 45
Frozen inputs (sha256, first 16): worksheet 6daf1bb28a0182b9; lead list c7e751ad88602dde; export 0fff54d1d4665449; database a23f9b2f85346a57
Scored on 2026-11-18. D6: blind truth only; any later correction is a footnote, not a change to the line above.
```

## Notes

- (a) Brave only, Gemini off, errors 0.
- (b) The correct-blank denominator is small (5).
- (c) Not determined 2, Ambiguous 0: 4% of the sample, under the protocol's 20% flag.
- (d) One lead's Google listing pointed at a Chattanooga website; it was left as pulled.
- (e) Charlotte numbers are not quoted here.
- (f) No src/ change was made before this commit.
