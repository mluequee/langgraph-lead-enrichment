"""Free checks for the blind verification worksheet generator (eval/make_holdout_worksheet.py).
Synthetic leads, no network, no keys.

    python eval/test_holdout_worksheet.py

The point of these checks: the worksheet is built from the lead list alone, so it can hold no pipeline answer.
"""
import csv
import hashlib
import os
import shutil
import sqlite3
import sys
import zipfile

import openpyxl

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))

import holdout_common as hc                      # noqa: E402
import make_holdout_worksheet as mw              # noqa: E402

failures = []


def check(label, ok):
    print(("PASS " if ok else "FAIL ") + label)
    if not ok:
        failures.append(label)


TEST_REL = os.path.join("data", f"_test_worksheet_{os.getpid()}")
BASE = os.path.join(ROOT, TEST_REL)
data_existed = os.path.isdir(os.path.join(ROOT, "data"))
os.makedirs(BASE, exist_ok=True)

MARKER = "ZZ-SECRET-ANSWER-ZZ"
LEADS = [{"business_name": f"Shop {chr(65 + i)} Barbershop", "website": f"https://shop{i}.example",
          "address": f"{i} Main St, Atlanta, GA 30303, USA", "phone": f"(404) 555-01{i:02d}", "id": f"ChIJ{i:020d}",
          "google_url": f"https://maps.example/{i}", "city": "Atlanta", "state": "GA",
          "flags": "chain_overlap:Roosters" if i == 3 else ""} for i in range(12)]
clean = os.path.join(BASE, "intake", "atlanta_leads_clean.csv")
os.makedirs(os.path.dirname(clean))
with open(clean, "w", newline="", encoding="utf-8") as f:
    w = csv.DictWriter(f, fieldnames=hc.CLEAN_COLUMNS)
    w.writeheader()
    w.writerows(LEADS)

# poison: an export and a database full of answers sit right beside the lead list, and a sealed folder next to that
os.makedirs(os.path.join(BASE, "sealed"))
for p in (os.path.join(BASE, "intake", "enriched_leads.csv"), os.path.join(BASE, "sealed", "enriched_leads.csv")):
    with open(p, "w", encoding="utf-8") as f:
        f.write(f"owner_first_name,owner_last_name\n{MARKER},{MARKER}\n")
conn = sqlite3.connect(os.path.join(BASE, "sealed", "leads.db"))
conn.execute("CREATE TABLE results (decision_maker_first_name TEXT)")
conn.execute("INSERT INTO results VALUES (?)", (MARKER,))
conn.commit()
conn.close()

out = os.path.join(BASE, "verify", "Atlanta Verification Worksheet.xlsx")
n = mw.build(clean, out)
check("the generator writes the worksheet and reports 12 leads", n == 12 and os.path.exists(out))

wb = openpyxl.load_workbook(out)
ws = wb["Verify"]
header = [c.value for c in ws[1]]
check("the Verify sheet has exactly the agreed columns, in order", header == hc.VERIFY_COLUMNS)
body = [[c.value for c in row] for row in ws.iter_rows(min_row=2)]
check("one row per lead", len(body) == 12)
col = {name: i for i, name in enumerate(header)}
expect_order = [r["id"] for r in sorted(LEADS, key=lambda r: (hashlib.sha256(f"20261011:order:{r['id']}".encode()).hexdigest(), r["id"]))]
check("rows are in the pre-registered random order (sha256 of '<seed>:order:<place_id>')", [r[col["place_id"]] for r in body] == expect_order)
check("the # column is 1..12 down the sheet", [r[col["#"]] for r in body] == list(range(1, 13)))
by_id = {r["id"]: r for r in LEADS}
check("business, address, phone, website and google_url are copied from the lead list",
      all(r[col["business_name"]] == by_id[r[col["place_id"]]]["business_name"]
          and r[col["address"]] == by_id[r[col["place_id"]]]["address"]
          and r[col["phone"]] == by_id[r[col["place_id"]]]["phone"]
          and r[col["website"]] == by_id[r[col["place_id"]]]["website"]
          and r[col["google_url"]] == by_id[r[col["place_id"]]]["google_url"] for r in body))
hand = list(range(hc.FIRST_HAND_COLUMN, len(header)))
check("every hand-filled cell is EMPTY (verdict, owner, co-owner, goes-by, sources, checked, minutes, notes)",
      all(r[j] in (None, "") for r in body for j in hand))
check("no flags / chain-overlap information is carried into the worksheet", "flags" not in header
      and not any("chain_overlap" in str(v) for r in body for v in r) )

dvs = {str(rng): dv for dv in ws.data_validations.dataValidation for rng in dv.sqref.ranges}
def dv_for(colname):
    letter = openpyxl.utils.get_column_letter(header.index(colname) + 1)
    return next((dv for rng, dv in dvs.items() if rng.startswith(f"{letter}2:")), None)
check("verdict, source_type, source_type_2 and checked are dropdowns", all(dv_for(c) is not None and dv_for(c).type == "list"
      for c in ("verdict", "source_type", "source_type_2", "checked")))
lists = wb["Lists"]
check("the Lists sheet holds exactly the four verdicts and the ten source types",
      [c.value for c in lists["A"][1:5]] == hc.VERDICTS and [c.value for c in lists["B"][1:11]] == hc.SOURCE_TYPES
      and len(hc.SOURCE_TYPES) == 10)
check("the ten source types are the D5 set", hc.SOURCE_TYPES == ["LinkedIn", "News", "BBB", "Booksy", "Video", "Directory",
                                                                  "Own website", "Social (IG/FB)", "GA registry", "Other"])
guide_text = " ".join(str(c.value) for row in wb["Instructions"].iter_rows() for c in row if c.value)
check("the Instructions sheet carries the time box, the blind rule, every verdict and every source type",
      "8 minutes" in guide_text and "sealed" in guide_text and all(v in guide_text for v in hc.VERDICTS)
      and all(s in guide_text for s in hc.SOURCE_TYPES) and "Never fetch or scrape a LinkedIn page" in guide_text)
check("the Instructions sheet is the first tab", wb.sheetnames[0] == "Instructions")

# --- blindness -----------------------------------------------------------------------------------------------
with zipfile.ZipFile(out) as z:
    blob = b"".join(z.read(name) for name in z.namelist())
check("a marker planted in a neighbouring export, database and sealed folder is NOT in the workbook", MARKER.encode() not in blob)
src = open(os.path.join(HERE, "make_holdout_worksheet.py"), encoding="utf-8").read()
code_only = "\n".join(l for l in src.splitlines() if not l.lstrip().startswith("#"))
code_only = code_only.split('"""', 2)[2] if code_only.count('"""') >= 2 else code_only       # drop the module docstring
check("the generator's code never opens a database, an export or the sealed folder, and does not import the pipeline",
      not any(w in code_only for w in ("sqlite3", "enriched_leads", "import graph", "from graph", "import run", "LeadDatabase", "open(")))

# --- same input, same worksheet order; refuses to overwrite ----------------------------------------------------
out2 = os.path.join(BASE, "verify2", "w.xlsx")
mw.build(clean, out2)
ws2 = openpyxl.load_workbook(out2)["Verify"]
check("a second build gives the same row order", [c.value for c in ws2["B"]] == [c.value for c in ws["B"]])
try:
    mw.build(clean, out)
    refused = False
except ValueError as e:
    refused = "never overwritten" in str(e)
check("an existing worksheet is NEVER overwritten", refused)
wb_after = openpyxl.load_workbook(out)["Verify"]
check("the refused call left the first worksheet intact", wb_after.max_row == 13)

# --- bad inputs --------------------------------------------------------------------------------------------------
def raises(fn):
    try:
        fn()
    except ValueError as e:
        return str(e)
    return None

check("an output path outside <repo>/data/ is refused", raises(lambda: mw.build(clean, os.path.join(ROOT, "elsewhere", "w.xlsx"))) is not None
      and not os.path.exists(os.path.join(ROOT, "elsewhere")))
dup = os.path.join(BASE, "dup.csv")
with open(dup, "w", newline="", encoding="utf-8") as f:
    w = csv.DictWriter(f, fieldnames=hc.CLEAN_COLUMNS)
    w.writeheader()
    w.writerows(LEADS + [LEADS[0]])
check("a lead list with a duplicate Place ID is refused", "duplicate" in (raises(lambda: mw.build(dup, os.path.join(BASE, "v3", "w.xlsx"))) or ""))
empty = os.path.join(BASE, "empty.csv")
with open(empty, "w", newline="", encoding="utf-8") as f:
    csv.writer(f).writerow(hc.CLEAN_COLUMNS)
check("an empty lead list is refused", "empty" in (raises(lambda: mw.build(empty, os.path.join(BASE, "v4", "w.xlsx"))) or ""))
check("the CLI returns exit code 2 with a STOP message for a bad input", mw.main([empty, "--out", os.path.join(BASE, "v5", "w.xlsx")]) == 2)

shutil.rmtree(BASE, ignore_errors=True)
if not data_existed:
    try:
        os.rmdir(os.path.join(ROOT, "data"))
    except OSError:
        pass
check("the test cleaned up after itself", not os.path.exists(BASE))

print("\nWorksheet tests passed." if not failures else f"\n{len(failures)} FAILED: {failures}")
sys.exit(1 if failures else 0)
