"""Stage E: the n8n comparison was moved out of the product into eval/compare_n8n.py. Free, offline, no network.
Checks that the moved class behaves exactly like the pre-stage-e one (known bugs included) on a grid of inputs, and that
the new database reader and report work on a throwaway database."""
import ast
import csv
import itertools
import logging
import os
import sqlite3
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
failures = []


def check(label, ok):
    print(("PASS " if ok else "FAIL ") + label)
    if not ok:
        failures.append(label)


import compare_n8n as cn                                       # noqa: E402

names = ["", "Ann", None, "<UNKNOWN>", "UNKNOWN", "NA", "N/A"]
lasts = ["", "Lee", "Bo", None]
confs = ["low", "medium", "high", ""]
inputs = [(f1, l1, c1, f2, l2, c2) for f1, l1, c1, f2, l2, c2 in itertools.product(
    ["", "Ann", None, "<UNKNOWN>"], ["", "Lee", None], confs, names, lasts, confs)]

old_src = None
try:
    r = subprocess.run(["git", "show", "pre-stage-e:first_graph.py"], cwd=ROOT, capture_output=True)
    old_src = r.stdout.decode("utf-8") if r.returncode == 0 else None
except OSError:
    pass
if old_src:
    node = [n for n in ast.parse(old_src).body if isinstance(n, ast.ClassDef) and n.name == "AccuracyComparison"][0]
    ns = {"logger": logging.getLogger("old")}
    exec(compile(ast.Module([node], []), "old_first_graph", "exec"), ns)
    Old = ns["AccuracyComparison"]
    def outcome(cls, f1, l1, c1, f2, l2, c2):
        try:
            return cls.compare({"decision_maker_first_name": f1, "decision_maker_last_name": l1, "confidence": c1},
                               {"first": f2, "last": l2, "confidence": c2})
        except Exception as e:                 # a None name on the n8n side raises in the old code too (known bug)
            return ("raised", type(e).__name__)
    same = all(outcome(cn.AccuracyComparison, *i) == outcome(Old, *i) for i in inputs)
    check(f"compare(): identical to the pre-stage-e class on {len(inputs)} input combinations", same)
else:
    print("SKIP old-vs-new grid: the pre-stage-e tag is not available")
c = cn.AccuracyComparison.compare
check("known bug kept as-is: an n8n blank written as '<UNKNOWN>' against a blank result scores exact",
      c({"decision_maker_first_name": "<UNKNOWN>", "decision_maker_last_name": "", "confidence": "low"},
        {"first": "<UNKNOWN>", "last": "", "confidence": "low"})["match"] == "exact")
check("known bug kept as-is: 'UNKNOWN' (no brackets) is not normalised and scores a miss against a blank",
      c({"decision_maker_first_name": "", "decision_maker_last_name": "", "confidence": "low"},
        {"first": "UNKNOWN", "last": "", "confidence": "low"})["match"] == "miss")

with tempfile.TemporaryDirectory() as d:
    db = os.path.join(d, "t.db")
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE leads (id INTEGER PRIMARY KEY, business_name TEXT);
        CREATE TABLE results (id INTEGER PRIMARY KEY AUTOINCREMENT, lead_id INTEGER, decision_maker_first_name TEXT,
                              decision_maker_last_name TEXT, confidence TEXT, status TEXT);
        INSERT INTO leads VALUES (1, 'A Cuts'), (2, 'B Cuts'), (3, 'C Cuts'), (4, 'D Cuts');
        INSERT INTO results (lead_id, decision_maker_first_name, decision_maker_last_name, confidence, status) VALUES
          (1, 'Old', 'Name', 'low', 'ok'), (1, 'Ann', 'Lee', 'high', 'ok'), (2, NULL, NULL, NULL, 'ok'),
          (3, NULL, NULL, NULL, 'error');""")
    conn.commit()
    conn.close()
    before = os.path.getmtime(db)
    res = cn.load_latest_results(db)
    check("load_latest_results: the newest result per lead; a lead without a result is left out",
          [(r["business_name"], r["decision_maker_first_name"]) for r in res] == [("A Cuts", "Ann"), ("B Cuts", None), ("C Cuts", None)])
    check("the database is opened read-only and untouched", os.path.getmtime(db) == before)
    csv_path = os.path.join(d, "n8n.csv")
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["business_name", "decision_maker_first_name", "decision_maker_last_name", "identity_confidence"])
        w.writerow(["A Cuts", "Ann", "Lee", "high"])
        w.writerow(["B Cuts", "", "", ""])
    n8n = cn.AccuracyComparison.load_n8n_extractions(csv_path)
    rows, m = cn.compare_all(res, n8n)
    check("compare_all: one exact match, one both_unknown, one LangGraph error (status=error)",
          (m["exact"], m["both_unknown"], m["langgraph_errors"]) == (1, 1, 1) and rows[2]["match"] == "error")
    check("main() runs end to end on the throwaway files and writes the comparison csv",
          subprocess.run([sys.executable, os.path.join(HERE, "compare_n8n.py"), "--db", db, "--n8n-csv", csv_path, "--out-dir", d],
                         capture_output=True, text=True).returncode == 0
          and any(f.startswith("phase7_accuracy_comparison_") for f in os.listdir(d)))
src_run = open(os.path.join(ROOT, "src", "run.py"), encoding="utf-8").read()
check("the product no longer mentions the n8n comparison (src/run.py: no AccuracyComparison, no n8n CSV argument)",
      "AccuracyComparison" not in src_run and "csv_path" not in src_run and "phase7_accuracy" not in src_run)

print("\nn8n comparison tests passed." if not failures else f"\n{len(failures)} FAILED: {failures}")
sys.exit(1 if failures else 0)
