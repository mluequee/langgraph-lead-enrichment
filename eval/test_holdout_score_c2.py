"""Free checks for eval/score_holdout_c2.py (the scorer for the Gemini-on Atlanta rerun).
Synthetic leads, a synthetic Brave-only run and a synthetic Gemini-on run (real database, export and seal code, fake
answers), in a throwaway folder under <repo>/data/_test_score_c2_<pid>. No real Atlanta file is read or written,
no network, no keys.

    python eval/test_holdout_score_c2.py

Expected figures below were worked out by hand from the protocol definitions, not copied from the script's output.
"""
import contextlib
import csv
import io
import json
import os
import re
import shutil
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
import score_holdout as sh                       # noqa: E402
import score_holdout_c2 as c2                    # noqa: E402
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


TEST_REL = os.path.join("data", f"_test_score_c2_{os.getpid()}")
BASE = os.path.join(ROOT, TEST_REL)
data_existed = os.path.isdir(os.path.join(ROOT, "data"))
os.makedirs(BASE, exist_ok=True)


def pid(n):
    return f"ChIJ{n:020d}"


def G(status, first=None, last=None, error=None, cost=0.05, url=None):
    return {"status": status, "first": first, "last": last, "error": error, "cost_usd": cost, "seconds": 3.0,
            "confidence": "high" if first else None, "evidence": {"evidence_url": url}}


OWN = hc.VERDICT_OWNER
NOO = hc.VERDICT_NO_OWNER
CANDS = json.dumps([{"source": "brave", "first": "Fred", "last": "Foster"}, {"source": "gemini", "first": "Dan", "last": "Dalton"}])
# i, business, flags, worksheet fill, delivered (first, last, confidence, url) or None, Brave answer, Gemini dict, reconcile case, candidates
LEADS = [
    dict(i=0, n="Alpha Barbershop", ws=dict(verdict=OWN, owner_first="Anna", owner_last="Alpha"), d=("Anna", "Alpha", "high", "https://e.example/a"),
         b=("Anna", "Alpha"), g=G("ok", "Anna", "Alpha", url="https://g.example/a"), case="agree"),
    dict(i=1, n="Beta Barbershop", ws=dict(verdict=OWN, owner_first="Ben", owner_last="Beta"), d=("Ben", "Beta", "medium", "https://e.example/b"),
         b=("Ben", "Beta"), g=G("ok"), case="brave_only"),
    dict(i=2, n="Gamma Barbershop", ws=dict(verdict=OWN, owner_first="Cara", owner_last="Gamma"), d=("Cara", "Gamma", "medium", "https://g.example/c"),
         b=None, g=G("ok", "Cara", "Gamma", url="https://g.example/c"), case="gemini_only"),
    dict(i=3, n="Delta Barbershop", ws=dict(verdict=OWN, owner_first="Dan", owner_last="Dalton"), d=None,
         b=("Fred", "Foster"), g=G("ok", "Dan", "Dalton", url="https://g.example/d"), case="conflict", cands=CANDS),
    dict(i=4, n="Echo Barbershop", ws=dict(verdict=NOO, checked="Yes"), d=None, b=None, g=G("ok"), case="both_blank"),
    dict(i=5, n="Foxtrot Barbershop", ws=dict(verdict=NOO, checked="Yes"), d=("Eve", "Echo", "high", "https://e.example/f"),
         b=("Eve", "Echo"), g=G("ok", "Eve", "Echo", url="https://g.example/f"), case="agree"),
    dict(i=6, n="Golf Barbershop", ws=dict(verdict=OWN, owner_first="Gil", owner_last="Golf", goes_by="Donny Blendz"), d=None,
         b=None, g=G("error", error="timeout after 180s; 2 attempt(s), 240s", cost=0.02), case="gemini_unavailable"),
    dict(i=7, n="Hotel Barbershop", ws=dict(verdict=OWN, owner_first="Hal", owner_last="Hotel", co_owner_first="Ida", co_owner_last="India"),
         d=("Hal", "Hotel", "high", "https://e.example/h"), b=("Hal", "Hotel"),
         g=G("error", error="HTTP 503: The model is overloaded", cost=0.02), case="gemini_unavailable"),
    dict(i=8, n="India Barbershop", ws=dict(verdict=hc.VERDICT_AMBIGUOUS), d=("Zed", "Zulu", "high", "https://e.example/i"),
         b=("Zed", "Zulu"), g=G("ok", "Zed", "Zulu", url="https://g.example/i"), case="agree"),
    dict(i=9, n="Juliet Barbershop", ws=dict(verdict=hc.VERDICT_UNDETERMINED), d=None, b=None, g=G("ok"), case="both_blank"),
    dict(i=10, n="Kilo Barbershop", ws=dict(verdict=OWN, owner_first="Kay", owner_last="Kilo"), d="ERR", b=None,
         g=G("error", error="HTTP 402: credits depleted; 1 attempt(s), 1s", cost=0.02), case=None),
    dict(i=11, n="Sam @ Fake", ws=dict(verdict=OWN, owner_first="Sam", owner_last="Sierra"), d=None, b=None, g=None, case=None),
    dict(i=12, n="Lima Barbershop", flag="chain_overlap:Lima Charlotte", ws=dict(verdict=OWN, owner_first="Lou", owner_last="Lima"),
         d=("Lou", "Lima", "medium", "https://e.example/l"), b=("Lou", "Lima"), g=G("ok"), case="brave_only"),
    dict(i=13, n="Mike Barbershop", ws=dict(verdict=OWN, owner_first="Mia", owner_last="Mike"), d=("Mia", "", "medium", ""),
         b=("Mia", ""), g=G("ok", "Mia", ""), case="agree"),
    dict(i=14, n="November Barbershop", ws=dict(verdict=OWN, owner_first="Nell", owner_last="November"),
         d=("Nell", "November", "high", "https://e.example/n"), b=("Nell", "November"),
         g=G("disabled", error="Gemini disabled for this batch after: HTTP 402: credits depleted", cost=0.0), case="gemini_unavailable"),
]
NAME_RE = re.compile(r"\b(Anna|Alpha|Ben|Beta|Cara|Gamma|Fred|Foster|Dan|Dalton|Eve|Echo|Hal|Hotel|Ida|India|Lou|Lima|Mia|Mike|Nell|November|"
                     r"Zed|Zulu|Kay|Kilo|Gil|Golf|Donny|Blendz|Delta|Foxtrot|Juliet|Barbershop|Fake)\b")

clean = os.path.join(BASE, "intake", "atlanta_leads_clean.csv")
os.makedirs(os.path.dirname(clean))
with open(clean, "w", newline="", encoding="utf-8") as f:
    w = csv.DictWriter(f, fieldnames=hc.CLEAN_COLUMNS)
    w.writeheader()
    for t in LEADS:
        w.writerow({"business_name": t["n"], "website": f"https://s{t['i']}.example", "address": f"{t['i']} Main St, Atlanta, GA 30303, USA",
                    "phone": f"(404) 555-01{t['i']:02d}", "id": pid(t["i"]), "google_url": f"g{t['i']}", "city": "Atlanta", "state": "GA",
                    "flags": t.get("flag", "")})

LOG = ("2026-11-21 10:00:00 INFO COST TRACKING:\n  Haiku tokens: 1,000,000 in + 100,000 out\n  Sonnet tokens: 200,000 in + 20,000 out\n"
       "  Total tokens: 1,320,000\n  Brave API calls: 400\n  Estimated cost: $4.10\n")


def build_sealed(folder, mode):
    """A sealed folder written by the real database, export and seal code. mode 'brave' = Gemini off, 'c2' = Gemini on."""
    os.makedirs(folder)
    dbp = os.path.join(folder, "leads.db")
    sdb.init_database(reset=True, db_path=dbp)
    sdb.seed_data_from_csv(clean, db_path=dbp)
    ldb = dbmod.LeadDatabase(dbp)
    lead_id = {l.place_id: l.id for l in ldb.get_all_leads()}
    for t in LEADS:
        if t["i"] == 11:
            continue                                         # the '@' lead: intake skips it, no result
        gem = t["g"] if mode == "c2" else {"status": "off"}
        if t["d"] == "ERR":
            res = {"status": "error", "error_detail": "all searches failed", "gemini": gem}
        else:
            d = t["d"] if mode == "c2" else ((t["b"] + ("medium", f"https://e.example/b{t['i']}")) if t["b"] else None)
            brave = t["b"] or (None, None)
            base = {"status": "ok", "gemini": gem, "reconcile_case": t["case"] if mode == "c2" else "gemini_off",
                    "brave_answer": {"first": brave[0], "last": brave[1]}}
            if d is None:
                res = dict(base, decision_maker_first_name="<UNKNOWN>", decision_maker_last_name="<UNKNOWN>", confidence="low")
            else:
                res = dict(base, decision_maker_first_name=d[0], decision_maker_last_name=d[1], confidence=d[2], delivered_confidence=d[2],
                           evidence_url=d[3] or None, source_tier="supporting")
            if mode == "c2" and t.get("cands"):
                res["review_candidates"] = t["cands"]
        ldb.write_result(lead_id[pid(t["i"])], res)
    export.write_export(dbp, os.path.join(folder, "enriched_leads.csv"))
    for name in ("batch_stats.csv", "skipped_leads.csv", "console.txt"):
        with open(os.path.join(folder, name), "w", encoding="utf-8") as f:
            f.write("synthetic\n")
    with open(os.path.join(folder, "langgraph_debug.log"), "w", encoding="utf-8") as f:
        f.write(LOG)
    sl.seal(folder)


def fill(path, overrides=None):
    wb = openpyxl.load_workbook(path)
    ws = wb["Verify"]
    head = [c.value for c in ws[1]]
    col = {h: head.index(h) + 1 for h in head}
    fills = {pid(t["i"]): dict(t["ws"]) for t in LEADS}
    for p, extra in (overrides or {}).items():
        fills[p].update(extra)
    for r in range(2, ws.max_row + 1):
        f = fills[ws.cell(row=r, column=col["place_id"]).value]
        if f.get("verdict") == OWN:
            f.setdefault("source_type", "Own website")
            f.setdefault("source_url", "https://src.example")
        for k, v in f.items():
            ws.cell(row=r, column=col[k], value=v)
    wb.save(path)


brave_sealed = os.path.join(BASE, "sealed")
c2_sealed = os.path.join(BASE, "c2")
build_sealed(brave_sealed, "brave")
build_sealed(c2_sealed, "c2")
ws_path = os.path.join(BASE, "verify", "worksheet.xlsx")
mw.build(clean, ws_path)
fill(ws_path)
score_dir = os.path.join(BASE, "score")
sh.score(ws_path, brave_sealed, clean, score_dir, today="2026-11-06")           # the Brave-only score writes the reference FREEZE.txt
check("the fixture's Brave-only score wrote a FREEZE.txt with the worksheet hash",
      f"worksheet sha256: {hc.file_sha256(ws_path)}" in open(os.path.join(score_dir, "FREEZE.txt"), encoding="utf-8").read())

out = os.path.join(BASE, "c2_score")
KW = dict(score_dir=score_dir, diff_fn=lambda rev: "empty", today="2026-11-21")

# --- refusals about --out: paths only, before ANYTHING is read (the worksheet below does not even exist) -----------------------
nowhere = os.path.join(BASE, "no_such_worksheet.xlsx")
real_score = os.path.join(ROOT, "data", "atlanta", "score")
real_sealed = os.path.join(ROOT, "data", "atlanta", "sealed")
real_c2 = os.path.join(ROOT, "data", "atlanta", "c2")
pre_exists = {p: os.path.exists(p) for p in (real_c2, os.path.join(ROOT, "data", "atlanta", "c2_score"))}
for label, target, word in (("the recorded score folder data/atlanta/score", real_score, "Brave-only score"),
                            ("a folder inside data/atlanta/score", os.path.join(real_score, "x"), "Brave-only score"),
                            ("the Brave-only sealed folder data/atlanta/sealed", real_sealed, "sealed folder"),
                            ("the Gemini-on sealed folder data/atlanta/c2", real_c2, "sealed folder"),
                            ("a folder inside the folder being scored", os.path.join(c2_sealed, "sub"), "sealed folder"),
                            ("the folder being scored itself", c2_sealed, "sealed folder")):
    msg = raises(lambda t=target: c2.score_c2(nowhere, c2_sealed, clean, t, **{**KW, "score_dir": real_score})) or ""
    check(f"--out = {label} is refused before the worksheet is opened", word in msg and "No such file" not in msg)
msg = raises(lambda: c2.score_c2(nowhere, c2_sealed, clean, os.path.join(real_score, "x"), today="2026-11-21", diff_fn=lambda rev: "empty")) or ""
check("the same refusal with the default score folder argument (nothing passed)", "Brave-only score" in msg)
os.makedirs(os.path.join(BASE, "holds_a_seal", "inner"))
open(os.path.join(BASE, "holds_a_seal", "SEAL.txt"), "w").close()
check("--out under any folder that holds a SEAL.txt is refused",
      "SEAL.txt" in (raises(lambda: c2.score_c2(nowhere, c2_sealed, clean, os.path.join(BASE, "holds_a_seal", "inner", "o"), **KW)) or ""))
os.makedirs(os.path.join(BASE, "already_there"))
check("--out that already exists is refused", "already exists" in (raises(lambda: c2.score_c2(nowhere, c2_sealed, clean, os.path.join(BASE, "already_there"), **KW)) or ""))
check("--out outside <repo>/data is refused", "inside" in (raises(lambda: c2.score_c2(nowhere, c2_sealed, clean, os.path.join(ROOT, "elsewhere"), **KW)) or ""))
check("no real Atlanta folder was created or touched by those refusals",
      all(os.path.exists(p) == was for p, was in pre_exists.items()))

# --- refusals about the frozen worksheet and the inputs ---------------------------------------------------------------------------
bad = os.path.join(BASE, "verify", "incomplete.xlsx")
mw.build(clean, bad)
fill(bad)
wb = openpyxl.load_workbook(bad)
wb["Verify"].cell(row=2, column=hc.VERIFY_COLUMNS.index("verdict") + 1).value = None       # cell(..., value=None) would not clear it
wb.save(bad)
msg = raises(lambda: c2.score_c2(bad, os.path.join(BASE, "no_such_sealed"), clean, os.path.join(BASE, "o1"), **KW)) or ""
check("an incomplete worksheet is refused before the sealed folder is looked at (it does not exist)", "no verdict" in msg and "SEAL" not in msg)
edited = os.path.join(BASE, "verify", "edited.xlsx")
mw.build(clean, edited)
fill(edited, overrides={pid(2): {"owner_first": "Dana"}})
msg = raises(lambda: c2.score_c2(edited, os.path.join(BASE, "no_such_sealed"), clean, os.path.join(BASE, "o2"), **KW)) or ""
check("a complete but different worksheet (hash differs from the Brave-only FREEZE.txt) stops BEFORE the sealed folder is read",
      "differs from the Brave-only FREEZE.txt" in msg and "SEAL" not in msg and not os.path.exists(os.path.join(BASE, "o2")))
os.makedirs(os.path.join(BASE, "empty_score"))
check("no Brave-only FREEZE.txt to compare with: refused",
      "no FREEZE.txt" in (raises(lambda: c2.score_c2(ws_path, c2_sealed, clean, os.path.join(BASE, "o3"), **{**KW, "score_dir": os.path.join(BASE, "empty_score")})) or ""))
short = os.path.join(BASE, "intake", "short.csv")
with open(short, "w", newline="", encoding="utf-8") as f:
    w = csv.DictWriter(f, fieldnames=hc.CLEAN_COLUMNS)
    w.writeheader()
    w.writerows(hc.read_clean_csv(clean)[:-1])
check("a different lead list (hash differs from the Brave-only FREEZE.txt) is refused",
      "lead list's hash differs" in (raises(lambda: c2.score_c2(ws_path, c2_sealed, short, os.path.join(BASE, "o4"), **KW)) or ""))
unsealed = os.path.join(BASE, "unsealed")
shutil.copytree(c2_sealed, unsealed)
os.remove(os.path.join(unsealed, "SEAL.txt"))
check("a sealed folder without SEAL.txt is refused", "no SEAL.txt" in (raises(lambda: c2.score_c2(ws_path, unsealed, clean, os.path.join(BASE, "o5"), **KW)) or ""))
tampered = os.path.join(BASE, "tampered")
shutil.copytree(c2_sealed, tampered)
with open(os.path.join(tampered, "enriched_leads.csv"), "a", encoding="utf-8") as f:
    f.write("\n")
check("an export that no longer matches SEAL.txt is refused", "does not match SEAL.txt" in (raises(lambda: c2.score_c2(ws_path, tampered, clean, os.path.join(BASE, "o6"), **KW)) or ""))
check("a Brave-only folder (every gemini_status off) is refused as not the Gemini-on rerun",
      "Brave-only run" in (raises(lambda: c2.score_c2(ws_path, brave_sealed, clean, os.path.join(BASE, "o7"), **KW)) or ""))
check("nothing was written by any refusal", not any(os.path.exists(os.path.join(BASE, f"o{n}")) for n in range(1, 8)) and not os.path.exists(out))

# --- the score ----------------------------------------------------------------------------------------------------------------------
text = c2.score_c2(ws_path, c2_sealed, clean, out, **KW)
print(text)
sha7 = sl.read_seal(c2_sealed)["git_commit"][:7]
blocks = text.split("WITHOUT the")
allb, nochain = blocks[0], "WITHOUT the" + blocks[1]
L = c2.DEFAULT_LABEL
check("the default label is 'Atlanta holdout, c2 combined, Gemini ON' and heads the output", text.startswith(L))

d_all = f"Delivered (combined): {L}, commit {sha7}, 2026-11-21: named 8/11 (73%) | lenient 7/8 = 87.5% [53%-98%] | strict 6/8 = 75.0% [41%-93%] | wrong 1 | correct blanks 1/2 | coverage 7/9 lenient, 6/9 strict | errors 1, excluded 3"
check("delivered, all leads: named 8/11, lenient 7/8 and strict 6/8 with Wilson intervals, wrong 1, correct blanks 1/2, errors 1, excluded 3", d_all in allb)
check("Brave alone (same run, all): named 8/11, lenient 6/8 = 75.0% [41%-93%], strict 5/8 = 62.5% [31%-86%], wrong 2, correct blanks 1/2",
      "named 8/11 (73%) | lenient 6/8 = 75.0% [41%-93%] | strict 5/8 = 62.5% [31%-86%] | wrong 2 | correct blanks 1/2 | coverage 6/9 lenient, 5/9 strict" in allb)
check("Gemini alone (all): 8 rows with an answer, named 5, lenient 4/5 = 80.0% [38%-96%], strict 3/5 = 60.0% [23%-88%], wrong 1, correct blanks 1/2",
      "named 5/8" in allb and "lenient 4/5 = 80.0% [38%-96%] | strict 3/5 = 60.0% [23%-88%] | wrong 1 | correct blanks 1/2 | coverage 4/6 lenient, 3/6 strict" in allb)
check("Gemini alone coverage against ALL 9 owner-bearing rows (unanswered rows are not found): lenient 4/9, strict 3/9",
      "coverage against ALL 9 owner-bearing scored rows (an unanswered row is not found): lenient 4/9, strict 3/9" in allb)
check("Gemini gave no answer on 3 scored rows (timeout, 503, disabled), left out of the Gemini-alone line", "Gemini gave no answer on 3 scored row(s)" in allb)
check("needs_review: 1 row, 1 in the scored set, 0 on no-owner shops", "needs_review: 1 row(s), 1 in the scored set; 0 of those sit on no-owner shops" in allb)
check("reachable-correct: lenient 8/9, strict 7/9 (the conflict's Gemini candidate is right); delivered alone 7/9 and 6/9",
      "lenient 8/9, strict 7/9; delivered alone lenient 7/9, strict 6/9" in allb)
check("evidence URL on named rows: 7 of 8 (the first-name-only row has none)", "Evidence URL on named rows: 7 of 8 (1 named row(s) without one)" in allb)
check("Gemini errors: 3 of 13 run = 23%, vendor-side 2 (timeout 1, HTTP 5xx 1), other 1 (the 402), disabled 1",
      "Gemini errors: 3 of 13 lead(s) run = 23%; vendor-side 2 (HTTP 5xx 1, timeout 1); other 1 (HTTP 402 credits depleted 1); disabled after a 402: 1" in allb)
check("fallback delivered on 2 of 3 error rows (the third is a lead-level error) and the rule says INVESTIGATE",
      "Brave fallback delivered on 2 of 3 error row(s). Rule: INVESTIGATE" in allb)
check("Gemini cost $0.5600 over 14 result rows", "summed over 14 result row(s)" in allb and "ignores the free search tier): $0.5600" in allb)
check("Brave + Anthropic cost from the run log: Anthropic $2.1000 + Brave $2.0000 (400 calls), whole-run estimate $4.6600",
      "Anthropic $2.1000 + Brave $2.0000" in allb and "400 Brave calls" in allb and "Whole-run cost estimate: $4.6600" in allb)

check("the without-chain block names 1 chain-overlap lead", "WITHOUT the 1 chain-overlap lead(s) (14)" in text)
check("without chain: delivered named 7/10, lenient 6/7 = 85.7% [49%-97%], strict 5/7 = 71.4% [36%-92%], wrong 1, correct blanks 1/2",
      "named 7/10 (70%) | lenient 6/7 = 85.7% [49%-97%] | strict 5/7 = 71.4% [36%-92%] | wrong 1 | correct blanks 1/2 | coverage 6/8 lenient, 5/8 strict | errors 1, excluded 3" in nochain)
check("without chain: Brave alone lenient 5/7, strict 4/7; Gemini alone over 7 rows, named 5",
      "lenient 5/7 = 71.4% [36%-92%] | strict 4/7 = 57.1%" in nochain and "named 5/7" in nochain
      and "coverage against ALL 8 owner-bearing scored rows (an unanswered row is not found): lenient 4/8, strict 3/8" in nochain)
check("without chain: reachable 7/8 lenient, 6/8 strict; Gemini errors 3 of 12 = 25%; Gemini cost $0.5100 over 13 rows",
      "lenient 7/8, strict 6/8" in nochain and "Gemini errors: 3 of 12 lead(s) run = 25%" in nochain and "$0.5100" in nochain and "summed over 13 result row(s)" in nochain)
check("without chain: the run-wide Brave + Anthropic cost is labelled whole run, not split", "WHOLE RUN" in nochain and "Whole-run cost estimate" not in nochain)
check("the output says the src diff against stage-f-run is empty and the figures count as holdout figures",
      "stage-f-run..HEAD -- src/: empty" in text and "count as holdout figures" in text)
check("the output carries no business name and no person's name", NAME_RE.search(text) is None)
check("the score folder holds exactly score_c2.txt and FREEZE.txt (no graded_rows file)", sorted(os.listdir(out)) == ["FREEZE.txt", "score_c2.txt"])
fz = open(os.path.join(out, "FREEZE.txt"), encoding="utf-8").read()
ref = open(os.path.join(score_dir, "FREEZE.txt"), encoding="utf-8").read()
check("FREEZE.txt carries the same worksheet and lead-list hashes as the Brave-only FREEZE.txt, plus the Gemini-on export and database hashes",
      f"worksheet sha256: {hc.file_sha256(ws_path)}" in fz and f"lead list sha256: {hc.file_sha256(clean)}" in fz and f"worksheet sha256: {hc.file_sha256(ws_path)}" in ref
      and f"export sha256: {hc.file_sha256(os.path.join(c2_sealed, 'enriched_leads.csv'))}" in fz and f"database sha256: {hc.file_sha256(os.path.join(c2_sealed, 'leads.db'))}" in fz)
check("the recorded Brave-only score folder was not touched", sorted(os.listdir(score_dir)) == ["FREEZE.txt", "graded_rows.csv", "score.txt"])
check("scoring into the same folder a second time is refused", "already exists" in (raises(lambda: c2.score_c2(ws_path, c2_sealed, clean, out, **KW)) or ""))

# --- the label argument --------------------------------------------------------------------------------------------------------------
text2 = c2.score_c2(ws_path, c2_sealed, clean, os.path.join(BASE, "c2_score_b"), label="Custom label", **KW)
check("--label replaces the default label", text2.startswith("Custom label:") and "Delivered (combined): Custom label, commit" in text2)

# --- no chain-overlap lead at all ------------------------------------------------------------------------------------------------------
db_rows = c2.read_results(os.path.join(c2_sealed, "leads.db"))
check("read_results returns the latest result per lead and sums cost over rows: 15 leads, the skipped one has no status",
      len(db_rows) == 15 and db_rows[pid(11)]["gemini_status"] is None and abs(db_rows[pid(0)]["cost_all"] - 0.05) < 1e-9 and db_rows[pid(0)]["n_results"] == 1)

# --- the Gemini error rule on its own (pure) ---------------------------------------------------------------------------------------------
def err_case(n_ok, errors, cases=None):
    rows, db = [], {}
    for k in range(n_ok + len(errors)):
        p = pid(100 + k)
        err = errors[k - n_ok] if k >= n_ok else None
        rows.append({"place_id": p, "grade": "blank"})
        db[p] = {"gemini_status": "error" if err else "ok", "gemini_error": err or "", "reconcile_case": "gemini_unavailable" if err else "agree"}
    return c2.gemini_errors(rows, db)


ok1 = err_case(19, ["timeout after 180s"])
check("1 timeout in 20 leads, fallback delivered: PASS (5%, vendor-side)", ok1["ok"] and ok1["rate"] == 0.05 and ok1["vendor"] == {"timeout": 1})
check("2 HTTP 5xx in 20 leads: INVESTIGATE (10%, above the 5% holdout limit for 5xx even though vendor-side)", not err_case(18, ["HTTP 503: x", "HTTP 500: y"])["ok"])
check("any other 4xx: INVESTIGATE and counted as 'other'", not err_case(19, ["HTTP 400: bad"])["ok"] and err_case(19, ["HTTP 400: bad"])["other"] == {"other HTTP error": 1})
check("an empty response counts as vendor-side", err_case(19, ["empty response (finish=STOP, block=-)"])["vendor"] == {"empty response": 1})
check("an error rate above 15% is INVESTIGATE even when every error is vendor-side", not err_case(8, ["timeout after 180s", "timeout after 180s"])["ok"])
check("no Gemini results at all gives a 0% rate, not a division error", c2.gemini_errors([{"place_id": "x", "grade": "blank"}], {"x": {"gemini_status": None}})["rate"] == 0.0)

# --- the run log cost reader and the git diff check -----------------------------------------------------------------------------------------
seal_c2 = sl.read_seal(c2_sealed)
costs, why = c2.run_costs(c2_sealed, seal_c2)
check("run_costs reads the four token counts and the Brave calls from the sealed log and prices them with src/cost.py",
      why is None and abs(costs["anthropic"] - 2.10) < 1e-9 and abs(costs["brave"] - 2.00) < 1e-9 and costs["brave_calls"] == 400 and costs["batches"] == 1)
bad_log = os.path.join(BASE, "badlog")
shutil.copytree(c2_sealed, bad_log)
with open(os.path.join(bad_log, "langgraph_debug.log"), "a", encoding="utf-8") as f:
    f.write("edited\n")
check("a run log that no longer matches SEAL.txt is not used", c2.run_costs(bad_log, seal_c2)[0] is None and "does not match" in c2.run_costs(bad_log, seal_c2)[1])
os.remove(os.path.join(bad_log, "langgraph_debug.log"))
check("a missing run log gives a reason, not a crash", c2.run_costs(bad_log, seal_c2)[0] is None)
real_run = c2.subprocess.run
try:
    class R:
        def __init__(self, rc):
            self.returncode = rc
    seen = []
    for rc, want in ((0, "empty"), (1, "NOT empty"), (128, "unknown")):
        c2.subprocess.run = lambda cmd, **kw: (seen.append(cmd), R(rc))[1]
        check(f"git diff exit {rc} is reported as {want!r}", c2.src_diff_status("abc123") == want)
    check("the diff is read-only: `git diff --quiet stage-f-run <rev> -- src/`", seen[0][3:] == ["diff", "--quiet", "stage-f-run", "abc123", "--", "src/"])
    check("an unknown commit is 'unknown' without calling git", c2.src_diff_status("unknown") == "unknown")
finally:
    c2.subprocess.run = real_run
text3 = c2.score_c2(ws_path, c2_sealed, clean, os.path.join(BASE, "c2_score_c"), score_dir=score_dir, today="2026-11-21", diff_fn=lambda rev: "NOT empty")
check("a non-empty src diff labels the figures post-holdout", "label these figures post-holdout" in text3)

# --- the CLI ----------------------------------------------------------------------------------------------------------------------------------
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    rc = c2.main(["--worksheet", bad, "--sealed", c2_sealed, "--clean", clean, "--out", os.path.join(BASE, "o9")])
check("the CLI returns exit code 2 with a STOP message for a bad input, and prints no name", rc == 2 and "STOP:" in buf.getvalue() and NAME_RE.search(buf.getvalue()) is None)
check("the CLI defaults are data/atlanta/c2, data/atlanta/c2_score and the label",
      c2.C2_SEALED == real_c2 and c2.DEFAULT_OUT == os.path.join(ROOT, "data", "atlanta", "c2_score") and c2.DEFAULT_LABEL == "Atlanta holdout, c2 combined, Gemini ON")

# --- imports -------------------------------------------------------------------------------------------------------------------------------------
probe = ("import sys; sys.path[:0] = [r'%s', r'%s']; import score_holdout_c2; "
         "print(sorted(m for m in ('graph','requests','anthropic','langchain_anthropic','langgraph','dotenv','httpx') if m in sys.modules))"
         % (HERE, os.path.join(ROOT, "src")))
outp = subprocess.run([sys.executable, "-I", "-c", probe], capture_output=True, text=True).stdout.strip()
check(f"importing the second scorer loads no pipeline graph, HTTP client or key loader (loaded: {outp})", outp == "[]")
src = open(os.path.join(HERE, "score_holdout_c2.py"), encoding="utf-8").read()
check("the second scorer imports grade_all, headline and judge instead of copying them",
      "sh.grade_all(" in src and "sh.headline(" in src and "from scoring import judge" in src and "def grade_all" not in src and "def headline" not in src and "def judge" not in src)

shutil.rmtree(BASE, ignore_errors=True)
if not data_existed:
    try:
        os.rmdir(os.path.join(ROOT, "data"))
    except OSError:
        pass
check("the test cleaned up after itself", not os.path.exists(BASE))

print("\nSecond scorer tests passed." if not failures else f"\n{len(failures)} FAILED: {failures}")
sys.exit(1 if failures else 0)
