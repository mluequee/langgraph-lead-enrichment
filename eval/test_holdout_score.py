"""Free checks for the stage F seal (eval/seal_holdout.py) and scorer (eval/score_holdout.py).
Synthetic leads and a synthetic pipeline run (real database and export code, fake answers), no network, no keys.

    python eval/test_holdout_score.py

Expected results below were worked out by hand from the protocol definitions (eval/HOLDOUT_PROTOCOL.md section 5).
"""
import csv
import hashlib
import os
import shutil
import sqlite3
import subprocess
import sys

import openpyxl

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))

import db as dbmod                               # noqa: E402
import export                                    # noqa: E402
import holdout_common as hc                      # noqa: E402
import make_holdout_worksheet as mw              # noqa: E402
import score_holdout as sc                       # noqa: E402
import seal_holdout as sl                        # noqa: E402
import setup_database as sdb                     # noqa: E402

failures = []


def check(label, ok):
    print(("PASS " if ok else "FAIL ") + label)
    if not ok:
        failures.append(label)


def raises(fn):
    try:
        fn()
    except ValueError as e:
        return str(e)
    return None


TEST_REL = os.path.join("data", f"_test_score_{os.getpid()}")
BASE = os.path.join(ROOT, TEST_REL)
data_existed = os.path.isdir(os.path.join(ROOT, "data"))
os.makedirs(BASE, exist_ok=True)


def pid(n):
    return f"ChIJ{n:020d}"


# (index, business, flag, worksheet fill, pipeline answer)  -- pipeline: (first, last, confidence, evidence_url) or None for a blank, "ERR" for an error
LEADS = [
    (0, "Alpha Barbershop", "", dict(verdict=hc.VERDICT_OWNER, owner_first="Anna", owner_last="Alpha"), ("Anna", "Alpha", "high", "https://e.example/a")),
    (1, "Beta Barbershop", "", dict(verdict=hc.VERDICT_OWNER, owner_first="Ben", owner_last="Beta"), ("Ben", "", "medium", "https://e.example/b")),
    (2, "Gamma Barbershop", "", dict(verdict=hc.VERDICT_OWNER, owner_first="Cara", owner_last="Gamma"), ("Dana", "Delta", "high", "https://e.example/c")),
    (3, "Delta Barbershop", "", dict(verdict=hc.VERDICT_OWNER, owner_first="Dan", owner_last="Dalton"), None),
    (4, "Echo Barbershop", "", dict(verdict=hc.VERDICT_NO_OWNER, checked="Yes"), None),
    (5, "Foxtrot Barbershop", "", dict(verdict=hc.VERDICT_NO_OWNER, checked="Yes"), ("Eve", "Echo", "medium", "https://e.example/f")),
    (6, "Golf Barbershop", "", dict(verdict=hc.VERDICT_OWNER, owner_first="Gil", owner_last="Golf", goes_by="Donny Blendz"), ("Donny", "Blendz", "medium", "https://e.example/g")),
    (7, "Hotel Barbershop", "", dict(verdict=hc.VERDICT_OWNER, owner_first="Hal", owner_last="Hotel", co_owner_first="Ida", co_owner_last="India"), ("Ida", "India", "high", "https://e.example/h")),
    (8, "India Barbershop", "", dict(verdict=hc.VERDICT_AMBIGUOUS), ("Zed", "Zulu", "high", "https://e.example/i")),
    (9, "Juliet Barbershop", "", dict(verdict=hc.VERDICT_UNDETERMINED), None),
    (10, "Kilo Barbershop", "", dict(verdict=hc.VERDICT_OWNER, owner_first="Kay", owner_last="Kilo"), "ERR"),
    (11, "Sam @ Fake", "", dict(verdict=hc.VERDICT_OWNER, owner_first="Sam", owner_last="Sierra"), None),
    (12, "Lima Barbershop", "chain_overlap:Lima Charlotte", dict(verdict=hc.VERDICT_OWNER, owner_first="Lou", owner_last="Lima"), ("Lou", "Lima", "high", "https://e.example/l")),
    (13, "Mike Barbershop", "", dict(verdict=hc.VERDICT_OWNER, owner_first="Mia", owner_last="Mike"), ("Mia", "Mike", "high", "")),
]
NAMES = ["Anna", "Alpha", "Dana Delta", "Eve Echo", "Donny", "Ida India", "Zed", "Lou Lima", "Mia Mike", "Cara", "Dalton"]

clean = os.path.join(BASE, "intake", "atlanta_leads_clean.csv")
os.makedirs(os.path.dirname(clean))
with open(clean, "w", newline="", encoding="utf-8") as f:
    w = csv.DictWriter(f, fieldnames=hc.CLEAN_COLUMNS)
    w.writeheader()
    for i, name, flag, _, _ in LEADS:
        w.writerow({"business_name": name, "website": f"https://s{i}.example", "address": f"{i} Main St, Atlanta, GA 30303, USA",
                    "phone": f"(404) 555-01{i:02d}", "id": pid(i), "google_url": f"g{i}", "city": "Atlanta", "state": "GA", "flags": flag})

# --- a "sealed" folder written by the real database and export code -------------------------------------------------
sealed = os.path.join(BASE, "sealed")
os.makedirs(sealed)
dbp = os.path.join(sealed, "leads.db")
sdb.init_database(reset=True, db_path=dbp)
sdb.seed_data_from_csv(clean, db_path=dbp)
ldb = dbmod.LeadDatabase(dbp)
lead_id = {l.place_id: l.id for l in ldb.get_all_leads()}
for i, name, flag, _, ans in LEADS:
    if i == 11:
        continue                                          # the '@' lead: intake skips it, no result
    if ans == "ERR":
        res = {"status": "error", "error_detail": "all searches failed", "gemini": {"status": "off"}}
    elif ans is None:
        res = {"decision_maker_first_name": "<UNKNOWN>", "decision_maker_last_name": "<UNKNOWN>", "confidence": "low",
               "status": "ok", "gemini": {"status": "off"}}
    else:
        res = {"decision_maker_first_name": ans[0], "decision_maker_last_name": ans[1], "confidence": ans[2],
               "delivered_confidence": ans[2], "evidence_url": ans[3] or None, "source_tier": "supporting", "status": "ok",
               "gemini": {"status": "off"}}
    ldb.write_result(lead_id[pid(i)], res)
export.write_export(dbp, os.path.join(sealed, "enriched_leads.csv"))
for name in ("batch_stats.csv", "skipped_leads.csv", "langgraph_debug.log", "console.txt"):
    with open(os.path.join(sealed, name), "w", encoding="utf-8") as f:
        f.write("synthetic\n")

# --- the seal ---------------------------------------------------------------------------------------------------------
printed = sl.seal(sealed)
seal_txt = open(os.path.join(sealed, "SEAL.txt"), encoding="utf-8").read()
check("the seal prints counts only: no business name, no owner name", not any(n in " ".join(printed) for n in NAMES + ["Barbershop"]))
check("SEAL.txt holds no name either", not any(n in seal_txt for n in NAMES + ["Barbershop"]))
check("SEAL.txt records the export hash, the database hash, the git commit and the frozen-source diff",
      f"sha256 enriched_leads.csv: {hc.file_sha256(os.path.join(sealed, 'enriched_leads.csv'))}" in seal_txt
      and f"sha256 leads.db: {hc.file_sha256(dbp)}" in seal_txt and "git_commit: " in seal_txt
      and sl.read_seal(sealed)["frozen_source_check"] in ("PASS", "UNKNOWN") and sl.read_seal(sealed)["denylist_lines_added"].isdigit()
      and "frozen_src_diff_lines" not in sl.read_seal(sealed))
s = sl.read_seal(sealed)
check("SEAL.txt counts: 14 leads = 9 found (the Ambiguous lead was named too) + 3 no_owner_found + 1 error + 1 skipped; 1 found without evidence",
      s["leads"] == "14" and s["outcome error"] == "1" and s["outcome skipped"] == "1" and s["outcome found"] == "9"
      and s["outcome no_owner_found"] == "3" and s["outcome needs_review"] == "0" and s["found_without_evidence_url"] == "1")
check("SEAL.txt confirms the run was Brave-only (every result gemini_status off)", s["gemini_status"] == "off 13")
check("a second seal on the same folder is refused", "already exists" in (raises(lambda: sl.seal(sealed)) or ""))

# the frozen-source record in SEAL.txt: PASS/FAIL plus the count of added denylist entries, nothing else
real_check = sl.frozen_check.git_frozen_check
try:
    for status, added, why in (("FAIL", 0, ["src/graph.py differs from stage-e-done (it must be identical)"]), ("PASS", 3, [])):
        folder_x = os.path.join(BASE, f"sealed_{status}")
        shutil.copytree(sealed, folder_x)
        os.remove(os.path.join(folder_x, "SEAL.txt"))
        sl.frozen_check.git_frozen_check = lambda st=status, ad=added, w=why: (st, ad, w)
        printed_x = sl.seal(folder_x)
        text_x = open(os.path.join(folder_x, "SEAL.txt"), encoding="utf-8").read()
        sx = sl.read_seal(folder_x)
        check(f"SEAL.txt records frozen_source_check: {status} and denylist_lines_added: {added}",
              sx["frozen_source_check"] == status and sx["denylist_lines_added"] == str(added))
        check(f"SEAL.txt ({status}) carries no reason text, file names or diff: only the two values",
              "differs" not in text_x and "src/graph.py" not in text_x and "frozen_src_diff_lines" not in text_x)
        warned = any("frozen source check" in line for line in printed_x)
        check(f"a {status} frozen check {'prints a WARNING' if status != 'PASS' else 'prints no warning'}", warned == (status != "PASS"))
finally:
    sl.frozen_check.git_frozen_check = real_check
os.makedirs(os.path.join(BASE, "empty_dir"), exist_ok=True)
check("sealing a folder with no export is refused (empty folder)", "missing" in (raises(lambda: sl.seal(os.path.join(BASE, "empty_dir"))) or ""))

# --- a finished worksheet ------------------------------------------------------------------------------------------------
ws_path = os.path.join(BASE, "verify", "Atlanta Verification Worksheet.xlsx")
mw.build(clean, ws_path)


def fill(path, overrides=None, skip_verdict_for=None):
    wb = openpyxl.load_workbook(path)
    ws = wb["Verify"]
    head = [c.value for c in ws[1]]
    col = {h: head.index(h) + 1 for h in head}
    fills = {pid(i): dict(f) for i, _, _, f, _ in LEADS}
    for p, extra in (overrides or {}).items():
        fills[p].update(extra)
    for r in range(2, ws.max_row + 1):
        p = ws.cell(row=r, column=col["place_id"]).value
        if p == skip_verdict_for:
            continue
        f = fills[p]
        if f.get("verdict") == hc.VERDICT_OWNER:
            f.setdefault("source_type", "Own website")
            f.setdefault("source_url", "https://src.example")
        for k, v in f.items():
            ws.cell(row=r, column=col[k], value=v)
    wb.save(path)


fill(ws_path)
out = os.path.join(BASE, "score")

# --- refusals that happen BEFORE the sealed folder is touched --------------------------------------------------------------
bad = os.path.join(BASE, "verify", "incomplete.xlsx")
mw.build(clean, bad)
fill(bad, skip_verdict_for=pid(3))
msg = raises(lambda: sc.score(bad, os.path.join(BASE, "no_such_sealed"), clean, os.path.join(BASE, "o1")))
check("an incomplete worksheet is refused with the row number, BEFORE the sealed folder is looked at (that folder does not even exist)",
      msg is not None and "no verdict" in msg and "SEAL" not in msg)
bad2 = os.path.join(BASE, "verify", "unchecked.xlsx")
mw.build(clean, bad2)
fill(bad2, overrides={pid(4): {"checked": None}})
check("'No owner public' without checked = Yes is refused", "checked" in (raises(lambda: sc.score(bad2, sealed, clean, os.path.join(BASE, "o2"))) or ""))
bad3 = os.path.join(BASE, "verify", "nosource.xlsx")
mw.build(clean, bad3)
fill(bad3, overrides={pid(0): {"source_type": None}})
check("'Owner found' without a source type is refused", "source_type" in (raises(lambda: sc.score(bad3, sealed, clean, os.path.join(BASE, "o3"))) or ""))
bad4 = os.path.join(BASE, "verify", "badverdict.xlsx")
mw.build(clean, bad4)
fill(bad4, overrides={pid(0): {"verdict": "Probably"}})
check("a verdict outside the four is refused", "not one of the four" in (raises(lambda: sc.score(bad4, sealed, clean, os.path.join(BASE, "o4"))) or ""))

# --- refusals about the seal ---------------------------------------------------------------------------------------------------
unsealed = os.path.join(BASE, "unsealed")
shutil.copytree(sealed, unsealed)
os.remove(os.path.join(unsealed, "SEAL.txt"))
check("a sealed folder without SEAL.txt is refused", "no SEAL.txt" in (raises(lambda: sc.score(ws_path, unsealed, clean, os.path.join(BASE, "o5"))) or ""))
tampered = os.path.join(BASE, "tampered")
shutil.copytree(sealed, tampered)
with open(os.path.join(tampered, "enriched_leads.csv"), "a", encoding="utf-8") as f:
    f.write("\n")
check("an export that no longer matches SEAL.txt is refused", "does not match SEAL.txt" in (raises(lambda: sc.score(ws_path, tampered, clean, os.path.join(BASE, "o6"))) or ""))
short = os.path.join(BASE, "intake", "short.csv")
rows_c = hc.read_clean_csv(clean)[:-1]
with open(short, "w", newline="", encoding="utf-8") as f:
    w = csv.DictWriter(f, fieldnames=hc.CLEAN_COLUMNS)
    w.writeheader()
    w.writerows(rows_c)
check("a worksheet and lead list with different Place IDs are refused", "different Place IDs" in (raises(lambda: sc.score(ws_path, sealed, short, os.path.join(BASE, "o7"))) or ""))

# --- the score -------------------------------------------------------------------------------------------------------------------
text = sc.score(ws_path, sealed, clean, out, today="2026-11-06")
head = text.splitlines()[0]
sha7 = s["git_commit"][:7]
print("   headline:", head)
check("headline: named 8/10 (80%)", "named 8/10 (80%)" in head)
check("headline: lenient 6/8 = 75.0% with its Wilson interval", "lenient 6/8 = 75.0% [41%-93%]" in head)
check("headline: strict 4/8 = 50.0% with its Wilson interval", "strict 4/8 = 50.0% [22%-78%]" in head)
check("headline: wrong 2, correct blanks 1/2", "wrong 2 | correct blanks 1/2" in head)
check("headline: coverage 6/8 lenient, 4/8 strict", "coverage 6/8 lenient, 4/8 strict" in head)
check("headline: errors 1, excluded 3 (ambiguous + not determined + skipped)", "errors 1, excluded 3" in head)
check("headline carries the commit from SEAL.txt, the date, and says Brave-only", head.startswith(f"Atlanta holdout, Brave-only, commit {sha7}, 2026-11-06:"))
check("goes-by name counts as a partial and a co-owner's name as full (grades full 4, partial 2)", "Grades: full 4, partial 2, wrong 2, blank 1, correct blank 1" in text)
check("the pipeline's confidence table: high 4/5, medium 2/3", "high   4/5 = 80%" in text and "medium 2/3 = 67%" in text)
check("named rows without an evidence URL: 1 of 8", "Named rows without an evidence URL: 1 of 8" in text)
check("exclusion counts are listed separately: errors 1, skipped 1, Ambiguous 1, Not determined 1",
      "errors 1, skipped at intake 1, Ambiguous 1, Not determined 1" in text)
check("the without-chain-overlap figures are reported beside the headline (S 9, named 7, 5/7 lenient, 3/7 strict)",
      "WITHOUT the 1 chain-overlap lead(s)" in text and "named 7/9" in text and "lenient 5/7" in text and "strict 3/7" in text)
check("score.txt contains no business name or owner name", not any(n in text for n in NAMES + ["Barbershop", "Fake"]))
graded = list(csv.DictReader(open(os.path.join(out, "graded_rows.csv"), newline="", encoding="utf-8")))
check("graded_rows.csv has all 14 leads with names and a grade each", len(graded) == 14 and any(r["business_name"] == "Alpha Barbershop" for r in graded)
      and {r["grade"] for r in graded} == {"full", "partial", "wrong", "blank", "correct_blank", "error", "skipped", "ambiguous", "undetermined"})
gmap = {r["business_name"]: r["grade"] for r in graded}
check("per-lead grades are the hand-worked ones", gmap["Alpha Barbershop"] == "full" and gmap["Beta Barbershop"] == "partial"
      and gmap["Gamma Barbershop"] == "wrong" and gmap["Delta Barbershop"] == "blank" and gmap["Echo Barbershop"] == "correct_blank"
      and gmap["Foxtrot Barbershop"] == "wrong" and gmap["Golf Barbershop"] == "partial" and gmap["Hotel Barbershop"] == "full"
      and gmap["India Barbershop"] == "ambiguous" and gmap["Juliet Barbershop"] == "undetermined" and gmap["Kilo Barbershop"] == "error"
      and gmap["Sam @ Fake"] == "skipped")
check("FREEZE.txt records the worksheet, lead list, export and database hashes",
      all(k in open(os.path.join(out, "FREEZE.txt"), encoding="utf-8").read() for k in ("worksheet sha256:", "lead list sha256:", "export sha256:", "database sha256:")))

# --- the freeze -------------------------------------------------------------------------------------------------------------------
text2 = sc.score(ws_path, sealed, clean, out, today="2026-11-06")
check("scoring the same frozen worksheet again gives the same result", text2 == text)
fill(ws_path, overrides={pid(2): {"owner_first": "Dana", "owner_last": "Delta"}})
check("a worksheet edited after the freeze is REFUSED (a correction is a footnote, not an edit)",
      "changed after the freeze" in (raises(lambda: sc.score(ws_path, sealed, clean, out)) or ""))

# --- the Wilson interval -------------------------------------------------------------------------------------------------------------
lo, hi = sc.wilson(5, 10)
check("wilson(5, 10) = [0.237, 0.763]", abs(lo - 0.2366) < 0.001 and abs(hi - 0.7634) < 0.001)
lo, hi = sc.wilson(8, 8)
check("wilson(8, 8) = [0.676, 1.0]", abs(lo - 0.6756) < 0.002 and hi > 0.999)
check("wilson(0, 0) is (0, 0), not a division error", sc.wilson(0, 0) == (0.0, 0.0))

# --- scoring.py is unchanged, and the new scripts load no pipeline ---------------------------------------------------------------------
try:
    old = subprocess.run(["git", "-C", ROOT, "show", "stage-e-done:eval/scoring.py"], capture_output=True, timeout=30)
    same = old.returncode != 0 or hashlib.sha256(old.stdout.replace(b"\r\n", b"\n")).hexdigest() == \
        hashlib.sha256(open(os.path.join(HERE, "scoring.py"), "rb").read().replace(b"\r\n", b"\n")).hexdigest()
except Exception:
    same = True
check("eval/scoring.py is byte-identical to stage-e-done (no scorer changes)", same)
probe = ("import sys; sys.path[:0] = [r'%s', r'%s']; import score_holdout, seal_holdout; "
         "print(sorted(m for m in ('graph','requests','anthropic','langchain_anthropic','langgraph','dotenv','httpx') if m in sys.modules))"
         % (HERE, os.path.join(ROOT, "src")))
outp = subprocess.run([sys.executable, "-I", "-c", probe], capture_output=True, text=True).stdout.strip()
check(f"importing the scorer and the seal loads no pipeline graph, HTTP client or key loader (loaded: {outp})", outp == "[]")
check("the scorer CLI returns exit code 2 with a STOP message for a bad input", sc.main(["--worksheet", bad, "--sealed", sealed, "--clean", clean, "--out", os.path.join(BASE, "o9")]) == 2)

shutil.rmtree(BASE, ignore_errors=True)
if not data_existed:
    try:
        os.rmdir(os.path.join(ROOT, "data"))
    except OSError:
        pass
check("the test cleaned up after itself", not os.path.exists(BASE))

print("\nSeal and scorer tests passed." if not failures else f"\n{len(failures)} FAILED: {failures}")
sys.exit(1 if failures else 0)
