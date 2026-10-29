"""Free checks for the stage F intake validator (eval/holdout_intake.py) and the shared draw rule.
Synthetic leads, a fake Charlotte database, no network, no keys.

    python eval/test_holdout_intake.py
"""
import csv
import hashlib
import os
import random
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))

import holdout_common as hc                      # noqa: E402
import holdout_intake as hi                      # noqa: E402
import setup_database as sdb                     # noqa: E402

failures = []


def check(label, ok):
    print(("PASS " if ok else "FAIL ") + label)
    if not ok:
        failures.append(label)


def pid(n):
    return f"ChIJ{n:020d}"


def sha(path):
    return hc.file_sha256(path)


TEST_REL = os.path.join("data", f"_test_holdout_{os.getpid()}")
BASE = os.path.join(ROOT, TEST_REL)
data_existed = os.path.isdir(os.path.join(ROOT, "data"))
os.makedirs(BASE, exist_ok=True)

HEAD = ["business_name", "website", "address", "phone", "id", "google_url"]


def letters(i):
    return "".join(chr(97 + (i // 26 ** k) % 26) for k in range(3))


ROWS = [
    ["Fade Factory Barbershop", "https://fadefactory.example", "110 Peachtree St NE, Atlanta, GA 30303, USA", "(404) 555-0111", pid(1), "g1"],
    ["Gentlemen's Cuts", "https://booksy.com/en-us/x", "220 Ponce de Leon Ave, Atlanta, GA 30308, USA", "(404) 555-0112", pid(2), "g2"],
    ["Decatur Barber Lounge", "", "330 Church St, Decatur, GA 30030, USA", "(404) 555-0113", pid(3), "g3"],
    ["John Smith", "", "440 Main St, Marietta, GA 30060, USA", "(770) 555-0114", pid(4), "g4"],
    ["Mobile Cuts by Mike", "https://instagram.com/mobilecuts", "550 Oak Rd, Atlanta, GA 30309, USA", "(404) 555-0115", pid(5), "g5"],
    ["Twin A Barbershop", "https://twina.example", "660 Elm St, Atlanta, GA 30310, USA", "(404) 555-0116", pid(6), "g6"],
    ["Twin B Barbershop", "", "660 Elm St, Atlanta, GA 30310, USA", "(404) 555-0116", pid(7), "g7"],
    ["Roosters Men's Grooming Center - Midtown", "https://roosters.example/midtown", "770 Spring St, Atlanta, GA 30308, USA", "(404) 555-0117", pid(8), "g8"],
    ["Sarah @ Zephyr", "", "880 Pine St, Atlanta, GA 30309, USA", "(404) 555-0118", pid(9), "g9"],       # '@' rule
    ["Fade Factory Barbershop", "", "990 Lake Rd, Roswell, GA 30075, USA", "(678) 555-0119", pid(10), "g10"],   # same name, other place
    ["No Id Barbers", "", "30 Oak St, Atlanta, GA 30303, USA", "(404) 555-0120", "", "g11"],                  # missing Place ID
    ["Fade Factory Duplicate", "", "110 Peachtree St NE, Atlanta, GA 30303, USA", "(404) 555-0111", pid(1), "g12"],  # duplicate Place ID
    ["Nashville Cuts", "", "12 Broad St, Nashville, TN 37201, USA", "(615) 555-0121", pid(13), "g13"],        # other state
    ["Nowhere Barbers", "", "somewhere", "(404) 555-0122", pid(14), "g14"],                                   # unparsable address
    ["Nophone Barbershop", "https://nophone.example", "5 Fifth St, Atlanta, GA 30303, USA", "", pid(15), "g15"],  # no phone (kept)
]
FILLER = [[f"{letters(i).title()} Barbershop", f"https://{letters(i)}.example", f"{100 + i} Filler Ave, Atlanta, GA 30303, USA",
           f"(404) 556-{i:04d}", pid(100 + i), f"gf{i}"] for i in range(60)]
POP_IDS = ({pid(n) for n in (1, 2, 3, 4, 5, 6, 7, 8, 10, 15)}) | {r[4] for r in FILLER}      # what must survive the filters (70)


def write_raw(path, header, rows, bom=False):
    with open(path, "w", newline="", encoding="utf-8-sig" if bom else "utf-8") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)
    return path


fake_charlotte = os.path.join(BASE, "fake_charlotte.db")
sdb.init_database(reset=True, db_path=fake_charlotte)
import sqlite3                                   # noqa: E402
c = sqlite3.connect(fake_charlotte)
c.execute("INSERT INTO leads (place_id, business_name) VALUES ('ChIJCHARLOTTE00000000001', 'Roosters Men''s Grooming Center - Ballantyne')")
c.execute("INSERT INTO leads (place_id, business_name) VALUES ('ChIJCHARLOTTE00000000002', 'Some Other Charlotte Shop')")
c.commit()
c.close()
charlotte_hash = sha(fake_charlotte)

quiet = lambda s: None                           # noqa: E731


def go(name, header, rows, **kw):
    """Run the validator on a fixture; returns (exit code, out folder, report text)."""
    raw = write_raw(os.path.join(BASE, f"{name}_raw.csv"), header, rows, bom=kw.pop("bom", False))
    out = os.path.join(TEST_REL, name)
    code = hi.run(raw, out, charlotte_db=kw.pop("charlotte_db", fake_charlotte), echo=quiet, **kw)
    folder = os.path.join(ROOT, out)
    rep = open(os.path.join(folder, "validation_report.txt"), encoding="utf-8").read() if os.path.exists(os.path.join(folder, "validation_report.txt")) else ""
    return code, folder, rep


def ids_in(path):
    return [r["id"] for r in csv.DictReader(open(path, newline="", encoding="utf-8"))]


# ---- the shared draw rule ------------------------------------------------------------------------------
ids = [pid(i) for i in range(200)]
expected = [p for _, p in sorted((hashlib.sha256(f"20261011:{p}".encode()).hexdigest(), p) for p in ids)][:45]
check("draw_sample = first 45 by sha256('20261011:<place_id>') ascending (independent recomputation)", hc.draw_sample(ids, 45) == expected)
shuffled = ids[:]
random.Random(1).shuffle(shuffled)
check("draw_sample does not depend on the order the IDs arrive in", hc.draw_sample(shuffled, 45) == expected)
check("draw_sample takes everyone when n is at least the population size", sorted(hc.draw_sample(ids[:30], 45)) == sorted(ids[:30]))
check("order_key and draw_key are different orderings", hc.order_key(1, "a") != hc.draw_key(1, "a"))
check("chain_stem strips a location suffix", hc.chain_stem("Roosters Men's Grooming Center - Ballantyne") == hc.chain_stem("Roosters Men's Grooming Center - Midtown"))

# ---- the main run, no sample ----------------------------------------------------------------------------
raw_main = os.path.join(BASE, "main_raw.csv")
code, folder, rep = go("main", HEAD, ROWS + FILLER, bom=True)
raw_hash = sha(raw_main)
check("exit 0 on a good pull", code == 0)
check("a byte-order mark is stripped and reported", "byte-order mark removed" in rep)
filtered = ids_in(os.path.join(folder, "atlanta_leads_filtered.csv"))
check("filtered population is exactly the 70 leads that should survive (dups, missing, other state, bad address, '@' gone)",
      sorted(filtered) == sorted(POP_IDS))
check("no --sample: no clean CSV and no sample.txt are written", not os.path.exists(os.path.join(folder, "atlanta_leads_clean.csv"))
      and not os.path.exists(os.path.join(folder, "sample.txt")))
check("the report says the list is not final", "NOT the final list" in rep)
dropped = list(csv.DictReader(open(os.path.join(folder, "dropped_rows.csv"), newline="", encoding="utf-8")))
reasons = " | ".join(d["reason"] for d in dropped)
check("dropped rows name each reason once: missing ID, duplicate, other state, unparsable address",
      len(dropped) == 4 and "missing business name or Place ID" in reasons and "duplicate Place ID" in reasons
      and "state is TN" in reasons and "does not parse" in reasons)
skipped = list(csv.DictReader(open(os.path.join(folder, "skipped_preview.csv"), newline="", encoding="utf-8")))
check("the '@' lead is in skipped_preview.csv and not in the filtered list",
      [s["business_name"] for s in skipped] == ["Sarah @ Zephyr"] and pid(9) not in filtered and "'@'" in skipped[0]["reason"])
check("the report states the Charlotte denylist cannot match here", "all Charlotte" in rep)

txt = open(os.path.join(folder, "eyeball_list.txt"), encoding="utf-8").read()
flagged_part, rest = txt.split("=== EVERYONE ELSE")
for nm in ("John Smith", "Mobile Cuts by Mike", "Twin A Barbershop", "Twin B Barbershop", "Gentlemen's Cuts"):
    check(f"eyeball list flags {nm!r}", nm in flagged_part)
check("eyeball list does not flag an ordinary shop (Decatur Barber Lounge, a filler shop)",
      "Decatur Barber Lounge" in rest and "Aaa Barbershop" in rest)
check("the shared phone and address are named in the reasons", "shares phone with" in flagged_part and "shares address with" in flagged_part)
fl = {r["id"]: r["flags"] for r in csv.DictReader(open(os.path.join(folder, "atlanta_leads_filtered.csv"), newline="", encoding="utf-8"))}
check("chain-name overlap with Charlotte is flagged and the lead is kept (Roosters Midtown vs Ballantyne)",
      "chain_overlap" in fl[pid(8)] and pid(8) in filtered)
check("two leads with the same name and different Place IDs are both kept and flagged",
      "same_name_other_place_id" in fl[pid(1)] and "same_name_other_place_id" in fl[pid(10)])
check("a lead with no phone is kept and flagged", "no_phone" in fl[pid(15)])
check("the report prints the city mix and the website mix", "city mix" in rep and "Atlanta" in rep and "website mix" in rep)
check("the raw pull file is untouched", sha(raw_main) == raw_hash)
check("the (fake) Charlotte database was opened read-only: unchanged", sha(fake_charlotte) == charlotte_hash)

# ---- the draw, after the skim ------------------------------------------------------------------------------
rawp = os.path.join(BASE, "main_raw.csv")
out_main = os.path.join(TEST_REL, "main")
orig_committed = hi.intake_py_committed
hi.intake_py_committed = lambda: True
code_no_skim = hi.run(rawp, out_main, sample=45, charlotte_db=fake_charlotte, echo=quiet)
check("--sample without --skimmed refuses", code_no_skim == 2 and not os.path.exists(os.path.join(folder, "atlanta_leads_clean.csv")))
hi.intake_py_committed = lambda: False
code_dirty = hi.run(rawp, out_main, sample=45, skimmed=True, charlotte_db=fake_charlotte, echo=quiet)
check("--sample refuses while src/intake.py has an uncommitted change (denylist must be committed first)",
      code_dirty == 2 and not os.path.exists(os.path.join(folder, "atlanta_leads_clean.csv")))
hi.intake_py_committed = lambda: True
code_ok = hi.run(rawp, out_main, sample=45, seed=20261011, skimmed=True, charlotte_db=fake_charlotte, echo=quiet)
clean_ids = ids_in(os.path.join(folder, "atlanta_leads_clean.csv"))
exp = [p for _, p in sorted((hashlib.sha256(f"20261011:{p}".encode()).hexdigest(), p) for p in POP_IDS)][:45]
check("the sample is the first 45 of the FILTERED population by the pre-registered rule", code_ok == 0 and clean_ids == exp)
check("no dropped, skipped or duplicate lead can appear in the sample",
      set(clean_ids) <= POP_IDS and pid(9) not in clean_ids and pid(13) not in clean_ids and len(set(clean_ids)) == 45)
check("sample.txt records seed, rule, population and the Place IDs in draw order",
      all(s in open(os.path.join(folder, "sample.txt"), encoding="utf-8").read() for s in ("seed 20261011", "population 70", "drawn 45", exp[0], exp[-1])))
clean_header = open(os.path.join(folder, "atlanta_leads_clean.csv"), encoding="utf-8").readline().strip().split(",")
check("the clean CSV has exactly the pipeline's columns (business_name, website, address, phone, id) plus carried fields",
      clean_header == hc.CLEAN_COLUMNS)

# the same sample when the rows arrive in a different order
rnd = (ROWS + FILLER)[:]
random.Random(7).shuffle(rnd)
code2, folder2, _ = go("shuffled", HEAD, rnd, sample=45, skimmed=True)
check("the sample is the same when the pull's rows are in a different order", code2 == 0 and ids_in(os.path.join(folder2, "atlanta_leads_clean.csv")) == exp)
code3, folder3, rep3 = go("small", HEAD, ROWS, sample=45, skimmed=True)
pop_small = ids_in(os.path.join(folder3, "atlanta_leads_filtered.csv"))
check("when 45 or fewer leads qualify, all are taken", code3 == 0 and sorted(ids_in(os.path.join(folder3, "atlanta_leads_clean.csv"))) == sorted(pop_small)
      and "all taken" in rep3)
hi.intake_py_committed = orig_committed

# ---- phantom column -----------------------------------------------------------------------------------------
code, folder_p, rep = go("phantom_plus", HEAD, [[str(i)] + r for i, r in enumerate(ROWS + FILLER)])
check("phantom column (data has an extra leading cell): detected, reported, realigned to the same 70 leads",
      code == 0 and "PHANTOM COLUMN" in rep and sorted(ids_in(os.path.join(folder_p, "atlanta_leads_filtered.csv"))) == sorted(POP_IDS))
code, folder_m, rep = go("phantom_minus", [""] + HEAD, ROWS + FILLER)
check("phantom column (header has an extra leading cell): detected and realigned",
      code == 0 and "PHANTOM COLUMN" in rep and sorted(ids_in(os.path.join(folder_m, "atlanta_leads_filtered.csv"))) == sorted(POP_IDS))
junk = [[f"x{i}", "???", "???", "???", "???", "???"] for i in range(10)]
code, folder_j, rep = go("garbage", HEAD, junk)
check("a file that cannot be aligned stops with exit 2 and writes no lead list",
      code == 2 and "STOP" in rep and not os.path.exists(os.path.join(folder_j, "atlanta_leads_filtered.csv")))
code, folder_h, rep = go("nohead", ["foo", "bar"], [["a", "b"]])
check("a file without the required columns stops and says which are missing", code == 2 and "required column" in rep)

# ---- Charlotte overlap stops it ---------------------------------------------------------------------------------
overlap_rows = ROWS + FILLER + [["Charlotte Shop Here", "", "1 Tryon St, Atlanta, GA 30303, USA", "(404) 555-0199", "ChIJCHARLOTTE00000000002", "gx"]]
code, folder_o, rep = go("overlap", HEAD, overlap_rows)
check("a Place ID that is already a Charlotte lead STOPS the validator (exit 2) and no list is written",
      code == 2 and "already Charlotte leads" in rep and not os.path.exists(os.path.join(folder_o, "atlanta_leads_filtered.csv")))
check("the stop message does not print business names", "Charlotte Shop Here" not in rep and "Some Other Charlotte Shop" not in rep)
code, folder_nc, rep = go("nocharlotte", HEAD, ROWS + FILLER, charlotte_db=os.path.join(BASE, "does_not_exist.db"))
check("with no Charlotte database the overlap check is skipped loudly, not silently", code == 0 and "NOT checked" in rep)

# ---- n8n answer columns ---------------------------------------------------------------------------------------------
n8n_head = HEAD + ["decision_maker_first_name", "email"]
n8n_rows = [r + ["Ownerfirst", "a@b.example"] for r in ROWS + FILLER]
code, folder_n, rep = go("n8ncols", n8n_head, n8n_rows)
clean_txt = open(os.path.join(folder_n, "atlanta_leads_filtered.csv"), encoding="utf-8").read()
check("filled n8n answer columns are warned about and NOT copied into the lead list",
      code == 0 and "n8n answer/outreach columns" in rep and "Ownerfirst" not in clean_txt and "a@b.example" not in clean_txt)

# ---- the output folder rule --------------------------------------------------------------------------------------------
echoed = []
code_bad = hi.run(rawp, os.path.join(ROOT, "elsewhere"), echo=echoed.append)
check("an output folder outside <repo>/data/ is refused", code_bad == 2 and echoed and "STOP" in echoed[0] and not os.path.exists(os.path.join(ROOT, "elsewhere")))

# ---- no pipeline, no network, no keys -------------------------------------------------------------------------------------
probe = ("import sys; sys.path[:0] = [r'%s', r'%s']; import holdout_intake; "
         "print(sorted(m for m in ('graph','requests','anthropic','langchain_anthropic','langgraph','dotenv','httpx') if m in sys.modules))"
         % (HERE, os.path.join(ROOT, "src")))
out = subprocess.run([sys.executable, "-I", "-c", probe], capture_output=True, text=True).stdout.strip()
check(f"importing the validator loads no pipeline graph, HTTP client or key loader (loaded: {out})", out == "[]")

# ---- the clean CSV really seeds the pipeline's database ---------------------------------------------------------------------
seed_dir = os.path.join(BASE, "seeded")
sdb.main(["--data-dir", seed_dir, "--csv", os.path.join(folder, "atlanta_leads_clean.csv")])
conn = sqlite3.connect(os.path.join(seed_dir, "leads.db"))
seeded = [r for r in conn.execute("SELECT place_id, city, state FROM leads")]
conn.close()
check("setup_database --data-dir seeds exactly the 45 drawn leads from the clean CSV, with city and state parsed",
      sorted(r[0] for r in seeded) == sorted(exp) and all(r[2] == "GA" and r[1] for r in seeded))

shutil.rmtree(BASE, ignore_errors=True)
if not data_existed:
    try:
        os.rmdir(os.path.join(ROOT, "data"))
    except OSError:
        pass
check("the test cleaned up after itself", not os.path.exists(BASE))

print("\nIntake validator tests passed." if not failures else f"\n{len(failures)} FAILED: {failures}")
sys.exit(1 if failures else 0)
