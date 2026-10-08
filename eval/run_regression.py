"""Run the pipeline on the hand-verified audit shops and score it.

From the repo root, venv active:
    python eval/run_regression.py --limit 5        # cheap smoke test first
    python eval/run_regression.py                  # the 47 audit shops (~$1.40)
    python eval/run_regression.py --subset all     # all 55, incl. the 8 partial rows
    python eval/run_regression.py --rescore eval/data/results_<stamp>.csv   # free

Rows the pipeline's intake filter skips (intake.py: '@' in the name, Place ID
denylist) are dropped from the run and from every denominator. The ground-truth
worksheet and ground_truth.json are not edited.

Always prints a cost estimate and asks before spending, unless --yes.
Results CSV lands in eval/data/ (gitignored: it holds real people's names).

These 55 shops are the DEVELOPMENT set. Numbers here show whether a change
helped. They are NOT listing numbers; those need a fresh holdout (CLAUDE.md).
"""
import argparse
import csv
import json
import logging
import os
import sqlite3
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

EVAL = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(EVAL)
sys.path.insert(0, EVAL)
sys.path.insert(0, os.path.join(ROOT, "src"))
from scoring import judge, summarize, fmt, norm_tokens  # noqa: E402
import intake                                             # noqa: E402
import c2_report                                          # noqa: E402

GT = os.path.join(EVAL, "data", "ground_truth.json")
DB = os.path.join(ROOT, "leads.db")
EST_COST_PER_LEAD = 0.04          # Brave + Anthropic ~$0.036 per lead; Gemini is extra and opt-in (see gemini_path.COST_NOTE)
DEFAULT_REFS = ["eval/data/results_20261007_085218.csv", "eval/data/results_20261005_080444.csv"]
BASELINE = ("PRE-A0 baseline (unmodified code, 2026-09-25, 2 runs, 47 audit rows incl. the 3 now excluded by intake): "
            "lenient 18/42, 17/42 | strict 12/42, 13/42 | correct blanks 0/4. History only: the filtered set has its own "
            "baseline (stage A0). Noise ~1-2 correct.")
FIELDS = ["id", "business_name", "subset", "first", "last", "confidence",
          "verified_confidence", "grade", "truth", "why_this_person", "cost_usd", "seconds", "error",
          # stage A: evidence and status (record-keeping; the grade never reads source_tier)
          "goes_by_name", "delivered_confidence", "source_tier", "evidence_url", "evidence_url_kind",
          "evidence_domain", "evidence_source", "other_evidence_urls", "status",
          # stage C2: each path's own answer from the same run, and what reconcile did
          "reconcile_case", "review_candidates", "brave_first", "brave_last", "brave_grade",
          "gemini_first", "gemini_last", "gemini_grade", "gemini_status", "gemini_error", "gemini_seconds",
          "gemini_cost_usd", "gemini_attempts", "gemini_searches",
          # recording only: Brave searches, 429s, retries that recovered, searches that errored
          "brave_calls", "brave_429", "brave_429_recovered", "brave_search_errors"]


def key(name):
    return " ".join(t for t in norm_tokens(name) if not t.isdigit())


def excluded_rows(rows):
    """Ground-truth rows the intake filter would skip: {row id: reason}.

    Uses the same rule as the pipeline (intake.skip_reason), applied to each row's
    business name and to the Place ID of the matching lead in leads.db. The
    ground-truth worksheet itself is never edited; excluded rows just drop out of
    every denominator.
    """
    place_ids = {}
    if os.path.exists(DB):
        conn = sqlite3.connect(DB)
        for pid, name in conn.execute("SELECT place_id, business_name FROM leads"):
            place_ids.setdefault(key(name), []).append(pid or "")
        conn.close()
    else:
        print(f"WARNING: {DB} not found. Applying the '@' rule only; the Place ID denylist can't be checked.")

    out = {}
    for r in rows:
        reason = intake.skip_reason(r["business_name"])
        for pid in place_ids.get(r["key"], []):
            reason = reason or intake.skip_reason(r["business_name"], pid)
        if reason:
            out[r["id"]] = reason
    return out


def print_excluded(rows, excluded):
    if not excluded:
        return
    print(f"Intake rule excludes {len(excluded)} ground-truth row(s) from every denominator:")
    for r in rows:
        if r["id"] in excluded:
            print(f"  - {r['business_name']} ({r['subset']}, {r['kind']}): {excluded[r['id']]}")


def truth_text(row):
    if row["kind"] == "no_owner":
        return "(no owner public)"
    if row["kind"] == "ambiguous":
        return "(ambiguous)"
    return " / ".join(f"{p['first']} {p['last'] or ''}".strip() for p in row["owners"])


def select(rows, subset, limit):
    picked = [r for r in rows if subset == "all" or r["subset"] == subset]
    return picked[:limit] if limit else picked


def run_pipeline(rows, verbose, workers=4, use_gemini=True):
    sys.path.insert(0, os.path.join(ROOT, "src"))
    import graph                                    # noqa: E402  (loads keys, builds graph)
    import run as run_mod                           # noqa: E402  (lead_to_input: the one place the graph input is built)
    graph.GEMINI_ENABLED = use_gemini
    graph.gemini_path.allow_gemini(use_gemini)   # real calls only when the caller passed --gemini and confirmed
    graph.gemini_path.reset()      # a 402 disables Gemini for this run only
    if not verbose:
        logging.getLogger("graph").setLevel(logging.WARNING)
        logging.getLogger("__main__").setLevel(logging.WARNING)

    leads = {key(l.business_name): l for l in graph.LeadDatabase().get_all_leads()}
    found = [(r, leads.get(r["key"])) for r in rows]
    missing = [r["business_name"] for r, l in found if l is None]
    if missing:
        print(f"WARNING: {len(missing)} audit shop(s) not in leads.db, skipped:")
        for m in missing:
            print(f"  - {m}")
    found = [(r, l) for r, l in found if l is not None]

    lock, done = threading.Lock(), [0]

    def one(item):
        row, lead = item
        rec = {"id": row["id"], "business_name": row["business_name"], "subset": row["subset"],
               "truth": truth_text(row), "error": ""}
        t0 = time.time()
        try:
            res = graph.app.invoke(run_mod.lead_to_input(lead))
            cost = res.get("cost")
            rec.update(first=res.get("decision_maker_first_name") or "",    # None (nothing found) -> empty cell
                       last=res.get("decision_maker_last_name") or "",
                       confidence=res.get("confidence", ""),
                       verified_confidence=res.get("verified_confidence", ""),
                       why_this_person=(res.get("why_this_person") or "")[:300],
                       cost_usd=round((cost.estimated_cost_usd() if cost else 0.0)
                                      + ((res.get("gemini") or {}).get("cost_usd") or 0.0), 4),
                       status=res.get("status") or "ok")
            b, g = res.get("brave_answer") or {}, res.get("gemini") or {}
            rec.update(reconcile_case=res.get("reconcile_case") or "", review_candidates=res.get("review_candidates") or "",
                       brave_first=b.get("first") or "", brave_last=b.get("last") or "",
                       gemini_first=g.get("first") or "", gemini_last=g.get("last") or "",
                       gemini_status=g.get("status") or "", gemini_error=(g.get("error") or "")[:200],
                       gemini_seconds=g.get("seconds") or 0, gemini_cost_usd=g.get("cost_usd") or 0,
                       gemini_attempts=g.get("attempts") or 0, gemini_searches=g.get("searches") or 0,
                       brave_calls=cost.brave_calls if cost else 0, brave_429=cost.brave_429 if cost else 0,
                       brave_429_recovered=cost.brave_429_recovered if cost else 0,
                       brave_search_errors=len(cost.brave_errors) if cost else 0)
            rec["brave_grade"] = judge(rec["brave_first"], rec["brave_last"], row)
            rec["gemini_grade"] = judge(rec["gemini_first"], rec["gemini_last"], row) if g.get("status") == "ok" else (g.get("status") or "error")
            for k in ("goes_by_name", "delivered_confidence", "source_tier", "evidence_url", "evidence_url_kind",
                      "evidence_domain", "evidence_source", "other_evidence_urls"):
                rec[k] = res.get(k) or ""
            if rec["status"] == "error":                  # a failed lead is an error, never a blank
                rec.update(grade="error", error=(res.get("error_detail") or "status error")[:300])
            else:
                rec["grade"] = judge(rec["first"], rec["last"], row)
        except Exception as e:                            # the eval must finish even if a lead fails
            rec.update(first="", last="", confidence="", verified_confidence="",
                       why_this_person="", cost_usd=0.0, grade="error", error=str(e)[:300], status="error",
                       brave_grade="error", gemini_grade="error")
        rec["seconds"] = round(time.time() - t0, 1)
        with lock:
            done[0] += 1
            print(f"[{done[0]}/{len(found)}] {rec['business_name'][:40]:40} -> "
                  f"{(rec['first'] + ' ' + rec['last']).strip()[:28]:28} ({rec['confidence']}) {rec['grade']}"
                  f" [{rec.get('reconcile_case') or '-'}] {rec['seconds']}s", flush=True)
        return rec

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:       # leads in parallel, output in row order
        return list(pool.map(one, found))


def rescore(path, rows, excluded):
    by_id = {r["id"]: r for r in rows}
    out = []
    with open(path, encoding="utf-8") as f:
        for rec in csv.DictReader(f):
            row = by_id.get(int(rec["id"]))
            if row is None:
                continue
            if row["id"] in excluded:
                rec["grade"] = "excluded"                 # skipped at intake: not in any denominator
            else:
                rec["grade"] = "error" if rec.get("error") else judge(rec["first"], rec["last"], row)
            rec["truth"] = truth_text(row)
            if "brave_first" in rec and row["id"] not in excluded:       # stage C2 CSVs: regrade each path too
                rec["brave_grade"] = judge(rec["brave_first"], rec["brave_last"], row)
                rec["gemini_grade"] = (judge(rec["gemini_first"], rec["gemini_last"], row)
                                       if rec.get("gemini_status") == "ok" else (rec.get("gemini_status") or "error"))
            out.append(rec)
    return out


GT_ROWS = []          # filled by main() so report() can grade review candidates


def report(recs):
    print("\n" + "=" * 78)
    for subset in ("audit", "partial"):
        grades = [r["grade"] for r in recs if r["subset"] == subset and r["grade"] != "error"]
        if grades:
            print(fmt(summarize(grades), f"{subset:7}"))
    if any(r["subset"] == "audit" for r in recs):
        print(BASELINE)

    if recs and "evidence_url" in recs[0]:            # stage A gate: every named lead carries an evidence URL
        named = [r for r in recs if r["grade"] in ("full", "partial", "wrong")]
        missing = [r for r in named if not r.get("evidence_url")]
        print(f"Named rows without an evidence URL: {len(missing)} of {len(named)}"
              + ("" if not missing else " -> " + ", ".join(r["business_name"] for r in missing)))

    c2_report.report(recs, {r["id"]: r for r in GT_ROWS})

    errors = [r for r in recs if r["grade"] == "error"]
    if errors:
        print(f"\nERRORS: {len(errors)} lead(s) failed and are NOT scored. Fix before trusting the numbers.")
        for r in errors:
            print(f"  {r['business_name']}: {r['error'][:120]}")

    print("\nPrecision by the pipeline's own confidence (lenient):")
    for c in ("high", "medium", "low"):
        g = [r["grade"] for r in recs if r.get("confidence") == c and r["grade"] in ("full", "partial", "wrong")]
        if g:
            right = sum(x in ("full", "partial") for x in g)
            print(f"  {c:6} {right}/{len(g)} = {right / len(g):.0%}")

    wrong = [r for r in recs if r["grade"] == "wrong"]
    if wrong:
        print(f"\nWrong names ({len(wrong)}):")
        for r in wrong:
            said = f"{r['first']} {r['last']}".strip()
            print(f"  {r['business_name'][:38]:38} said {said[:24]:24} ({r.get('confidence', '')}) | truth {r['truth']}")

    total = sum(float(r.get("cost_usd") or 0) for r in recs)
    print(f"\nCost this run: ${total:.2f}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--subset", choices=["audit", "partial", "all"], default="audit")
    ap.add_argument("--limit", type=int, default=0, help="only the first N shops (smoke test)")
    ap.add_argument("--rescore", metavar="CSV", help="re-grade a saved results CSV, no API calls")
    ap.add_argument("--yes", action="store_true", help="skip the cost confirmation")
    ap.add_argument("--verbose", action="store_true", help="show the pipeline's own logging")
    ap.add_argument("--workers", type=int, default=4, help="leads processed at the same time (default 4)")
    ap.add_argument("--gemini", action="store_true",
                    help="OPT IN to the Gemini path (costs money; ~$0.45/lead observed Oct 7, cause unconfirmed). Default: off")
    ap.add_argument("--no-gemini", action="store_true", help="accepted for compatibility; Gemini is off unless --gemini is given")
    ap.add_argument("--ref", nargs="*", default=None, metavar="CSV",
                    help="earlier results CSVs to list row-by-row differences against (default: stage A run 2, A0 run 2)")
    a = ap.parse_args()

    if not os.path.exists(GT):
        raise SystemExit(f"Missing {GT}. Run: python eval/build_ground_truth.py")
    with open(GT, encoding="utf-8") as f:
        gt = json.load(f)

    excluded = excluded_rows(gt)
    print_excluded(gt, excluded)
    GT_ROWS[:] = gt

    def print_diffs(recs):
        for ref in (a.ref if a.ref is not None else DEFAULT_REFS):
            if os.path.exists(os.path.join(ROOT, ref) if not os.path.isabs(ref) else ref):
                p = ref if os.path.isabs(ref) else os.path.join(ROOT, ref)
                c2_report.diff_vs(recs, rescore(p, gt, excluded), os.path.basename(p))

    if a.rescore:
        recs = rescore(a.rescore, gt, excluded)
        report(recs)
        print_diffs(recs)
        return

    rows = [r for r in select(gt, a.subset, 0) if r["id"] not in excluded]
    if a.limit:
        rows = rows[:a.limit]
    use_gemini = a.gemini and not a.no_gemini
    est = len(rows) * EST_COST_PER_LEAD
    print(f"About to run the pipeline on {len(rows)} shop(s), {a.workers} at a time, Gemini "
          f"{'ON' if use_gemini else 'off'}. Estimated cost ~${est:.2f} for Anthropic + Brave (Gemini not included).")
    if not a.yes and input("Proceed? [y/N] ").strip().lower() != "y":
        raise SystemExit("Cancelled. Nothing was spent.")
    if use_gemini:
        import gemini_path                                # noqa: E402
        if not gemini_path.confirm_opt_in(len(rows), a.yes):
            raise SystemExit("Cancelled. Nothing was spent.")

    run_start = time.time()
    recs = run_pipeline(rows, a.verbose, a.workers, use_gemini)
    wall = time.time() - run_start
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(EVAL, "data", f"results_{stamp}.csv")
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(recs)
    report(recs)
    print(f"Wall time: {wall:.0f}s ({wall / 60:.1f} min), {a.workers} workers")
    print_diffs(recs)
    print(f"Saved: {path}")


if __name__ == "__main__":
    main()
