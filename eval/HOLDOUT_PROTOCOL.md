# Stage F: Atlanta holdout protocol (pre-registered)

Written and committed BEFORE any Atlanta lead exists in this repo. The git timestamp of this file is the
proof that the definitions below were fixed before the data was seen. Everything here is decided by the
stage F plan and decisions D1-D13 (2026-10-11); nothing in this file may be changed to suit a result.

Contains no names of real people. The Atlanta data, the worksheet and every output under `data/atlanta/`
hold real people's names and stay out of git (`data/`, `*.csv`, `*.db`, `*.log`, `*.xlsx` are all ignored).

## 1. Purpose and what the number means

The 55-row Charlotte audit set is the DEVELOPMENT set: the pipeline was tuned against it, so its numbers
overstate real performance and never go on a listing. This holdout is one cold, Brave-only run of the
`stage-e-done` pipeline on leads it has never seen, in a different city, verified blind. It yields one
reported number.

**Population (D12).** The leads are the output of the n8n workflow Ai Lead Qualification (after Get
Businesses and Get Websites), for Atlanta, GA, suburbs included (D3). The result describes "leads MBG
would work", not every barbershop in Atlanta. The result must say so, and must report the city mix
(count of leads per city in the scored set).

**Code under test (D4).** `stage-e-done` as is. Stage B (gate fixes) stays skipped; no `src/` gate,
prompt, model or routing change is made for this holdout. The stage F tooling (data folder argument,
logging, eval scripts) does not change pipeline behavior. This is checked at tag time:
`git diff stage-e-done -- src/graph.py src/reconcile.py src/gemini_path.py src/evidence.py src/intake.py`
must be empty. The only allowed later change to `src/intake.py` is Place ID denylist lines from the skim
(section 3), each with a reason, committed BEFORE the sample is drawn.

**Gemini is OFF (D4).** The run is Brave only: `--gemini` is not passed. Gemini for the same leads comes
after Nov 1 as a separate step (section 11) and cannot change the holdout number.

## 2. Terms

- **U**: leads in the clean lead CSV that went into the sealed database (the drawn sample, or the whole
  filtered pull if 45 or fewer qualify).
- **Excluded** (leave every denominator, each reported as its own count):
  - verdict `Ambiguous` or `Not determined` (section 6);
  - export outcome `error` (section 8);
  - export outcome `skipped` (intake skipped it; should be 0 because the validator filters first, and is
    reported if not).
- **S** = U minus excluded. The scored set.
- **O**: rows in S whose verdict is `Owner found` (owner-bearing). **N**: rows in S whose verdict is
  `No owner public` (no-owner shops). S = O + N.
- **named**: a row of S where `scoring.is_blank(owner_first_name, owner_last_name)` is false, taken from
  the export column values. Gemini is off, so the delivered answer is the Brave answer. If an export row
  has outcome `needs_review` it delivered no name and counts as a blank; with Gemini off that outcome
  cannot occur, and if it does it is reported as an anomaly.
- **Grades** are exactly those of `eval/scoring.py` (`judge`): `full`, `partial`, `wrong`, `blank`,
  `correct_blank`. Matching rules are unchanged: first AND last name for `full`; first name only, an
  initial, or a recorded goes-by name for `partial`. Any name on a no-owner shop is `wrong`. Every listed
  owner and co-owner on the worksheet counts as correct.

## 3. Intake and the sample

1. The lead pull is saved from the Drive export to `data/atlanta/intake/atlanta_pull_raw.csv`.
2. The offline validator (`eval/holdout_intake.py`) is run FIRST WITH NO `--sample`. It realigns a
   phantom leading column, dedupes on Place ID, drops rows with missing required fields or a state other
   than GA, applies `intake.skip_reason` (the `@` rule and the Place ID denylist), flags Place ID overlap
   with Charlotte (which stops the validator, D11) and chain-name overlap with Charlotte (kept and flagged,
   D11), and prints an eyeball list for person-as-business names.
3. Marvin skims the full list. Any non-shop record without an `@` gets a line in `PLACE_ID_DENYLIST`
   (`src/intake.py`) with a reason, in its own commit.
4. **The sample is drawn AFTER the skim and any denylist commit, never before.** The validator is run a
   second time with `--sample 45 --seed 20261011` (D2, D13), drawing from the filtered population. If 45
   or fewer leads qualify, all are taken.
5. **Draw procedure (fixed, version-independent).** Sort the filtered population by
   `sha256("20261011:" + place_id)` hex digest ascending and take the first 45. The worksheet row order
   uses `sha256("20261011:order:" + place_id)` ascending over the same set. The seed, counts and the
   Place IDs drawn are written to `sample.txt`. No lead is hand-picked, swapped or re-drawn after the
   draw.
6. Nothing about any lead's answer is known at draw time: the pipeline has not been run on Atlanta data.

## 4. The cold run (D10)

Marvin runs it himself in a standalone PowerShell window, NOT in the Claude desktop app, so owner names
never appear in any tool output that the assistant or its memory tools can read. Console output is
redirected to a file inside `data/atlanta/sealed/` and not opened.

Before the run: Brave balance of $6 or more and Anthropic credit of $5 or more; a Charlotte key check the
same day (`python eval/run_regression.py --limit 3`, about $0.12). No smoke test is run on Atlanta leads
(that would be a second look at the holdout). Expected cost about $0.036 a lead (about $1.62 for 45).

The pipeline writes only into `data/atlanta/sealed/` (database, export, batch stats, skipped file, debug
log). The folder is not opened, previewed or read until the worksheet is complete (section 7). After the
run a seal script records the hashes of the sealed files and a commit SHA in `SEAL.txt` (no names).

## 5. The one reported number (definitions fixed here)

All computed by `eval/score_holdout.py` from the sealed export and the completed worksheet, joined on
`place_id`.

- **Match rate** = named / |S|.
- **Lenient precision** = (full + partial) / named.
- **Strict precision** = full / named.
  Both precisions carry a Wilson 95% interval: for k successes of n, with z = 1.96,
  centre = (p + z^2/(2n)) / (1 + z^2/n), half-width = z * sqrt(p(1-p)/n + z^2/(4n^2)) / (1 + z^2/n),
  where p = k/n.
- **Coverage** (the form CLAUDE.md uses): lenient = (full + partial) / |O|, strict = full / |O|.
- **Wrong names** = count of rows graded `wrong`. Any name on a no-owner shop counts here.
- **Correct blanks** = correct_blank / |N|. The denominator will be small (Charlotte had 3 to 4); the
  result states it.
- **Also printed, not part of the headline:** precision by the pipeline's own confidence (high, medium,
  low); named rows without an evidence URL (expected 0); per-city counts; the counts of every excluded
  class; the same figures computed **without** the leads flagged as chain-name overlaps with Charlotte
  (D11). The headline is the figure WITH them; the without-them figure is reported beside it.

**Headline format (one line):**
`Atlanta holdout, Brave-only, commit <sha>, <date>: named a/S (m%) | lenient L/a = x% [lo-hi] | strict T/a = y% [lo-hi] | wrong w | correct blanks c/N | coverage L/O lenient, T/O strict | errors e, excluded x`

**D6: headline = blind truth only.** The truth is the worksheet exactly as saved at the freeze (section 7).
If hand re-checking after unblinding finds a worksheet mistake, the headline does not change; the
correction appears as a footnote with its reason and its effect on the numbers.

## 6. Verification rules (blind, D5, D7)

The worksheet is generated from the clean lead CSV only, before the run, and cannot contain a pipeline
answer. Rows are in the fixed random order of section 3. Marvin verifies each lead without seeing any
pipeline output.

**Verdicts (one per row, dropdown):**
- `Owner found`: a source names an owner. Fill `owner_first`, `owner_last`, optionally `goes_by`, and
  `co_owner_first` / `co_owner_last` when a source names co-owners. Record the source type and URL.
- `No owner public`: allowed ONLY after every source type below was checked (the worksheet has a
  `checked` column recording this). This is a claim that the owner is not publicly findable.
- `Ambiguous`: sources conflict, or the business cannot be told apart from another of the same name.
  Excluded from every denominator.
- `Not determined`: the 8-minute limit ended without a determination. Excluded from every denominator.
  If more than 20% of rows end `Not determined` or `Ambiguous`, the result says so prominently.

**Time box (D7):** 8 minutes a lead. Verification may be split across sessions; the worksheet is saved
between sessions and its hash is recorded only at the freeze.

**Source types (D5):** LinkedIn (search-result snippet only, never fetch a LinkedIn page), News, BBB,
Booksy, Video, Directory, Own website, Social (IG/FB), GA registry (manual lookup by hand, never
scripted), Other. A row records the type of its main source and, optionally, a second.

**Titles are not ownership.** A stylist or head-barber title, reviews and testimonials are not evidence
of ownership. A booking-platform "owner" is whoever holds the subscription.

## 7. Blinding and the freeze

- The worksheet is built from `atlanta_leads_clean.csv` alone; the generator never opens the database or
  the export.
- `data/atlanta/sealed/` is not opened, previewed, read or summarized by Marvin or by any assistant
  until every row has a verdict and the worksheet is saved. The scoring script refuses to run on an
  incomplete worksheet and refuses if the export's hash does not match `SEAL.txt`.
- **The freeze is the moment `score_holdout.py` first runs on a complete worksheet.** It records the
  worksheet's SHA-256 in the score output at that moment. The worksheet is not edited after that.

## 8. Error policy (D8)

- A lead whose export outcome is `error` (every search failed, a model call raised, the graph crashed, or
  no result recorded) is NOT a blank. It is reported as its own count and excluded from every denominator.
- The only allowed re-run is the WHOLE run, and only if at least 10% of the leads errored for a
  vendor-side reason (timeout, 5xx, rate limit, out of credits). The first run is then discarded openly,
  in writing, with its error counts; a second run is never chosen over the first on its score.
- Out-of-credit looks like every call failing; credits are checked before the run, not after.

## 9. No code changes based on holdout results

**No `src/` change is made on the basis of the holdout until the number is recorded.** "Recorded" means all
three:
1. the score file is saved in `data/atlanta/score/`;
2. the worksheet and seal hashes are in it;
3. the counts (no names) are committed as `eval/HOLDOUT_RESULT.md` and tagged `stage-f-recorded`, and
   pasted into the build plan.

Any `src/` change after that turns Atlanta into a development set; any later Atlanta number is labeled
"post-holdout".

## 10. Amendment rules

This protocol is amended only openly, in writing: an `## Amendments` section appended at the end of this
file, each entry dated, stating what changed and why, and committed. Allowed before the worksheet freeze
(for example to fix an unclear definition); never to alter a definition after the score has been computed.
A change made after scoring is a footnote to the result and does not alter the headline.

## 11. Gemini after Nov 1 (separate step, cannot change the number)

The Gemini run reuses the same clean lead list and the same frozen worksheet, in a different data folder
(`data/atlanta/c2/`). `data/atlanta/sealed/` is never written again and the scorer checks its seal hash
every time. The worksheet is never edited; the second run is scored as additional labeled column sets
beside the recorded holdout line (`holdout_brave` from October stays as recorded; `c2_brave_rerun`,
`c2_gemini`, `c2_combined` are new). The C2 gate compares against the Brave rerun from the same day, not
against the October run. The C2 figures count as holdout figures only if
`git diff stage-f-run..HEAD -- src/` is empty; otherwise they are labeled "post-holdout". The Gemini spend
cap and balance are resolved first.

## 12. Fixed parameters

| Item | Value |
|---|---|
| Sample size | 45 (all, if 45 or fewer qualify) |
| Seed | 20261011 |
| Draw rule | first 45 by `sha256("20261011:" + place_id)` ascending, after the skim |
| Code under test | `stage-e-done` pipeline; tooling tag `stage-f-run` |
| Models and Gemini | unchanged; Gemini off |
| Verification | blind, 8 minutes a lead, verdicts per section 6 |
| Interval | Wilson 95% |

## Amendments

### 2026-10-30: clarification of section 1, "must be empty"

Section 1 says the diff of the five frozen files against `stage-e-done` "must be empty" at tag time, and also says
the only allowed later change to `src/intake.py` is Place ID denylist lines. Read literally, those two sentences
disagree once a denylist line exists. This entry settles it:

- `src/graph.py`, `src/reconcile.py`, `src/gemini_path.py` and `src/evidence.py`: the diff against
  `stage-e-done` must be empty.
- `src/intake.py`: every changed line must be an ADDED line inside the `PLACE_ID_DENYLIST` block, as a
  `"<place id>": "<reason>",` entry. A removed line, a changed line, an added line outside the block, an entry
  without a reason, or an entry repeating an existing Place ID fails.

`eval/frozen_check.py` applies exactly this rule. The seal script runs it and records `frozen_source_check`
(PASS or FAIL) and `denylist_lines_added` (a count) in `SEAL.txt`, and nothing else about it. The stage F gate
runs the same check. The tag `stage-f-run` is placed after the skim and any denylist commit, and before the
sample is drawn and before the cold run.

This is a clarification made on 2026-10-30, before any Atlanta data existed in this repository and before any
lead was pulled. It changes no definition in sections 2 to 12 and cannot change the number.
