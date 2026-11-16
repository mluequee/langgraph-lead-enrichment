"""Free checks for the stage F intake validator (eval/holdout_intake.py) and the shared draw rule.
Synthetic leads, a fake Charlotte database, no network, no keys.

    python eval/test_holdout_intake.py
"""
import contextlib
import csv
import hashlib
import io
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

HEAD = ["business_name", "website", "address", "phone", "id", "google_url", "qualified_lead"]


def letters(i):
    return "".join(chr(97 + (i // 26 ** k) % 26) for k in range(3))


SPELLINGS = ["TRUE", "true", " TRUE "]               # every qualified row cycles through these spellings


def qual(rows):
    """Append a qualified_lead value (cycling TRUE / true / ' TRUE ') to each 6-cell row."""
    return [r + [SPELLINGS[i % 3]] for i, r in enumerate(rows)]


ROWS = qual([
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
])
FILLER = qual([[f"{letters(i).title()} Barbershop", f"https://{letters(i)}.example", f"{100 + i} Filler Ave, Atlanta, GA 30303, USA",
                f"(404) 556-{i:04d}", pid(100 + i), f"gf{i}"] for i in range(60)])
POP_IDS = ({pid(n) for n in (1, 2, 3, 4, 5, 6, 7, 8, 10, 15)}) | {r[4] for r in FILLER}      # what must survive the filters (70)

# Rows the amendment says are NOT in the population. Every name starts with "Unqualified" and is unique, so a leak
# of a name or a Place ID anywhere is one string search. Some also hit a second drop rule (duplicate, other state,
# '@', missing ID, bad address): the qualified filter must keep their names out of every file anyway.
NQ_SPECIAL = [
    ["Unqualified Zeta Barbers", "", "330 Church St, Decatur, GA 30030, USA", "(404) 555-0113", pid(201), "n1", "FALSE"],   # shares phone+address with Decatur
    ["Unqualified Eta Barbershop", "https://eta.example", "301 Eta St, Atlanta, GA 30303, USA", "(404) 557-0002", pid(202), "n2", ""],
    ["Unqualified Theta Barbers", "", "302 Theta St, Atlanta, GA 30303, USA", "(404) 557-0003", pid(203), "n3", " False "],
    ["Unqualified Theta Barbers Dup", "", "302 Theta St, Atlanta, GA 30303, USA", "(404) 557-0003", pid(203), "n3b", "false"],  # duplicate
    ["Unqualified Iota Cuts", "", "303 Broad St, Nashville, TN 37201, USA", "(615) 557-0004", pid(205), "n5", "FALSE"],       # other state
    ["Unqualified @ Kappa", "", "304 Kappa St, Atlanta, GA 30303, USA", "(404) 557-0005", pid(206), "n6", "false"],            # '@'
    ["Unqualified Lambda Barbers", "", "305 Lambda St, Atlanta, GA 30303, USA", "(404) 557-0006", "", "n7", ""],               # missing ID
    ["Unqualified Mu Barbers", "", "somewhere else", "(404) 557-0007", pid(208), "n8", "FALSE"],                               # unparsable
]
NQ_FILLER = [[f"Unqualified Filler {letters(i).title()} Barbershop", f"https://nq{letters(i)}.example",
              f"{400 + i} Nq Ave, Atlanta, GA 30303, USA", f"(404) 558-{i:04d}", pid(300 + i), f"nf{i}", "FALSE" if i % 2 else ""]
             for i in range(10)]
NQ = NQ_SPECIAL + NQ_FILLER
NQ_NAMES = [r[0] for r in NQ if r[0]]
NQ_IDS = [r[4] for r in NQ if r[4]]
# The main fixture: 75 qualified rows + 18 not qualified = 93 rows (true 75, false 11, blank 7).
ALL_ROWS = ROWS + FILLER + NQ


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
    echoed = []
    code = hi.run(raw, out, charlotte_db=kw.pop("charlotte_db", fake_charlotte), echo=echoed.append, **kw)
    go.console = "\n".join(echoed)                # what the user would have seen on the console
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
code, folder, rep = go("main", HEAD, ALL_ROWS, bom=True, workflow="Ai Lead Qualification", pull_date="2026-11-15")
main_console = go.console
raw_hash = sha(raw_main)
check("exit 0 on a good pull", code == 0)
check("a byte-order mark is stripped and reported", "byte-order mark removed" in rep)
filtered = ids_in(os.path.join(folder, "atlanta_leads_filtered.csv"))
check("filtered population is exactly the 70 qualified leads that survive (not qualified, dups, missing, other state, bad address, '@' gone)",
      sorted(filtered) == sorted(POP_IDS))
check("no --sample: no clean CSV and no sample.txt are written", not os.path.exists(os.path.join(folder, "atlanta_leads_clean.csv"))
      and not os.path.exists(os.path.join(folder, "sample.txt")))
check("the report says the list is not final", "NOT the final list" in rep)
dropped = list(csv.DictReader(open(os.path.join(folder, "dropped_rows.csv"), newline="", encoding="utf-8")))
reasons = " | ".join(d["reason"] for d in dropped)
check("dropped rows (qualified rows only) name each reason once: missing ID, duplicate, other state, unparsable address",
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
code_ok = hi.run(rawp, out_main, sample=45, seed=20261011, skimmed=True, charlotte_db=fake_charlotte, echo=quiet, pull_date="2026-11-15")
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
rnd = ALL_ROWS[:]
random.Random(7).shuffle(rnd)
code2, folder2, _ = go("shuffled", HEAD, rnd, sample=45, skimmed=True)
check("the sample is the same when the pull's rows are in a different order", code2 == 0 and ids_in(os.path.join(folder2, "atlanta_leads_clean.csv")) == exp)
code3, folder3, rep3 = go("small", HEAD, ROWS + NQ, sample=45, skimmed=True)
pop_small = ids_in(os.path.join(folder3, "atlanta_leads_filtered.csv"))
check("when 45 or fewer leads qualify, all are taken", code3 == 0 and sorted(ids_in(os.path.join(folder3, "atlanta_leads_clean.csv"))) == sorted(pop_small)
      and "all taken" in rep3)
hi.intake_py_committed = orig_committed

# ---- qualified_lead (amendment 2026-11-07) --------------------------------------------------------------------
BLOCK1 = "raw pull: 93 rows   workflow: Ai Lead Qualification   pull date: 2026-11-15"
BLOCK2 = "removed as missing name/ID: 2   removed as duplicate Place ID: 2   remaining: 89"
BLOCK3 = "qualified (true): 73   not qualified (false or blank): 16"
check("the values line counts all 93 rows before any drop: true 75, false 11, blank 7",
      "qualified_lead values, all 93 rows: true 75, false 11, blank 7" in rep)
check("the report block gives raw pull, workflow and pull date; removals and remaining; qualified and not qualified (exact numbers)",
      BLOCK1 in rep and BLOCK2 in rep and BLOCK3 in rep)
check("the same block and values line are printed on the console", BLOCK1 in main_console and BLOCK2 in main_console and BLOCK3 in main_console
      and "true 75, false 11, blank 7" in main_console)
check("the 'rows read' line continues from the 73 qualified rows and says so",
      "qualified (true) 73 -> unparsable address 1, not GA 1, intake skip 1 -> filtered population 70" in rep)
filtered_txt = open(os.path.join(folder, "atlanta_leads_filtered.csv"), encoding="utf-8").read()
check("the 18 not-qualified rows are absent from the filtered list (names and Place IDs)",
      not (set(NQ_IDS) & set(filtered)) and not any(n in filtered_txt for n in NQ_NAMES))
check("a not-qualified lead sharing phone and address with a qualified one does not flag it or get named (Decatur stays unflagged)",
      "Unqualified" not in txt and "Decatur Barber Lounge" in rest)
check("dropped_rows.csv and skipped_preview.csv hold no not-qualified row",
      not any(n in open(os.path.join(folder, f), encoding="utf-8").read() for n in NQ_NAMES for f in ("dropped_rows.csv", "skipped_preview.csv")))
NQ_SURVIVORS = {r[4] for r in NQ if r[4] and "@" not in r[0] and "Nashville" not in r[2] and "somewhere" not in r[2]}
alt = [p for _, p in sorted((hashlib.sha256(f"20261011:{p}".encode()).hexdigest(), p) for p in POP_IDS | NQ_SURVIVORS)][:45]
check("sensitivity: drawing from qualified + not-qualified rows would give a DIFFERENT sample, so the sample check above proves the filter", alt != exp)
rep_sample = open(os.path.join(folder, "validation_report.txt"), encoding="utf-8").read()
check("the report block is also in the report of the --sample run", BLOCK1 in rep_sample and BLOCK3 in rep_sample and "DRAWN: 45 of 70" in rep_sample)

# privacy invariant: no not-qualified name or Place ID in any file under the output folder, the report or the console
all_text = ""
for dp, _, fns in os.walk(folder):
    for fn in fns:
        all_text += open(os.path.join(dp, fn), encoding="utf-8", errors="replace").read() + "\n"
leaks = [x for x in NQ_NAMES + NQ_IDS if x in all_text or x in main_console or x in rep_sample]
check(f"privacy: no not-qualified business name or Place ID in any file under --out, the report or the console (leaks: {leaks[:3]})", not leaks)
check("privacy: the not-qualified duplicate / other-state / '@' / missing-ID / bad-address rows are counted but never named (word 'Unqualified' absent)",
      "Unqualified" not in all_text and "Unqualified" not in main_console)

# no pull date given: older callers keep working
code_nd, folder_nd, rep_nd = go("nodate", HEAD, ROWS + FILLER)
check("run() without pull_date or workflow still works: 'pull date: not given', default workflow named",
      code_nd == 0 and "pull date: not given" in rep_nd and "workflow: Ai Lead Qualification" in rep_nd)
check("with every row qualified, the block says so (qualified (true): 73, not qualified: 0)",
      "qualified (true): 73   not qualified (false or blank): 0" in rep_nd)
code_wf, _, rep_wf = go("wf", HEAD, ROWS + FILLER, workflow="Other Flow", pull_date="2026-11-16")
check("a different workflow name and pull date are reported as given", "workflow: Other Flow   pull date: 2026-11-16" in rep_wf)

# header spelling: 'Qualified Lead' is found by alias
code_al, folder_al, rep_al = go("alias", HEAD[:-1] + ["Qualified Lead"], ROWS + NQ)
check("the header 'Qualified Lead' is found by alias", code_al == 0 and "qualified_lead values" in rep_al)

# missing column
code_mq, folder_mq, rep_mq = go("noqual", HEAD[:-1], [r[:-1] for r in ROWS + FILLER])
check("a missing qualified_lead column stops (exit 2), names the column, lists the headers seen, and writes no lead file",
      code_mq == 2 and "qualified_lead" in rep_mq and "Headers seen" in rep_mq and "business_name" in rep_mq
      and not os.path.exists(os.path.join(folder_mq, "atlanta_leads_filtered.csv")) and not os.path.exists(os.path.join(folder_mq, "dropped_rows.csv"))
      and not os.path.exists(os.path.join(folder_mq, "eyeball_list.txt")))

# unexpected values
bad_rows = [["Secret Name One Barbers", "", "1 A St, Atlanta, GA 30303, USA", "(404) 559-0001", pid(401), "b1", "yes"],
            ["Secret Name Two Barbers", "", "2 A St, Atlanta, GA 30303, USA", "(404) 559-0002", pid(402), "b2", " YES "],
            ["Secret Name Three Barbers", "", "3 A St, Atlanta, GA 30303, USA", "(404) 559-0003", pid(403), "b3", "x" * 60]]
code_bv, folder_bv, rep_bv = go("badvalue", HEAD, ROWS + FILLER + bad_rows)
check("an unexpected value ('yes') stops (exit 2), names the value with its count and row numbers, and writes no lead file",
      code_bv == 2 and "'yes'" in rep_bv and "2 row" in rep_bv and "rows 77, 78" in rep_bv
      and not os.path.exists(os.path.join(folder_bv, "atlanta_leads_filtered.csv")) and not os.path.exists(os.path.join(folder_bv, "dropped_rows.csv")))
check("the unexpected-value stop names no business and no Place ID", "Secret Name" not in rep_bv and pid(401) not in rep_bv and pid(402) not in rep_bv)
check("an unexpected value is cut to 40 characters", ("x" * 40) in rep_bv and ("x" * 41) not in rep_bv)
check("the values line is printed even when the run then stops on a bad value", "qualified_lead values, all 78 rows: true 75, false 0, blank 0" in rep_bv)
many = [["Secret Many Barbers", "", f"{i} M St, Atlanta, GA 30303, USA", f"(404) 560-{i:04d}", pid(500 + i), f"m{i}", "maybe"] for i in range(15)]
code_mv, _, rep_mv = go("manybad", HEAD, many)
line_maybe = [ln for ln in rep_mv.splitlines() if "'maybe'" in ln][0]
check("at most 10 row numbers are listed for one unexpected value", code_mv == 2 and "15 row" in line_maybe
      and "rows 2, 3, 4, 5, 6, 7, 8, 9, 10, 11" in line_maybe and "12" not in line_maybe)

# duplicate Place IDs that disagree on qualified_lead
dis = [["Dissent Alpha Barbers", "", "1 D St, Atlanta, GA 30303, USA", "(404) 561-0001", pid(601), "d1", "true"],
       ["Dissent Beta Barbers", "", "1 D St, Atlanta, GA 30303, USA", "(404) 561-0002", pid(601), "d2", "false"]]
code_d, folder_d, rep_d = go("dissent", HEAD, ROWS + FILLER + dis)
check("a duplicate Place ID whose qualified_lead values differ (true vs false) stops (exit 2), counts it, shows the Place ID, no names, no lead file",
      code_d == 2 and "1 Place ID" in rep_d and pid(601) in rep_d and "Dissent" not in rep_d
      and not os.path.exists(os.path.join(folder_d, "atlanta_leads_filtered.csv")))
dis_bf = [["Dissent Gamma Barbers", "", "1 D St, Atlanta, GA 30303, USA", "(404) 561-0003", pid(602), "d3", "false"],
          ["Dissent Delta Barbers", "", "1 D St, Atlanta, GA 30303, USA", "(404) 561-0004", pid(602), "d4", ""]]
code_d2, folder_d2, rep_d2 = go("dissent_bf", HEAD, ROWS + FILLER + dis_bf)
check("blank versus false counts as differing: stops, counts it, and (neither row qualified) prints neither names nor the Place ID",
      code_d2 == 2 and "1 Place ID" in rep_d2 and pid(602) not in rep_d2 and "Dissent" not in rep_d2
      and not os.path.exists(os.path.join(folder_d2, "atlanta_leads_filtered.csv")))
dis4 = [[f"Dissent Many {i}", "", f"{i} D St, Atlanta, GA 30303, USA", f"(404) 562-{i:04d}", pid(700 + i), f"dm{i}", "true"] for i in range(4)]
dis4 += [[f"Dissent Many B{i}", "", f"{i} D St, Atlanta, GA 30303, USA", f"(404) 563-{i:04d}", pid(700 + i), f"dn{i}", "false"] for i in range(4)]
code_d4, _, rep_d4 = go("dissent4", HEAD, dis4)
check("with 4 disagreeing Place IDs the report says 4 and lists at most 3", code_d4 == 2 and "4 Place ID" in rep_d4
      and sum(pid(700 + i) in rep_d4 for i in range(4)) == 3)
agree = [["Agree First Barbers", "", "1 G St, Atlanta, GA 30303, USA", "(404) 564-0001", pid(801), "a1", "TRUE"],
         ["Agree Second Barbers", "", "2 G St, Atlanta, GA 30303, USA", "(404) 564-0002", pid(801), "a2", " true "],
         ["Agree Unq First Barbers", "", "3 G St, Atlanta, GA 30303, USA", "(404) 564-0003", pid(802), "a3", "FALSE"],
         ["Agree Unq Second Barbers", "", "4 G St, Atlanta, GA 30303, USA", "(404) 564-0004", pid(802), "a4", "false"]]
code_a, folder_a, rep_a = go("agree", HEAD, agree)
agree_txt = open(os.path.join(folder_a, "atlanta_leads_filtered.csv"), encoding="utf-8").read()
check("duplicates that agree on qualified_lead are handled as before: the first is kept, the second is dropped (counted)",
      code_a == 0 and ids_in(os.path.join(folder_a, "atlanta_leads_filtered.csv")) == [pid(801)] and "Agree First Barbers" in agree_txt
      and "Agree Second" not in agree_txt and "removed as duplicate Place ID: 2" in rep_a)
agree_all = "".join(open(os.path.join(folder_a, f), encoding="utf-8").read() for f in os.listdir(folder_a))
check("an agreeing not-qualified duplicate pair leaves no trace in any file", "Agree Unq" not in agree_all and pid(802) not in agree_all)
dropped_a = list(csv.DictReader(open(os.path.join(folder_a, "dropped_rows.csv"), newline="", encoding="utf-8")))
check("the qualified duplicate is listed in dropped_rows.csv, the not-qualified one is not",
      len(dropped_a) == 1 and dropped_a[0]["business_name"] == "Agree Second Barbers")

# nothing qualified
code_z, folder_z, rep_z = go("zeroq", HEAD, [r[:-1] + ["false"] for r in ROWS])
check("no qualified row: the block is printed (qualified (true): 0) and the run stops, no lead named",
      code_z == 2 and "qualified (true): 0" in rep_z and "nothing is left" in rep_z and "Fade Factory" not in rep_z)

# the sample is drawn over qualified IDs only; 45 or fewer qualified take all
big_nq = [[f"Unqualified Big {letters(i).title()} Barbershop", "", f"{600 + i} Big Ave, Atlanta, GA 30303, USA", f"(404) 565-{i:04d}", pid(900 + i), f"bn{i}", "false"]
          for i in range(20)]
code_s30, folder_s30, rep_s30 = go("take_all_30", HEAD, FILLER[:30] + big_nq, sample=45, skimmed=True)
ids30 = ids_in(os.path.join(folder_s30, "atlanta_leads_clean.csv"))
check("50 rows but only 30 qualified: all 30 qualified are taken, none of the 20 not qualified",
      code_s30 == 0 and sorted(ids30) == sorted(r[4] for r in FILLER[:30]) and "all taken" in rep_s30 and "qualified (true): 30" in rep_s30)
code_s45, folder_s45, rep_s45 = go("take_all_45", HEAD, FILLER[:45] + big_nq, sample=45, skimmed=True)
check("exactly 45 qualified: all 45 taken", code_s45 == 0 and len(ids_in(os.path.join(folder_s45, "atlanta_leads_clean.csv"))) == 45)
code_s46, folder_s46, _ = go("draw_46", HEAD, FILLER[:46] + big_nq, sample=45, skimmed=True)
q46 = [r[4] for r in FILLER[:46]]
exp46 = [p for _, p in sorted((hashlib.sha256(f"20261011:{p}".encode()).hexdigest(), p) for p in q46)][:45]
check("46 qualified: the first 45 by sha256('20261011:' + place_id) over the qualified IDs only (independent recomputation)",
      code_s46 == 0 and ids_in(os.path.join(folder_s46, "atlanta_leads_clean.csv")) == exp46)

# a Charlotte Place ID on a not-qualified row: the overlap check sees ALL rows, but the ID and name are withheld
ov_nq = ROWS + FILLER + [["Unqualified Overlap Barbers", "", "1 Tryon St, Atlanta, GA 30303, USA", "(404) 555-0198", "ChIJCHARLOTTE00000000001", "ox", "false"]]
code_ov, folder_ov, rep_ov = go("overlap_nq", HEAD, ov_nq)
check("a Charlotte Place ID on a NOT-qualified row still STOPS (the overlap check sees all rows); its ID and name are withheld",
      code_ov == 2 and "1 Place ID" in rep_ov and "ChIJCHARLOTTE00000000001" not in rep_ov and "Unqualified" not in rep_ov
      and not os.path.exists(os.path.join(folder_ov, "atlanta_leads_filtered.csv")))

# CLI: --pull-date is required and must be YYYY-MM-DD
raw_cli = os.path.join(BASE, "main_raw.csv")
out_cli = os.path.join(TEST_REL, "cli")


def cli(args):
    buf, err = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
            rc = hi.main(args)
    except SystemExit as e:
        rc = e.code if isinstance(e.code, int) else 1
    return rc, buf.getvalue(), err.getvalue()


base_args = [raw_cli, "--out", out_cli, "--charlotte-db", fake_charlotte]
for bad in ("2026-13-45", "15/11/2026", "20261115", "2026-11-5", "tomorrow", ""):
    rc, _, _ = cli(base_args + ["--pull-date", bad])
    check(f"main() with --pull-date {bad!r} exits nonzero and runs nothing", rc not in (0, None) and not os.path.exists(os.path.join(ROOT, out_cli)))
rc, _, err = cli(base_args)
check("main() without --pull-date exits nonzero", rc not in (0, None) and "pull-date" in err and not os.path.exists(os.path.join(ROOT, out_cli)))
rc, so, _ = cli(base_args + ["--pull-date", "2026-11-15", "--workflow", "Some Flow"])
check("main() with a good --pull-date runs and reports it (and --workflow)", rc == 0 and "pull date: 2026-11-15" in so and "workflow: Some Flow" in so)
rc, so, _ = cli(base_args + ["--pull-date", "2026-11-15"])
check("--workflow defaults to 'Ai Lead Qualification'", rc == 0 and "workflow: Ai Lead Qualification" in so)

# ---- phantom column -----------------------------------------------------------------------------------------
code, folder_p, rep = go("phantom_plus", HEAD, [[str(i)] + r for i, r in enumerate(ALL_ROWS)])
check("phantom column (data has an extra leading cell): detected, reported, realigned to the same 70 leads",
      code == 0 and "PHANTOM COLUMN" in rep and sorted(ids_in(os.path.join(folder_p, "atlanta_leads_filtered.csv"))) == sorted(POP_IDS))
check("phantom column (extra leading data cell): qualified_lead is realigned too (true 75, false 11, blank 7; qualified 73)",
      "true 75, false 11, blank 7" in rep and BLOCK3 in rep)
code, folder_m, rep = go("phantom_minus", [""] + HEAD, ALL_ROWS)
check("phantom column (header has an extra leading cell): detected and realigned",
      code == 0 and "PHANTOM COLUMN" in rep and sorted(ids_in(os.path.join(folder_m, "atlanta_leads_filtered.csv"))) == sorted(POP_IDS))
check("phantom column (extra leading header cell): qualified_lead is realigned too (true 75, false 11, blank 7; qualified 73)",
      "true 75, false 11, blank 7" in rep and BLOCK3 in rep)
junk = [[f"x{i}", "???", "???", "???", "???", "???", "true"] for i in range(10)]
code, folder_j, rep = go("garbage", HEAD, junk)
check("a file that cannot be aligned stops with exit 2 and writes no lead list",
      code == 2 and "STOP" in rep and not os.path.exists(os.path.join(folder_j, "atlanta_leads_filtered.csv")))
code, folder_h, rep = go("nohead", ["foo", "bar"], [["a", "b"]])
check("a file without the required columns stops and says which are missing", code == 2 and "required column" in rep)

# ---- Charlotte overlap stops it ---------------------------------------------------------------------------------
overlap_rows = ROWS + FILLER + [["Charlotte Shop Here", "", "1 Tryon St, Atlanta, GA 30303, USA", "(404) 555-0199", "ChIJCHARLOTTE00000000002", "gx", "true"]]
code, folder_o, rep = go("overlap", HEAD, overlap_rows)
check("a Place ID that is already a Charlotte lead STOPS the validator (exit 2) and no list is written",
      code == 2 and "already Charlotte leads" in rep and not os.path.exists(os.path.join(folder_o, "atlanta_leads_filtered.csv")))
check("the stop message does not print business names", "Charlotte Shop Here" not in rep and "Some Other Charlotte Shop" not in rep)
check("the overlap stop still shows the Place ID of a qualified row", "ChIJCHARLOTTE00000000002" in rep)
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
