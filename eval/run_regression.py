"""Run the pipeline on the hand-verified audit shops and score it.

From the repo root, venv active:
    python eval/run_regression.py --limit 5        # cheap smoke test first
    python eval/run_regression.py                  # the 47 audit shops (~$1.40)
    python eval/run_regression.py --subset all     # all 55, incl. the 8 partial rows
    python eval/run_regression.py --rescore eval/data/results_<stamp>.csv   # free

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
import sys
import time
from datetime import datetime

EVAL = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(EVAL)
sys.path.insert(0, EVAL)
from scoring import judge, summarize, fmt, norm_tokens  # noqa: E402

GT = os.path.join(EVAL, "data", "ground_truth.json")
EST_COST_PER_LEAD = 0.03          # 08-25 run averaged $0.027/lead
BASELINE = ("Baseline, unmodified code, 2026-09-25, 2 runs: lenient 18/42, 17/42 (~41%) | strict 12/42, 13/42 (~30%) | "
            "correct blanks 0/4. Noise ~1-2 correct: a fix must reach ~21/42 lenient to be real.")
FIELDS = ["id", "business_name", "subset", "first", "last", "confidence",
          "verified_confidence", "grade", "truth", "why_this_person", "cost_usd", "seconds", "error"]


def key(name):
    return " ".join(t for t in norm_tokens(name) if not t.isdigit())


def graph_input(lead):
    """What the graph receives for one lead.

    MUST stay identical to the app.invoke call in first_graph.process_leads_batch,
    or this eval tests something production doesn't run.
    """
    return {
        "business_name": lead.business_name,
        "lead_id": lead.id,
        "website": lead.website,
        "address": lead.address,
        "phone": lead.phone,
        "city": lead.city,
        "state": lead.state,
    }


def truth_text(row):
    if row["kind"] == "no_owner":
        return "(no owner public)"
    if row["kind"] == "ambiguous":
        return "(ambiguous)"
    return " / ".join(f"{p['first']} {p['last'] or ''}".strip() for p in row["owners"])


def select(rows, subset, limit):
    picked = [r for r in rows if subset == "all" or r["subset"] == subset]
    return picked[:limit] if limit else picked


def run_pipeline(rows, verbose):
    sys.path.insert(0, ROOT)
    import first_graph                                    # noqa: E402  (loads keys, builds graph)
    if not verbose:
        logging.getLogger("first_graph").setLevel(logging.WARNING)
        logging.getLogger("__main__").setLevel(logging.WARNING)

    leads = {key(l.business_name): l for l in first_graph.LeadDatabase().get_all_leads()}
    found = [(r, leads.get(r["key"])) for r in rows]
    missing = [r["business_name"] for r, l in found if l is None]
    if missing:
        print(f"WARNING: {len(missing)} audit shop(s) not in leads.db, skipped:")
        for m in missing:
            print(f"  - {m}")
    found = [(r, l) for r, l in found if l is not None]

    out = []
    for i, (row, lead) in enumerate(found, 1):
        rec = {"id": row["id"], "business_name": row["business_name"], "subset": row["subset"],
               "truth": truth_text(row), "error": ""}
        t0 = time.time()
        try:
            res = first_graph.app.invoke(graph_input(lead))
            cost = res.get("cost")
            rec.update(first=res.get("decision_maker_first_name", ""),
                       last=res.get("decision_maker_last_name", ""),
                       confidence=res.get("confidence", ""),
                       verified_confidence=res.get("verified_confidence", ""),
                       why_this_person=(res.get("why_this_person") or "")[:300],
                       cost_usd=round(cost.estimated_cost_usd(), 4) if cost else 0.0)
            rec["grade"] = judge(rec["first"], rec["last"], row)
        except Exception as e:                            # the eval must finish even if a lead fails
            rec.update(first="", last="", confidence="", verified_confidence="",
                       why_this_person="", cost_usd=0.0, grade="error", error=str(e)[:300])
        rec["seconds"] = round(time.time() - t0, 1)
        out.append(rec)
        print(f"[{i}/{len(found)}] {rec['business_name'][:40]:40} -> "
              f"{(rec['first'] + ' ' + rec['last']).strip()[:28]:28} ({rec['confidence']}) {rec['grade']}")
    return out


def rescore(path, rows):
    by_id = {r["id"]: r for r in rows}
    out = []
    with open(path, encoding="utf-8") as f:
        for rec in csv.DictReader(f):
            row = by_id.get(int(rec["id"]))
            if row is None:
                continue
            rec["grade"] = "error" if rec.get("error") else judge(rec["first"], rec["last"], row)
            rec["truth"] = truth_text(row)
            out.append(rec)
    return out


def report(recs):
    print("\n" + "=" * 78)
    for subset in ("audit", "partial"):
        grades = [r["grade"] for r in recs if r["subset"] == subset and r["grade"] != "error"]
        if grades:
            print(fmt(summarize(grades), f"{subset:7}"))
    if any(r["subset"] == "audit" for r in recs):
        print(BASELINE)

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
    a = ap.parse_args()

    if not os.path.exists(GT):
        raise SystemExit(f"Missing {GT}. Run: python eval/build_ground_truth.py")
    with open(GT, encoding="utf-8") as f:
        gt = json.load(f)

    if a.rescore:
        report(rescore(a.rescore, gt))
        return

    rows = select(gt, a.subset, a.limit)
    est = len(rows) * EST_COST_PER_LEAD
    print(f"About to run the pipeline on {len(rows)} shop(s). Estimated cost ~${est:.2f} (Anthropic + Brave).")
    if not a.yes and input("Proceed? [y/N] ").strip().lower() != "y":
        raise SystemExit("Cancelled. Nothing was spent.")

    recs = run_pipeline(rows, a.verbose)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(EVAL, "data", f"results_{stamp}.csv")
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(recs)
    report(recs)
    print(f"Saved: {path}")


if __name__ == "__main__":
    main()
