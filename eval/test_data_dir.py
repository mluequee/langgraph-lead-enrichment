"""Free checks for the stage F data folder (--data-dir): no network, dummy keys, synthetic leads, a fake graph.

    python eval/test_data_dir.py

Checks that
  - a data folder must sit inside <repo>/data/ and can never be the repo root or a sibling folder;
  - the root layout is exactly what it always was when no folder is given;
  - setup_database.py with --data-dir builds and seeds a database in that folder, refuses --csv without
    --data-dir (which would wipe the Charlotte leads.db), refuses --data-dir without --csv, reads a CSV
    with a byte-order mark, and leaves the Charlotte leads.db byte-for-byte unchanged;
  - run.py with a data folder writes the database, export, batch stats, skipped-leads file AND the debug log
    into that folder, writes NOT ONE BYTE to the repo-root debug log or the root export files, and refuses a
    missing or empty database or a Place ID that is also in Charlotte's.
"""
import csv
import hashlib
import logging
import os
import shutil
import sqlite3
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, HERE)
for k in ("ANTHROPIC_API_KEY", "BRAVE_API_KEY", "GEMINI_API_KEY"):
    os.environ[k] = "test-key-not-real"        # load_dotenv never overrides these, so real keys are never used

import paths                                    # noqa: E402
import setup_database as sdb                    # noqa: E402
import run                                      # noqa: E402  (imports graph, which attaches the root debug-log handler)

failures = []


def check(label, ok):
    print(("PASS " if ok else "FAIL ") + label)
    if not ok:
        failures.append(label)


def sha(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def snap(path):
    """(exists, size, mtime_ns) of a file, so 'untouched' can be checked even for a file that does not exist."""
    if not os.path.exists(path):
        return (False, 0, 0)
    st = os.stat(path)
    return (True, st.st_size, st.st_mtime_ns)


def raises_systemexit(fn, *a, **k):
    try:
        fn(*a, **k)
    except SystemExit as e:
        return str(e) or "exit"
    return None


CHARLOTTE_DB = paths.DB_PATH
charlotte_hash = sha(CHARLOTTE_DB) if os.path.exists(CHARLOTTE_DB) else None

# --- resolve_data_dir -------------------------------------------------------------------------------
tmp_root = tempfile.mkdtemp(prefix="stagef_root_")
os.makedirs(os.path.join(tmp_root, "data"))


def rejects(d, root=tmp_root):
    try:
        paths.resolve_data_dir(d, root)
    except ValueError:
        return True
    return False


good = paths.resolve_data_dir(os.path.join(tmp_root, "data", "atlanta", "sealed"), tmp_root)
check("resolve_data_dir accepts <root>/data/atlanta/sealed", good == os.path.realpath(os.path.join(tmp_root, "data", "atlanta", "sealed")))
check("resolve_data_dir rejects the repo root", rejects(tmp_root))
check("resolve_data_dir rejects <root>/data itself", rejects(os.path.join(tmp_root, "data")))
check("resolve_data_dir rejects a sibling folder of data/", rejects(os.path.join(tmp_root, "elsewhere")))
check("resolve_data_dir rejects a path that climbs out with '..'", rejects(os.path.join(tmp_root, "data", "..", "elsewhere")))
check("resolve_data_dir rejects a folder whose name only starts with 'data' (data_evil)", rejects(os.path.join(tmp_root, "data_evil", "x")))
check("resolve_data_dir rejects an absolute path on a different tree", rejects(tempfile.gettempdir()))

# --- data_paths -------------------------------------------------------------------------------------
root_layout = paths.data_paths(None)
check("data_paths(None) is the root layout the pipeline has always used",
      root_layout["db"] == paths.DB_PATH == os.path.join(ROOT, "leads.db")
      and root_layout["export"] == paths.EXPORT_PATH and root_layout["batch_stats"] == paths.BATCH_STATS_PATH
      and root_layout["skipped"] == "skipped_leads.csv" and root_layout["log"] == "langgraph_debug.log")
dp = paths.data_paths(os.path.join(tmp_root, "data", "atlanta", "sealed"), tmp_root)
folder = os.path.realpath(os.path.join(tmp_root, "data", "atlanta", "sealed"))
check("data_paths(folder): all five files sit directly in the folder, none in the root",
      sorted(dp) == ["batch_stats", "db", "export", "log", "skipped"]
      and all(os.path.dirname(v) == folder for v in dp.values()))
shutil.rmtree(tmp_root, ignore_errors=True)

# --- setup_database --data-dir ----------------------------------------------------------------------
TEST_REL = os.path.join("data", f"_test_stage_f_{os.getpid()}")
TEST_DIR = os.path.join(ROOT, TEST_REL)
data_existed = os.path.isdir(os.path.join(ROOT, "data"))
os.makedirs(TEST_DIR, exist_ok=True)

csv_path = os.path.join(TEST_DIR, "synthetic_leads.csv")
HEAD = ["business_name", "website", "address", "phone", "id", "google_url"]
ROWS = [
    ["Zephyr Cuts", "https://zephyrcuts.example", "10 Main St, Atlanta, GA 30303, USA", "(404) 555-0101", "TESTPID-001", "u"],
    ["Peachtree Fade", "", "20 Peach St, Decatur, GA 30030, USA", "(404) 555-0102", "TESTPID-002", "u"],
    ["Sarah @ Zephyr", "", "10 Main St, Atlanta, GA 30303, USA", "(404) 555-0103", "TESTPID-003", "u"],
    ["No Id Barbers", "", "30 Oak St, Atlanta, GA 30303, USA", "(404) 555-0104", "", "u"],
]
with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:          # utf-8-sig WRITES a BOM, like a Drive export
    w = csv.writer(f)
    w.writerow(HEAD)
    w.writerows(ROWS)
check("fixture CSV really starts with a byte-order mark", open(csv_path, "rb").read(3) == b"\xef\xbb\xbf")

sub = os.path.join(TEST_REL, "sealed")                                   # relative to the repo root's folder, like a user would
sub_abs = os.path.join(ROOT, sub)
r1 = raises_systemexit(sdb.main, ["--csv", csv_path])
check("--csv without --data-dir refuses (it would target the Charlotte leads.db)", r1 is not None and "--data-dir" in r1)
r2 = raises_systemexit(sdb.main, ["--data-dir", sub_abs])
check("--data-dir without --csv refuses (it would seed Charlotte's CSV into another city)", r2 is not None and "--csv" in r2)
r3 = raises_systemexit(sdb.main, ["--data-dir", tempfile.gettempdir(), "--csv", csv_path])
check("--data-dir outside <repo>/data/ refuses", r3 is not None and "inside" in r3)
r4 = raises_systemexit(sdb.main, ["--data-dir", ROOT, "--csv", csv_path, "--reset"])
check("--data-dir pointing at the repo root refuses, even with --reset", r4 is not None)
check("nothing was created by the refused calls", not os.path.exists(sub_abs))

sdb.main(["--data-dir", sub_abs, "--csv", csv_path])
atl_db = os.path.join(sub_abs, "leads.db")
check("--data-dir builds leads.db inside the folder", os.path.exists(atl_db))
conn = sqlite3.connect(atl_db)
names = [r[0] for r in conn.execute("SELECT business_name FROM leads ORDER BY id")]
cities = {r[0]: (r[1], r[2]) for r in conn.execute("SELECT place_id, city, state FROM leads")}
conn.close()
check("BOM CSV seeds correctly: 3 leads (the row with no Place ID is dropped), first name intact",
      names == ["Zephyr Cuts", "Peachtree Fade", "Sarah @ Zephyr"] and "﻿Zephyr Cuts" not in names)
check("city and state are parsed from the address", cities["TESTPID-002"] == ("Decatur", "GA"))
r5 = raises_systemexit(sdb.main, ["--data-dir", sub_abs, "--csv", csv_path])
check("a second build without --reset refuses to touch the existing database", r5 is not None and "--reset" in r5)
sdb.main(["--data-dir", sub_abs, "--csv", csv_path, "--reset"])
conn = sqlite3.connect(atl_db)
n_rebuilt = len(conn.execute("SELECT 1 FROM leads").fetchall())
conn.close()                                                             # an open handle would lock the file on Windows
check("--reset rebuilds the folder's database", n_rebuilt == 3)
check("Charlotte leads.db is byte-for-byte unchanged by setup_database --data-dir",
      charlotte_hash is None or sha(CHARLOTTE_DB) == charlotte_hash)

# --- run.py: preflight --------------------------------------------------------------------------------
check("preflight: a valid folder database returns its lead count", run.preflight_data_dir(atl_db) == 3)
check("preflight: a missing database refuses", raises_systemexit(run.preflight_data_dir, os.path.join(sub_abs, "nope.db")) is not None)
empty_db = os.path.join(sub_abs, "empty.db")
sdb.init_database(reset=True, db_path=empty_db)
check("preflight: an empty database refuses", "no leads" in (raises_systemexit(run.preflight_data_dir, empty_db) or ""))
fake_charlotte = os.path.join(sub_abs, "fake_charlotte.db")
sdb.init_database(reset=True, db_path=fake_charlotte)
conn = sqlite3.connect(fake_charlotte)
conn.execute("INSERT INTO leads (place_id, business_name) VALUES ('TESTPID-002', 'Same Place Id Elsewhere')")
conn.commit()
conn.close()
fc_hash = sha(fake_charlotte)
msg = raises_systemexit(run.preflight_data_dir, atl_db, fake_charlotte)
check("preflight: a Place ID that is also in the Charlotte database refuses (names not printed)",
      msg is not None and "1 Place ID" in msg and "Same Place Id Elsewhere" not in msg)
check("preflight opens the Charlotte database read-only (file unchanged)", sha(fake_charlotte) == fc_hash)

# --- run.py: a full data-folder run with a fake graph --------------------------------------------------
class FakeApp:
    """Stands in for the compiled graph: returns an answer per lead, calls nothing."""
    calls = []

    def invoke(self, lead_input):
        FakeApp.calls.append(lead_input["business_name"])
        logging.getLogger("graph").info(f"fake graph answered for {lead_input['business_name']}")
        return {"decision_maker_first_name": "Fakefirst", "decision_maker_last_name": "Fakelast", "confidence": "high",
                "title": "Owner", "evidence_url": "https://example.test/x", "status": "ok",
                "delivered_confidence": "high", "gemini": {"status": "off"}}


root_logger = logging.getLogger()
orig_handlers = [h for h in root_logger.handlers if isinstance(h, logging.FileHandler)]
check("graph.py attached a debug-log FileHandler to the root logger (what the redirect must replace)",
      any(os.path.basename(h.baseFilename) == "langgraph_debug.log" for h in orig_handlers))
root_log = next(h.baseFilename for h in orig_handlers if os.path.basename(h.baseFilename) == "langgraph_debug.log")
root_files = [root_log, paths.EXPORT_PATH, paths.BATCH_STATS_PATH, os.path.join(os.getcwd(), "skipped_leads.csv")]
before = {f: snap(f) for f in root_files}

real_app, run.app = run.app, FakeApp()
try:
    run.process_leads_batch(workers=1, data_dir=sub_abs)
finally:
    run.app = real_app
    for h in list(root_logger.handlers):                       # release the folder log (Windows locks open files), put graph's back
        if isinstance(h, logging.FileHandler) and h not in orig_handlers:
            root_logger.removeHandler(h)
            h.close()
    for h in orig_handlers:
        if h not in root_logger.handlers:
            root_logger.addHandler(h)

atl = paths.data_paths(sub_abs)
check("the fake graph ran for the 2 non-skipped leads only", sorted(FakeApp.calls) == ["Peachtree Fade", "Zephyr Cuts"])
check("database, export, batch stats, skipped-leads file and debug log all exist in the folder",
      all(os.path.exists(atl[k]) for k in ("db", "export", "batch_stats", "skipped", "log")))
folder_log = open(atl["log"], encoding="utf-8").read()
check("the folder's debug log holds the run's log lines (and the fake lead name)",
      "Processing 2 leads" in folder_log and "fake graph answered for Zephyr Cuts" in folder_log)
check("the repo-root debug log got NOT ONE new byte", snap(root_log) == before[root_log])
check("no root enriched_leads.csv / batch_stats.csv / skipped_leads.csv was written or changed",
      all(snap(f) == before[f] for f in root_files[1:]))
rows = list(csv.DictReader(open(atl["export"], encoding="utf-8")))
check("the folder's export has the 3 leads: 2 found, 1 skipped", sorted(r["outcome"] for r in rows) == ["found", "found", "skipped"])
skipped_rows = list(csv.DictReader(open(atl["skipped"], encoding="utf-8")))
check("the folder's skipped-leads file lists the '@' lead", [r["business_name"] for r in skipped_rows] == ["Sarah @ Zephyr"])
check("Charlotte leads.db is byte-for-byte unchanged after the full data-folder run",
      charlotte_hash is None or sha(CHARLOTTE_DB) == charlotte_hash)

# --- redirect_debug_log is safe to call twice ------------------------------------------------------------
t = os.path.join(sub_abs, "twice.log")
a = run.redirect_debug_log(t)
n1 = sum(isinstance(h, logging.FileHandler) and h.baseFilename == a for h in root_logger.handlers)
run.redirect_debug_log(t)
n2 = sum(isinstance(h, logging.FileHandler) and h.baseFilename == a for h in root_logger.handlers)
check("redirect_debug_log twice leaves exactly one handler on the folder log", n1 == 1 and n2 == 1)
for h in list(root_logger.handlers):
    if isinstance(h, logging.FileHandler) and h not in orig_handlers:
        root_logger.removeHandler(h)
        h.close()

# --- clean up ---------------------------------------------------------------------------------------------
shutil.rmtree(TEST_DIR, ignore_errors=True)
if not data_existed:
    try:
        os.rmdir(os.path.join(ROOT, "data"))
    except OSError:
        pass

print("\nData-folder tests passed." if not failures else f"\n{len(failures)} FAILED: {failures}")
sys.exit(1 if failures else 0)
