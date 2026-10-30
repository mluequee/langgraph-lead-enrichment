"""Is the pipeline code the cold run uses still the frozen stage-e-done code? (protocol section 1 and the Amendments entry)

    python eval/frozen_check.py            # prints PASS or FAIL, exit code 0 or 1

The rule:
  * src/graph.py, src/reconcile.py, src/gemini_path.py and src/evidence.py must be IDENTICAL to stage-e-done;
  * src/intake.py may differ ONLY by lines ADDED inside the PLACE_ID_DENYLIST block, each a `"<place id>": "<reason>",`
    entry (a comment or blank line inside the block is inert and also allowed). Any removed line, any changed line,
    any added line outside the block, any entry without a reason, any entry that repeats an existing Place ID, or a
    missing/duplicated block FAILS.

Standard library plus git only: no network, no keys, no pipeline import. The comparison is of file TEXT (line
endings normalised), working tree against `git show stage-e-done:<path>`, so an uncommitted edit counts too.
Used by eval/seal_holdout.py (which records PASS/FAIL and the number of added denylist entries in SEAL.txt) and by
the stage F gate script.
"""
import difflib
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

BASE_REF = "stage-e-done"
STRICT_FILES = ["src/graph.py", "src/reconcile.py", "src/gemini_path.py", "src/evidence.py"]
INTAKE_FILE = "src/intake.py"

_START = re.compile(r"^PLACE_ID_DENYLIST\s*=\s*\{\s*$")
_END = re.compile(r"^\}\s*$")
_ENTRY = re.compile(r'^\s+"(?P<key>[^"\n]+)"\s*:\s*"(?P<reason>[^"\n]+)"\s*,?\s*$')
_INERT = re.compile(r"^\s*(#.*)?$")


def _lines(text):
    return text.replace("\r\n", "\n").replace("\r", "\n").split("\n")


def _block(lines):
    """(start, end) line indexes of the PLACE_ID_DENYLIST block (the `PLACE_ID_DENYLIST = {` line and the first `}`
    at column 0 after it), or None if there is not exactly one such block."""
    starts = [i for i, l in enumerate(lines) if _START.match(l)]
    if len(starts) != 1:
        return None
    for j in range(starts[0] + 1, len(lines)):
        if _END.match(lines[j]):
            return starts[0], j
    return None


def check_intake(old_text, new_text):
    """Compare src/intake.py against its frozen text. Returns (ok, added_entries, problems)."""
    old, new = _lines(old_text), _lines(new_text)
    ob, nb = _block(old), _block(new)
    if ob is None:
        return False, 0, ["the frozen intake.py has no single PLACE_ID_DENYLIST block (cannot compare)"]
    if nb is None:
        return False, 0, ["intake.py no longer has exactly one PLACE_ID_DENYLIST block"]
    old_keys = {m.group("key") for l in old[ob[0] + 1:ob[1]] for m in [_ENTRY.match(l)] if m}

    problems, added, new_keys = [], 0, set()
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(a=old, b=new, autojunk=False).get_opcodes():
        if tag == "equal":
            continue
        if tag in ("delete", "replace"):
            problems.append(f"changed or removed line(s) at old line {i1 + 1}: {old[i1:i2][0].strip()[:60]!r}")
            continue
        for j in range(j1, j2):                                   # tag == "insert"
            line = new[j]
            if not (nb[0] < j < nb[1]):
                problems.append(f"added line outside the PLACE_ID_DENYLIST block (new line {j + 1}): {line.strip()[:60]!r}")
                continue
            m = _ENTRY.match(line)
            if m:
                if m.group("key") in old_keys or m.group("key") in new_keys:
                    problems.append(f"added entry repeats an existing Place ID (new line {j + 1})")
                new_keys.add(m.group("key"))
                added += 1
            elif not _INERT.match(line):
                problems.append(f"added line inside the block is not a '\"<place id>\": \"<reason>\",' entry (new line {j + 1}): {line.strip()[:60]!r}")
    return (not problems), added, problems


def check_frozen(old_of, new_of):
    """old_of(path) / new_of(path) return a file's text (or None if unavailable).
    Returns (status, added_entries, problems) with status PASS, FAIL or UNKNOWN."""
    problems = []
    for path in STRICT_FILES + [INTAKE_FILE]:
        if old_of(path) is None or new_of(path) is None:
            return "UNKNOWN", 0, [f"could not read {path} (is git available and the {BASE_REF} tag present?)"]
    for path in STRICT_FILES:
        if _lines(old_of(path)) != _lines(new_of(path)):
            problems.append(f"{path} differs from {BASE_REF} (it must be identical)")
    ok, added, intake_problems = check_intake(old_of(INTAKE_FILE), new_of(INTAKE_FILE))
    problems += [f"{INTAKE_FILE}: {p}" for p in intake_problems]
    return ("PASS" if not problems and ok else "FAIL"), added, problems


def _git_show(path):
    try:
        r = subprocess.run(["git", "-C", ROOT, "show", f"{BASE_REF}:{path}"], capture_output=True, timeout=60)
    except Exception:
        return None
    return r.stdout.decode("utf-8", errors="replace") if r.returncode == 0 else None


def _working(path):
    try:
        with open(os.path.join(ROOT, path), encoding="utf-8", errors="replace", newline="") as f:
            return f.read()
    except OSError:
        return None


def git_frozen_check():
    """The real check: the working tree against stage-e-done."""
    return check_frozen(_git_show, _working)


def main():
    status, added, problems = git_frozen_check()
    print(f"frozen source check: {status}   (added denylist entries: {added})")
    for p in problems:
        print("  - " + p)
    return 0 if status == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
