"""Self-check: the scorer must reproduce the 2026-09-21 hand audit.

Free to run, no API calls:
    python eval/test_scoring.py

It re-scores the 08-25 LangGraph and n8n outputs (from the Blind Key) with the
automatic name matcher and checks the result equals Marvin's hand grading:
LangGraph 17/41, n8n 18/27. If this fails, the scorer is wrong and no number
from run_regression.py can be trusted until it is fixed.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from scoring import judge, summarize, fmt  # noqa: E402

GT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "ground_truth.json")
EXPECTED = {"LangGraph": (17, 41), "n8n": (18, 27)}   # (correct, named), audit rows


def load():
    if not os.path.exists(GT):
        raise SystemExit(f"Missing {GT}. Run build_ground_truth.py first.")
    with open(GT, encoding="utf-8") as f:
        return json.load(f)


def test_row_level_agreement():
    """Every historical answer: matcher verdict == hand verdict."""
    bad = []
    for row in load():
        for system, h in row["historical"].items():
            if not h or not h["said"] or row["kind"] == "ambiguous":
                continue
            parts = h["said"].split(" ", 1)          # replay in the pipeline's real
            f, l = (parts + ["<UNKNOWN>"])[:2]      # two-field format, sentinel surname
            g = judge(f, l, row)
            auto_right = g in ("full", "partial")
            if auto_right != h["credited"]:
                bad.append(f"  {row['business_name']} | {system} said '{h['said']}' | "
                           f"hand={'right' if h['credited'] else 'wrong'} auto={g}")
    assert not bad, "Scorer disagrees with hand grading:\n" + "\n".join(bad)


def test_reproduces_audit_totals():
    rows = [r for r in load() if r["subset"] == "audit"]
    for system, (want_right, want_named) in EXPECTED.items():
        grades = [judge(r["historical"][system]["said"], "", r)
                  for r in rows if r["historical"][system] is not None]
        s = summarize(grades)
        print(fmt(s, f"08-25 {system:9}"))
        got = (s["correct_lenient"], s["named"])
        assert got == (want_right, want_named), f"{system}: got {got}, expected {(want_right, want_named)}"


def test_matcher_rules():
    t = {"kind": "owner", "owners": [{"first": "Greg", "last": "Melton", "aliases": []}]}
    assert judge("Greg", "Melton", t) == "full"
    assert judge("Greg", "", t) == "partial"          # first name only
    assert judge("Greg", "Smith", t) == "wrong"       # right first, wrong surname
    assert judge("G", "Melton", t) == "wrong"         # initial first names are not matched
    assert judge("<UNKNOWN>", "<UNKNOWN>", t) == "blank"   # pipeline's real no-result output
    assert judge("Unknown", "Unknown", t) == "blank"       # extract_owner prompt's no-result output
    assert judge("", "", t) == "blank"
    assert judge("Greg", "<UNKNOWN>", t) == "partial"      # first name found, surname sentinel
    assert judge("Greg", "Unknown", t) == "partial"
    w = {"kind": "owner", "owners": [{"first": "Wize", "last": None, "aliases": []}]}
    assert judge("Wize", "<UNKNOWN>", w) == "full"         # truth has no surname
    n = {"kind": "no_owner", "owners": []}
    assert judge("Unknown", "Unknown", n) == "correct_blank"
    assert judge("Jen", "", n) == "wrong"             # a name on a no-owner shop is invented
    a = {"kind": "ambiguous", "owners": []}
    assert judge("Olivia", "Duran", a) == "excluded"
    f = {"kind": "owner", "owners": [{"first": "Niki", "last": "Vancannon", "aliases": []}]}
    assert judge("Niki", "Voncannon", f) == "full"    # spelling slip tolerated
    s = {"kind": "owner", "owners": [{"first": "Jim", "last": "Eggers", "aliases": []}]}
    assert judge("Andre", "Stanley", s) == "wrong"    # the surname-echo fabrication


if __name__ == "__main__":
    failed = 0
    for fn in (test_matcher_rules, test_row_level_agreement, test_reproduces_audit_totals):
        try:
            fn()
            print(f"PASS  {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL  {fn.__name__}\n{e}")
    print("\nScorer trusted." if not failed else f"\n{failed} check(s) failed. Do not trust scores.")
    sys.exit(1 if failed else 0)
