"""Seal a finished cold run (stage F, protocol sections 4 and 7). Prints COUNTS ONLY: no business name, no owner name.

    python eval/seal_holdout.py data/atlanta/sealed

Run it right after the cold run, from the same PowerShell window. It writes SEAL.txt in the sealed folder:
the SHA-256 of every output file, the git commit and whether the frozen pipeline files still equal stage-e-done,
and the outcome counts. The scorer (eval/score_holdout.py) refuses to run if the export no longer matches
SEAL.txt, so the answers cannot change between the run and the scoring.

It never prints, copies or summarizes a name. It refuses to run twice on the same folder (a second seal could
hide a change made after a peek). The folder must be under <repo>/data/.
"""
import argparse
import csv
import datetime
import os
import sqlite3
import subprocess
import sys
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))
import holdout_common as hc                       # noqa: E402
import paths                                      # noqa: E402

SEALED_FILES = ["leads.db", "enriched_leads.csv", "batch_stats.csv", "skipped_leads.csv", "langgraph_debug.log", "console.txt"]
FROZEN_SRC = ["src/graph.py", "src/reconcile.py", "src/gemini_path.py", "src/evidence.py", "src/intake.py"]


def _git(*args):
    try:
        r = subprocess.run(["git", "-C", ROOT, *args], capture_output=True, text=True, timeout=60)
    except Exception:
        return None
    return r.stdout if r.returncode == 0 else None


def git_facts():
    head = (_git("rev-parse", "HEAD") or "unknown").strip()
    diff = _git("diff", "stage-e-done", "--", *FROZEN_SRC)
    dirty = _git("status", "--porcelain", "--", "src")
    return {"git_commit": head,
            "frozen_src_diff_lines": str(len(diff.splitlines())) if diff is not None else "unknown",
            "src_uncommitted_changes": ("yes" if dirty.strip() else "no") if dirty is not None else "unknown"}


def count_outcomes(export_csv):
    with open(export_csv, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    c = Counter(r["outcome"] for r in rows)
    found_no_ev = sum(1 for r in rows if r["outcome"] == "found" and not (r.get("evidence_url") or "").strip())
    return len(rows), c, found_no_ev


def gemini_status_counts(db_path):
    """Counts of results.gemini_status (the Brave-only run should say off everywhere). Read-only; counts only."""
    uri = "file:" + os.path.abspath(db_path).replace("\\", "/") + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        return Counter(r[0] or "none" for r in conn.execute("SELECT gemini_status FROM results"))
    finally:
        conn.close()


def seal(folder):
    """Write SEAL.txt. Returns (lines_printed). Raises ValueError for a refusal."""
    folder = paths.resolve_data_dir(folder)
    seal_path = os.path.join(folder, "SEAL.txt")
    if os.path.exists(seal_path):
        raise ValueError(f"{seal_path} already exists. A folder is sealed once.")
    export = os.path.join(folder, "enriched_leads.csv")
    db = os.path.join(folder, "leads.db")
    for need in (export, db):
        if not os.path.exists(need):
            raise ValueError(f"{need} is missing. Run the pipeline first (and check the run finished).")

    total, outcomes, found_no_ev = count_outcomes(export)
    gem = gemini_status_counts(db)
    facts = git_facts()
    lines = [f"sealed_at: {datetime.datetime.now().isoformat(timespec='seconds')}"]
    lines += [f"{k}: {v}" for k, v in facts.items()]
    for name in SEALED_FILES:
        p = os.path.join(folder, name)
        lines.append(f"sha256 {name}: {hc.file_sha256(p) if os.path.exists(p) else 'absent'}")
    lines.append(f"leads: {total}")
    for o in ("found", "needs_review", "no_owner_found", "error", "skipped"):
        lines.append(f"outcome {o}: {outcomes.get(o, 0)}")
    lines.append(f"found_without_evidence_url: {found_no_ev}")
    lines.append("gemini_status: " + ", ".join(f"{k} {v}" for k, v in sorted(gem.items())))
    with open(seal_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    printed = [f"Sealed {folder}",
               f"  leads {total}   errors {outcomes.get('error', 0)}   found without an evidence URL {found_no_ev}"]
    if set(gem) - {"off"}:
        printed.append("  WARNING: some results are not gemini_status=off; this was supposed to be a Brave-only run")
    if facts["frozen_src_diff_lines"] not in ("0",):
        printed.append(f"  WARNING: the frozen pipeline files differ from stage-e-done ({facts['frozen_src_diff_lines']} diff lines)")
    if facts["src_uncommitted_changes"] != "no":
        printed.append(f"  WARNING: src/ has uncommitted changes ({facts['src_uncommitted_changes']})")
    printed.append("  SEAL.txt written (hashes and counts only). Do not open this folder until the worksheet is complete.")
    return printed


def read_seal(folder):
    """SEAL.txt as a dict of key -> value strings."""
    out = {}
    with open(os.path.join(folder, "SEAL.txt"), encoding="utf-8") as f:
        for line in f:
            if ": " in line:
                k, v = line.rstrip("\n").split(": ", 1)
                out[k] = v
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("sealed_dir", help="the data folder the cold run wrote to (under <repo>/data/)")
    a = ap.parse_args(argv)
    try:
        for line in seal(a.sealed_dir):
            print(line)
    except (ValueError, OSError, sqlite3.Error, KeyError) as e:
        print(f"STOP: {e}")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
