"""Free checks for the frozen-source check (eval/frozen_check.py): no network, no keys, no pipeline import.

    python eval/test_frozen_check.py

Rule under test (protocol section 1 and its Amendments entry): graph.py, reconcile.py, gemini_path.py and
evidence.py identical to stage-e-done; intake.py differs ONLY by entries ADDED inside PLACE_ID_DENYLIST.
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import frozen_check as fc                        # noqa: E402

failures = []


def check(label, ok):
    print(("PASS " if ok else "FAIL ") + label)
    if not ok:
        failures.append(label)


OLD = '''"""Intake filter (synthetic stand-in for src/intake.py)."""
import csv
import logging

SKIPPED_FILE = "skipped_leads.csv"

# Add a line per record, with the business name and why.
PLACE_ID_DENYLIST = {
    "ChIJold0000000000000000001": "Old Shop: independent barber, not a shop",
}

AT_SIGN_REASON = "business name contains '@'"


def skip_reason(business_name, place_id=""):
    if place_id and place_id in PLACE_ID_DENYLIST:
        return f"Place ID denylist: {PLACE_ID_DENYLIST[place_id]}"
    return None
'''
OLD_ENTRY = '    "ChIJold0000000000000000001": "Old Shop: independent barber, not a shop",\n'
NEW1 = '    "ChIJnew0000000000000000001": "Booth Renter Co: person-as-business, no shop",\n'
NEW2 = '    "ChIJnew0000000000000000002": "Mobile Mike: mobile barber, no shop",\n'


def res(new_text):
    return fc.check_intake(OLD, new_text)


def failed(new_text, needle=None):
    ok, n, problems = res(new_text)
    return (not ok) and (needle is None or any(needle in p for p in problems))


# ---- src/intake.py -------------------------------------------------------------------------------------
ok, n, p = res(OLD)
check("identical intake.py passes with 0 added entries", ok and n == 0 and p == [])
ok, n, p = res(OLD.replace(OLD_ENTRY, OLD_ENTRY + NEW1))
check("ONE entry added at the end of the block passes, count 1", ok and n == 1)
ok, n, p = res(OLD.replace(OLD_ENTRY, NEW2 + OLD_ENTRY + NEW1))
check("two entries added (one before, one after the existing one) pass, count 2", ok and n == 2)
ok, n, p = res(OLD.replace(OLD_ENTRY, OLD_ENTRY.rstrip("\n") + "\n" + NEW1.replace(",\n", "\n")))
check("an added entry without the trailing comma still passes (it is the last entry)", ok and n == 1)
ok, n, p = res(OLD.replace("\n", "\r\n"))
check("Windows line endings alone are not a change", ok and n == 0)
ok, n, p = res(OLD.replace(OLD_ENTRY, OLD_ENTRY + NEW1).replace("\n", "\r\n"))
check("an added entry in a CRLF file passes, count 1", ok and n == 1)
ok, n, p = res(OLD.replace(OLD_ENTRY, "    # Atlanta, added after the skim\n" + OLD_ENTRY + NEW1))
check("an inert comment inside the block is allowed and is not counted as a denylist entry", ok and n == 1)

check("a REMOVED entry fails", failed(OLD.replace(OLD_ENTRY, ""), "changed or removed"))
check("a CHANGED line (an existing entry's reason edited) fails",
      failed(OLD.replace("independent barber, not a shop", "something else"), "changed or removed"))
check("a changed line outside the block (AT_SIGN_REASON edited) fails", failed(OLD.replace("contains '@'", "contains '@' or '#'"), "changed or removed"))
check("an ADDED line outside the block (a new import) fails", failed(OLD.replace("import csv\n", "import csv\nimport os\n"), "outside the PLACE_ID_DENYLIST block"))
check("an added function after the block fails", failed(OLD + "\n\ndef backdoor():\n    return True\n", "outside the PLACE_ID_DENYLIST block"))
check("an added entry-looking line OUTSIDE the block (in AT_SIGN area) fails",
      failed(OLD.replace("AT_SIGN_REASON =", NEW1 + "AT_SIGN_REASON ="), "outside the PLACE_ID_DENYLIST block"))
check("an added line above the block (comment) fails: outside the block", failed(OLD.replace("PLACE_ID_DENYLIST = {", "# new comment\nPLACE_ID_DENYLIST = {"),
                                                                                  "outside the PLACE_ID_DENYLIST block"))
check("an added CODE line inside the block fails", failed(OLD.replace(OLD_ENTRY, OLD_ENTRY + '    __import__("os"),\n'), "not a"))
check("an entry with an empty reason fails", failed(OLD.replace(OLD_ENTRY, OLD_ENTRY + '    "ChIJnew0000000000000000009": "",\n'), "not a"))
check("an entry that repeats an existing Place ID fails (it would silently replace a reason)",
      failed(OLD.replace(OLD_ENTRY, OLD_ENTRY + '    "ChIJold0000000000000000001": "New reason",\n'), "repeats an existing Place ID"))
check("the same new Place ID added twice fails", failed(OLD.replace(OLD_ENTRY, OLD_ENTRY + NEW1 + NEW1), "repeats"))
check("an extra closing brace after the block fails", failed(OLD.replace("}\n\nAT_SIGN", "}\n}\n\nAT_SIGN")))
check("a second PLACE_ID_DENYLIST assignment fails", failed(OLD + "\nPLACE_ID_DENYLIST = {}\n"))
check("a removed denylist block fails", failed(OLD.replace(OLD[OLD.index("PLACE_ID_DENYLIST = {"):OLD.index("AT_SIGN_REASON")], "")))
check("an entry added with a different quoting style (single quotes) fails", failed(OLD.replace(OLD_ENTRY, OLD_ENTRY + "    'ChIJnew0000000000000000003': 'x',\n"), "not a"))

# ---- the whole rule over the five files -------------------------------------------------------------------
FILES = {"src/graph.py": "G\nline\n", "src/reconcile.py": "R\n", "src/gemini_path.py": "P\n", "src/evidence.py": "E\n", "src/intake.py": OLD}


def run(new_overrides=None, old_files=None):
    old = dict(old_files or FILES)
    new = dict(old)
    new.update(new_overrides or {})
    return fc.check_frozen(old.get, new.get)


st, n, p = run()
check("all five files identical: PASS, 0 added", st == "PASS" and n == 0 and not p)
st, n, p = run({"src/intake.py": OLD.replace(OLD_ENTRY, OLD_ENTRY + NEW1 + NEW2)})
check("denylist-only addition in intake.py with the other four identical: PASS, 2 added", st == "PASS" and n == 2)
st, n, p = run({"src/intake.py": OLD.replace(OLD_ENTRY, "")})
check("a removed denylist line: FAIL", st == "FAIL")
st, n, p = run({"src/intake.py": OLD.replace("import csv\n", "import csv\nimport os\n")})
check("an added line outside the denylist: FAIL", st == "FAIL")
for path in ("src/graph.py", "src/reconcile.py", "src/gemini_path.py", "src/evidence.py"):
    st, n, p = run({path: FILES[path] + "# one extra line\n"})
    check(f"a one-line change to {path} FAILS and names the file", st == "FAIL" and any(path in x for x in p))
st, n, p = run({"src/graph.py": "G\nline\n# x\n", "src/intake.py": OLD.replace(OLD_ENTRY, OLD_ENTRY + NEW1)})
check("a change to a frozen file FAILS even when intake.py's addition alone would pass", st == "FAIL")
check("a whitespace-only difference in a frozen file FAILS (it must be identical)", run({"src/evidence.py": "E \n"})[0] == "FAIL")
check("a missing file gives UNKNOWN, never PASS", fc.check_frozen(lambda p_: None, lambda p_: "x")[0] == "UNKNOWN")

# ---- against the real repository ---------------------------------------------------------------------------
real_old = fc._git_show("src/intake.py")
if real_old is None:
    print("SKIP git not available or stage-e-done tag missing: real-repository checks skipped")
else:
    st, n, p = fc.git_frozen_check()
    check(f"real tree vs stage-e-done: PASS with {n} added entries (reasons: {p})", st == "PASS")
    lines = real_old.replace("\r\n", "\n").split("\n")
    end = next(i for i, l in enumerate(lines) if l.startswith("}") and i > next(j for j, x in enumerate(lines) if x.startswith("PLACE_ID_DENYLIST")))
    with_new = "\n".join(lines[:end] + [NEW1.rstrip("\n")] + lines[end:])
    ok, n, p = fc.check_intake(real_old, with_new)
    check("the REAL intake.py with one entry inserted before the closing brace passes, count 1", ok and n == 1)
    ok, n, p = fc.check_intake(real_old, "\n".join(lines[:end + 1] + ["PLACE_ID_DENYLIST.update({})"] + lines[end + 1:]))
    check("the REAL intake.py with a line added right after the block fails", not ok)
    for cmd, label in (([sys.executable, os.path.join(HERE, "frozen_check.py")], "eval/frozen_check.py"),
                       ([sys.executable, os.path.join(HERE, "seal_holdout.py"), "--check-frozen"], "eval/seal_holdout.py --check-frozen")):
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        check(f"{label} exits 0 and prints PASS on the real tree", r.returncode == 0 and "frozen source check: PASS" in r.stdout)

print("\nFrozen-check tests passed." if not failures else f"\n{len(failures)} FAILED: {failures}")
sys.exit(1 if failures else 0)
