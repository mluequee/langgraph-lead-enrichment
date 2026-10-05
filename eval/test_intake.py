"""Self-check for the intake filter and the scorer's exclusions. Free, no API calls:
    python eval/test_intake.py

Checks the skip rule itself, then that the regression scorer drops exactly the
skipped rows from its denominators (reads leads.db and ground_truth.json).
"""
import json
import os
import sys

EVAL = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, EVAL)
sys.path.insert(0, os.path.dirname(EVAL))
import intake                                             # noqa: E402
from run_regression import excluded_rows, GT              # noqa: E402

KONS_ID = "ChIJn0eh8yihVogR7mNmS1xrqTU"
failures = []


def check(label, ok):
    print(("PASS " if ok else "FAIL ") + label)
    if not ok:
        failures.append(label)


# The rule
check("'@' in name is skipped", intake.skip_reason("Jody hawk @ Landrys Barbershop") is not None)
check("'@' rule also drops Legendary Cuts @ University (accepted cost)",
      intake.skip_reason("Legendary Cuts @ University") is not None)
check("denylisted Place ID is skipped", intake.skip_reason("Barber Kons the Butcher", KONS_ID) is not None)
check("denylist works even if the name has no '@'", intake.skip_reason("anything", KONS_ID) is not None)
check("plain shop is kept", intake.skip_reason("Golden Roots", "ChIJsomething") is None)
check("Emerson Joseph is kept (confirmed real shop)", intake.skip_reason("Emerson Joseph") is None)
check("Roosters Ballantyne is kept (confirmed real shop)",
      intake.skip_reason("Roosters Men's Grooming Center - Ballantyne") is None)
check("Sarah @ Roosters MGC is skipped", intake.skip_reason("Sarah @ Roosters MGC") is not None)

# The scorer's exclusions against the real ground truth
with open(GT, encoding="utf-8") as f:
    gt = json.load(f)
ex = excluded_rows(gt)
names = sorted(r["business_name"] for r in gt if r["id"] in ex)
print("excluded rows:", names)
check("scorer excludes exactly 3 of 55 rows", len(ex) == 3)
check("Kons, Jody Hawk and Legendary Cuts are the excluded rows",
      names == ["Barber Kons the Butcher", "Jody hawk @ Landrys Barbershop", "Legendary Cuts @ University"])
for keep in ("Emerson Joseph", "Roosters Men's Grooming Center - Ballantyne"):
    check(f"{keep} stays in the scored set",
          all(r["id"] not in ex for r in gt if r["business_name"] == keep) and any(r["business_name"] == keep for r in gt))

print("\nIntake tests passed." if not failures else f"\n{len(failures)} FAILED: {failures}")
sys.exit(1 if failures else 0)
