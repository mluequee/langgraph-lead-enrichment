"""Stage E layout checks, free (no network, dummy keys): src/ never imports eval/, the graph input is built in one place,
data paths resolve to the repo root, and the pieces graph.py re-exports are the same objects as in db.py / cost.py."""
import ast
import glob
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SRC = os.path.join(ROOT, "src")
sys.path.insert(0, SRC)
for k in ("ANTHROPIC_API_KEY", "BRAVE_API_KEY", "GEMINI_API_KEY"):
    os.environ[k] = "test-key-not-real"        # load_dotenv never overrides these, so real keys are never used

failures = []


def check(label, ok):
    print(("PASS " if ok else "FAIL ") + label)
    if not ok:
        failures.append(label)


EVAL_MODULES = {os.path.splitext(os.path.basename(p))[0] for p in glob.glob(os.path.join(HERE, "*.py"))}
bad = []
for path in glob.glob(os.path.join(SRC, "*.py")):
    for n in ast.walk(ast.parse(open(path, encoding="utf-8").read())):
        names = [a.name.split(".")[0] for a in n.names] if isinstance(n, ast.Import) else \
                [(n.module or "").split(".")[0]] if isinstance(n, ast.ImportFrom) else []
        bad += [(os.path.basename(path), x) for x in names if x in EVAL_MODULES]
check(f"src/ never imports an eval/ module (checked {len(glob.glob(os.path.join(SRC, '*.py')))} files; found {bad})", not bad)

import paths                                                     # noqa: E402
check("paths: ROOT is the repo root and the data files sit directly in it",
      paths.ROOT == ROOT and paths.DB_PATH == os.path.join(ROOT, "leads.db")
      and paths.EXPORT_PATH == os.path.join(ROOT, "enriched_leads.csv") and paths.BATCH_STATS_PATH == os.path.join(ROOT, "batch_stats.csv"))

import graph                                                     # noqa: E402
import db                                                        # noqa: E402
import cost                                                      # noqa: E402
import run                                                       # noqa: E402
import export                                                    # noqa: E402
check("graph.py re-exports the very same LeadCost / LeadDatabase / Lead objects", graph.LeadCost is cost.LeadCost
      and graph.LeadDatabase is db.LeadDatabase and graph.Lead is db.Lead)
check("LeadDatabase() default path and export defaults resolve to <repo root>/leads.db",
      db.LeadDatabase.__init__.__defaults__[0] == paths.DB_PATH == export.DEFAULT_DB == graph.DB_PATH)   # read, never opened
lead = db.Lead(id=7, business_name="Zephyr Cuts", website="w", address="a", phone="p", city="c", state="s", place_id="pid")
check("lead_to_input: exactly the seven fields the graph takes (place_id is not one of them)",
      run.lead_to_input(lead) == {"business_name": "Zephyr Cuts", "lead_id": 7, "website": "w", "address": "a",
                                  "phone": "p", "city": "c", "state": "s"})
src_run = open(os.path.join(SRC, "run.py"), encoding="utf-8").read()
src_reg = open(os.path.join(HERE, "run_regression.py"), encoding="utf-8").read()
check("production (run._invoke_lead) and the harness both call lead_to_input; neither builds the input itself",
      "app.invoke(lead_to_input(lead))" in src_run and "run_mod.lead_to_input(lead)" in src_reg and '"lead_id": lead.id' not in src_reg)
check("Gemini stays off by default in the graph", graph.GEMINI_ENABLED is False and not graph.gemini_path.is_allowed())

print("\nLayout tests passed." if not failures else f"\n{len(failures)} FAILED: {failures}")
sys.exit(1 if failures else 0)
