"""Score the Gemini-on rerun of the Atlanta holdout (stage F, protocol section 11). COUNTS ONLY: the output holds no
business name and no person's name, and no per-lead file is written.

    python eval/score_holdout_c2.py --worksheet "data/atlanta/verify/Atlanta Verification Worksheet.xlsx" \
        --sealed data/atlanta/c2 --clean data/atlanta/intake/atlanta_leads_clean.csv --out data/atlanta/c2_score

It is the second set of figures beside the recorded Brave-only line (eval/HOLDOUT_RESULT.md), never a replacement for
it. eval/score_holdout.py is imported, not copied: grade_all, headline, judge (from eval/scoring.py), the worksheet
checks and the Wilson interval are the same code that produced the Brave-only line.

Order of work (so it cannot be used to peek or to move a number):
  1. --out is checked first, from paths alone: it must not exist, must not be (or sit inside) data/atlanta/score, the
     folder being scored, data/atlanta/sealed or data/atlanta/c2, nor sit anywhere under a folder holding a SEAL.txt;
  2. the worksheet is checked before the sealed folder is read (the same rules as the Brave-only scorer);
  3. the worksheet's SHA-256 and the lead list's SHA-256 must equal the ones in data/atlanta/score/FREEZE.txt: the same
     frozen worksheet and the same 45 leads, or the script stops;
  4. SEAL.txt must exist and the export, database and run log must still match its hashes;
  5. a folder whose every result says gemini_status = off is refused (that is a Brave-only run, not this one).
Then <out>/score_c2.txt (counts) and <out>/FREEZE.txt (hashes) are written. The folder has to be new, so a run cannot
be repeated into the same place; delete nothing, pick a new --out.

Read from the sealed folder's leads.db (read-only): brave_first_name, brave_last_name, gemini_first_name,
gemini_last_name, gemini_status, gemini_error, gemini_cost_usd, review_candidates, reconcile_case (latest result row
per lead). The delivered name is graded from the sealed enriched_leads.csv, exactly as the Brave-only scorer does.
"""
import argparse
import csv
import datetime
import json
import os
import re
import sqlite3
import subprocess
import sys
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))
import c2_report                                      # noqa: E402
import holdout_common as hc                           # noqa: E402
import paths                                          # noqa: E402
import score_holdout as sh                            # noqa: E402
import seal_holdout                                   # noqa: E402
from cost import LeadCost                             # noqa: E402
from scoring import judge, summarize                  # noqa: E402

DEFAULT_LABEL = "Atlanta holdout, c2 combined, Gemini ON"
DATA = os.path.join(ROOT, "data")
SCORE_DIR = os.path.join(DATA, "atlanta", "score")             # the recorded Brave-only score and its FREEZE.txt
BRAVE_SEALED = os.path.join(DATA, "atlanta", "sealed")         # the October run: never written again
C2_SEALED = os.path.join(DATA, "atlanta", "c2")
DEFAULT_OUT = os.path.join(DATA, "atlanta", "c2_score")
DEFAULT_WORKSHEET = os.path.join(DATA, "atlanta", "verify", "Atlanta Verification Worksheet.xlsx")
DEFAULT_CLEAN = os.path.join(DATA, "atlanta", "intake", "atlanta_leads_clean.csv")
TAG = "stage-f-run"


# ---- the output folder -------------------------------------------------------------------------------------
def _norm(p):
    return os.path.normcase(os.path.realpath(os.path.abspath(p)))


def _inside(child, parent):
    c, p = _norm(child), _norm(parent)
    return c == p or c.startswith(p + os.sep)


def check_out(out_dir, sealed_dir, score_dir=SCORE_DIR, protected=(BRAVE_SEALED, C2_SEALED)):
    """Resolve --out and refuse every place it must not be. Looks at paths (and whether SEAL.txt exists) only."""
    out = paths.resolve_data_dir(out_dir)
    if _inside(out, score_dir):
        raise ValueError("--out is (or is inside) the recorded Brave-only score folder; that folder is never written again")
    for s in (sealed_dir, *protected):
        if _inside(out, s):
            raise ValueError("--out is (or is inside) a sealed folder; sealed folders are never written again")
    base = _norm(os.path.join(paths.ROOT, paths.DATA_ROOT_NAME))
    cur = _norm(out)
    while cur != base and cur.startswith(base + os.sep):
        if os.path.exists(os.path.join(cur, "SEAL.txt")):
            raise ValueError("--out sits inside a folder that holds a SEAL.txt (a sealed folder)")
        cur = os.path.dirname(cur)
    if os.path.exists(out):
        raise ValueError("--out already exists. A scoring folder is written once; choose a new name.")
    return out


# ---- the frozen worksheet ----------------------------------------------------------------------------------
def read_freeze(path):
    out = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            if ": " in line:
                k, v = line.rstrip("\n").split(": ", 1)
                out[k] = v
    return out


def check_against_brave_freeze(worksheet_path, clean_csv, score_dir):
    """The worksheet and the lead list must be the ones frozen by the Brave-only score. Returns the hashes."""
    ref = os.path.join(score_dir, "FREEZE.txt")
    if not os.path.exists(ref):
        raise ValueError("the recorded Brave-only score has no FREEZE.txt to compare the worksheet with; "
                         "scoring a second set without that anchor is refused")
    old = read_freeze(ref)
    ws_hash, clean_hash = hc.file_sha256(worksheet_path), hc.file_sha256(clean_csv)
    if not old.get("worksheet sha256"):
        raise ValueError("the Brave-only FREEZE.txt has no 'worksheet sha256' line")
    if old["worksheet sha256"] != ws_hash:
        raise ValueError("the worksheet's hash differs from the Brave-only FREEZE.txt. The second set is scored on the "
                         "same frozen worksheet (protocol section 11); a correction is a footnote, not an edit.")
    if old.get("lead list sha256") and old["lead list sha256"] != clean_hash:
        raise ValueError("the lead list's hash differs from the Brave-only FREEZE.txt: the rerun must use the same 45 leads")
    return ws_hash, clean_hash


# ---- the sealed folder ---------------------------------------------------------------------------------------
def read_results(db_path):
    """{place_id: row} from the database, read-only: the latest result row per lead (plus cost summed over every
    row of that lead and the number of rows). Leads without a result (skipped at intake) get None values."""
    uri = "file:" + os.path.abspath(db_path).replace("\\", "/") + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT l.place_id AS place_id, r.brave_first_name, r.brave_last_name, r.gemini_first_name, r.gemini_last_name, "
            "r.gemini_status, r.gemini_error, r.gemini_cost_usd, r.review_candidates, r.reconcile_case "
            "FROM leads l LEFT JOIN results r ON r.lead_id = l.id AND r.id = (SELECT MAX(id) FROM results WHERE lead_id = l.id)")
        out = {r["place_id"]: dict(r) for r in rows}
        for r in conn.execute("SELECT l.place_id AS place_id, COUNT(*) AS n, SUM(r.gemini_cost_usd) AS cost "
                              "FROM results r JOIN leads l ON r.lead_id = l.id GROUP BY l.place_id"):
            if r["place_id"] in out:
                out[r["place_id"]].update(n_results=r["n"], cost_all=r["cost"] or 0.0)
    finally:
        conn.close()
    for d in out.values():
        d.setdefault("n_results", 0)
        d.setdefault("cost_all", 0.0)
    return out


def run_costs(sealed_dir, seal):
    """Brave + Anthropic cost of the whole run from the sealed run log's COST TRACKING block, priced by src/cost.py
    exactly as the pipeline does. Numbers only are taken from the log. Returns (dict, None) or (None, reason)."""
    p = os.path.join(sealed_dir, "langgraph_debug.log")
    if not os.path.exists(p):
        return None, "no langgraph_debug.log in the sealed folder"
    if hc.file_sha256(p) != seal.get("sha256 langgraph_debug.log"):
        return None, "langgraph_debug.log does not match SEAL.txt, so it was not used"
    with open(p, encoding="utf-8", errors="replace") as f:
        text = f.read()
    hk = re.findall(r"Haiku tokens:\s*([\d,]+) in \+ ([\d,]+) out", text)
    sn = re.findall(r"Sonnet tokens:\s*([\d,]+) in \+ ([\d,]+) out", text)
    bv = re.findall(r"Brave API calls:\s*(\d+)", text)
    if not hk or not (len(hk) == len(sn) == len(bv)):
        return None, "the cost summary lines were not found in the run log"
    num = lambda s: int(s.replace(",", ""))                       # noqa: E731
    anthropic = LeadCost(0, "-", haiku_input_tokens=sum(num(a) for a, _ in hk), haiku_output_tokens=sum(num(b) for _, b in hk),
                         sonnet_input_tokens=sum(num(a) for a, _ in sn), sonnet_output_tokens=sum(num(b) for _, b in sn))
    brave = LeadCost(0, "-", brave_calls=sum(int(x) for x in bv))
    return {"anthropic": anthropic.estimated_cost_usd(), "brave": brave.estimated_cost_usd(),
            "brave_calls": brave.brave_calls, "batches": len(hk)}, None


def src_diff_status(rev, root=ROOT):
    """'empty', 'NOT empty' or 'unknown' for `git diff stage-f-run <rev> -- src/` (read-only)."""
    if not rev or rev == "unknown":
        return "unknown"
    try:
        r = subprocess.run(["git", "-C", root, "diff", "--quiet", TAG, rev, "--", "src/"], capture_output=True, timeout=60)
    except Exception:
        return "unknown"
    return {0: "empty", 1: "NOT empty"}.get(r.returncode, "unknown")


# ---- the numbers ----------------------------------------------------------------------------------------------
def parse_candidates(text):
    """review_candidates (JSON text from the database) as a list of dicts; anything else is an empty list."""
    try:
        data = json.loads(text or "[]")
    except (ValueError, TypeError):
        return []
    return [c for c in data if isinstance(c, dict)] if isinstance(data, list) else []


def alone_rows(rows, ws_by_pid, db, which):
    """The graded rows with each SCORED row regraded from one path's own answer ('brave' or 'gemini'). Unscored rows
    keep their grade, so the errors/excluded counts match the delivered line. For Gemini, a scored row where the Gemini
    path gave no answer (status not ok) becomes 'no_answer' and drops out of that line's denominator."""
    out = []
    for r in rows:
        if r["grade"] not in sh.SCORED:
            out.append(r)
            continue
        d = db.get(r["place_id"]) or {}
        if which == "gemini" and d.get("gemini_status") != "ok":
            out.append(dict(r, grade="no_answer"))
            continue
        out.append(dict(r, grade=judge(d.get(f"{which}_first_name") or "", d.get(f"{which}_last_name") or "",
                                       sh.truth_of(ws_by_pid[r["place_id"]]))))
    return out


def reachable(rows, ws_by_pid, db):
    """(O, lenient, strict): owner-bearing scored rows whose delivered name is correct, or whose needs_review
    candidates include a correct name."""
    O = lenient = strict = 0
    for r in rows:
        if r["grade"] not in sh.SCORED or r["kind"] != "owner":
            continue
        O += 1
        hit_l, hit_s = r["grade"] in ("full", "partial"), r["grade"] == "full"
        if not hit_s:
            truth = sh.truth_of(ws_by_pid[r["place_id"]])
            grades = [judge(c.get("first") or "", c.get("last") or "", truth) for c in parse_candidates((db.get(r["place_id"]) or {}).get("review_candidates"))]
            hit_l = hit_l or any(g in ("full", "partial") for g in grades)
            hit_s = any(g == "full" for g in grades)
        lenient += hit_l
        strict += hit_s
    return O, lenient, strict


def gemini_errors(rows, db):
    """Gemini error figures over every lead that was run (a result row exists), whatever the worksheet verdict:
    this is an operational rate, not a precision figure. Classes and the pass rule come from eval/c2_report.py."""
    recs = []
    for r in rows:
        d = db.get(r["place_id"]) or {}
        if d.get("gemini_status") is not None:
            recs.append({"gemini_status": d["gemini_status"], "gemini_error": d.get("gemini_error") or "",
                         "reconcile_case": d.get("reconcile_case"), "grade": r["grade"]})
    g_err = [x for x in recs if x["gemini_status"] == "error"]
    g_run = [x for x in recs if x["gemini_status"] in ("ok", "error")]
    g_off = [x for x in recs if x["gemini_status"] == "disabled"]
    rate = len(g_err) / len(g_run) if g_run else 0.0
    classes = {k: len(v) for k, v in c2_report.error_classes(g_err).items()}
    return {"errors": len(g_err), "run": len(g_run), "disabled": len(g_off), "off": sum(x["gemini_status"] == "off" for x in recs),
            "rate": rate, "vendor": {k: v for k, v in classes.items() if k in c2_report.VENDOR_SIDE},
            "other": {k: v for k, v in classes.items() if k not in c2_report.VENDOR_SIDE},
            "fallback_ok": sum(x["reconcile_case"] == "gemini_unavailable" and x["grade"] != "error" for x in g_err),
            "ok": c2_report.gemini_rule_ok(g_err, g_off, rate, len(g_run), holdout=True)}


def _classes(d):
    return ", ".join(f"{k} {v}" for k, v in sorted(d.items())) or "none"      # sorted: the order must not depend on the worksheet's row order


def scope_block(title, rows, ws_by_pid, db, sha, today, label, costs, cost_reason):
    """The report for one set of rows (all leads, or without the chain-overlap leads)."""
    L = [title, "-" * len(title)]
    main, s, O, N = sh.headline(rows, sha, today, label=label)
    L.append("Delivered (combined): " + main)
    L.append("Brave alone, same run: " + sh.headline(alone_rows(rows, ws_by_pid, db, "brave"), sha, today, label=f"{label} | Brave alone")[0])
    g_rows = alone_rows(rows, ws_by_pid, db, "gemini")
    L.append("Gemini alone, same run: " + sh.headline(g_rows, sha, today, label=f"{label} | Gemini alone")[0])
    sg = summarize([r["grade"] for r in g_rows if r["grade"] in sh.SCORED])
    L.append(f"  (Gemini gave no answer on {sum(r['grade'] == 'no_answer' for r in g_rows)} scored row(s): error, disabled or off; "
             f"they are left out of the Gemini-alone line above, whose coverage is out of the rows it answered, and counted in the error figures below)")
    L.append(f"  Gemini alone, coverage against ALL {O} owner-bearing scored rows (an unanswered row is not found): "
             f"lenient {sg['correct_lenient']}/{O}, strict {sg['correct_strict']}/{O}")
    scored = [r for r in rows if r["grade"] in sh.SCORED]
    nr_all = [r for r in rows if r["outcome"] == "needs_review"]
    nr_scored = [r for r in nr_all if r["grade"] in sh.SCORED]
    L.append(f"needs_review: {len(nr_all)} row(s), {len(nr_scored)} in the scored set; "
             f"{sum(r['grade'] == 'correct_blank' for r in nr_scored)} of those sit on no-owner shops and count as correct blanks above (nothing was delivered)")
    _, rl, rs = reachable(rows, ws_by_pid, db)
    L.append(f"Reachable-correct (delivered correct, plus a correct name among the review_candidates): lenient {rl}/{O}, strict {rs}/{O}; "
             f"delivered alone lenient {s['correct_lenient']}/{O}, strict {s['correct_strict']}/{O}")
    named = [r for r in scored if r["grade"] in ("full", "partial", "wrong")]
    L.append(f"Evidence URL on named rows: {sum(bool((r['evidence_url'] or '').strip()) for r in named)} of {len(named)} "
             f"({sum(not (r['evidence_url'] or '').strip() for r in named)} named row(s) without one)")
    g = gemini_errors(rows, db)
    L.append(f"Gemini errors: {g['errors']} of {g['run']} lead(s) run = {g['rate']:.0%}; vendor-side {sum(g['vendor'].values())} ({_classes(g['vendor'])}); "
             f"other {sum(g['other'].values())} ({_classes(g['other'])}); disabled after a 402: {g['disabled']}"
             + (f"; gemini_status off on {g['off']} lead(s) (not expected)" if g["off"] else ""))
    L.append(f"  Brave fallback delivered on {g['fallback_ok']} of {g['errors']} error row(s). Rule: {'PASS' if g['ok'] else 'INVESTIGATE'} "
             f"(15% at most, every error vendor-side, fallback delivered on every error, no 402 or disabled row, 5xx under about 5% of the run)")
    pids = {r["place_id"] for r in rows}
    spent = sum((db.get(p) or {}).get("cost_all") or 0.0 for p in pids)
    n_rows = sum((db.get(p) or {}).get("n_results") or 0 for p in pids)
    L.append(f"Gemini cost (gemini_cost_usd, summed over {n_rows} result row(s); a list-price estimate that ignores the free search tier): ${spent:.4f}")
    if costs:
        L.append(f"Brave + Anthropic cost, WHOLE RUN (run log, priced as src/cost.py prices it: {costs['brave_calls']} Brave calls, "
                 f"known to over-count timeouts): Anthropic ${costs['anthropic']:.4f} + Brave ${costs['brave']:.4f}")
        if title.startswith("All"):
            L.append(f"Whole-run cost estimate: ${spent + costs['anthropic'] + costs['brave']:.4f}")
    else:
        L.append(f"Brave + Anthropic cost: not available ({cost_reason})")
    return L


def score_c2(worksheet_path, sealed_dir, clean_csv, out_dir, label=DEFAULT_LABEL, today=None, score_dir=SCORE_DIR,
             diff_fn=src_diff_status, protected=(BRAVE_SEALED, C2_SEALED)):
    """Score the Gemini-on run. Returns the text of score_c2.txt. Raises ValueError for any refusal."""
    today = today or datetime.date.today().isoformat()
    sealed_dir = paths.resolve_data_dir(sealed_dir)
    out_dir = check_out(out_dir, sealed_dir, score_dir, protected)                    # 1. paths only
    ws_rows = sh.read_worksheet(worksheet_path)                                         # 2. worksheet before the sealed folder
    sh.check_worksheet(ws_rows)
    ws_hash, clean_hash = check_against_brave_freeze(worksheet_path, clean_csv, score_dir)   # 3. same frozen inputs

    if not os.path.exists(os.path.join(sealed_dir, "SEAL.txt")):                        # 4. the seal
        raise ValueError(f"{sealed_dir} has no SEAL.txt. Run eval/seal_holdout.py right after the Gemini-on run.")
    seal = seal_holdout.read_seal(sealed_dir)
    for name in ("enriched_leads.csv", "leads.db"):
        p = os.path.join(sealed_dir, name)
        if not os.path.exists(p) or hc.file_sha256(p) != seal.get(f"sha256 {name}"):
            raise ValueError(f"{name} does not match SEAL.txt. The answers changed after sealing; this score would not be the rerun.")

    clean_rows = hc.read_clean_csv(clean_csv)
    with open(os.path.join(sealed_dir, "enriched_leads.csv"), newline="", encoding="utf-8") as f:
        export_rows = list(csv.DictReader(f))
    w_ids, c_ids, e_ids = ({r["place_id"] for r in ws_rows}, {r["id"] for r in clean_rows}, {r["place_id"] for r in export_rows})
    if w_ids != c_ids:
        raise ValueError("the worksheet and the lead list hold different Place IDs")
    if e_ids != c_ids:
        raise ValueError("the sealed export and the lead list hold different Place IDs. Wrong folder?")
    db = read_results(os.path.join(sealed_dir, "leads.db"))
    if set(db) != c_ids:
        raise ValueError("the sealed database and the lead list hold different Place IDs. Wrong folder?")
    if not any(d.get("gemini_status") not in (None, "off") for d in db.values()):
        raise ValueError("no result in this folder has a Gemini status other than off: this looks like a Brave-only run, "
                         "not the Gemini-on rerun")

    rows = sh.grade_all(ws_rows, export_rows, clean_rows)
    ws_by_pid = {w["place_id"]: w for w in ws_rows}
    sha = seal.get("git_commit", "unknown")
    costs, cost_reason = run_costs(sealed_dir, seal)
    cnt = Counter(r["grade"] for r in rows)
    no_chain = [r for r in rows if not r["chain_overlap"]]
    n_chain = len(rows) - len(no_chain)

    L = [f"{label}: second set of figures beside the recorded Brave-only line; counts only (no names).", "",
         f"Population: the same lead list and frozen worksheet as the Brave-only holdout; leads in the sample {len(rows)}.",
         f"Excluded from every denominator (in this order): errors {cnt['error']}, skipped at intake {cnt['skipped']}, "
         f"Ambiguous {cnt['ambiguous']}, Not determined {cnt['undetermined']}", ""]
    L += scope_block(f"All leads ({len(rows)})", rows, ws_by_pid, db, sha, today, label, costs, cost_reason)
    L.append("")
    if n_chain:
        L += scope_block(f"WITHOUT the {n_chain} chain-overlap lead(s) ({len(no_chain)})", no_chain, ws_by_pid, db, sha, today,
                         f"{label}, without {n_chain} chain-overlap", costs, cost_reason)
    else:
        L.append("No lead is flagged as a chain-name overlap; the with/without figures are the same.")
    seal_diff, head_diff = diff_fn(sha), diff_fn("HEAD")
    holdout_ok = seal_diff == "empty" and head_diff == "empty"
    L += ["", f"git diff {TAG}..<sealed commit> -- src/: {seal_diff}; git diff {TAG}..HEAD -- src/: {head_diff}. "
              + ("Empty: these count as holdout figures (protocol section 11)." if holdout_ok else
                 "Not confirmed empty: label these figures post-holdout (protocol section 11)."),
          "Seal facts: " + "; ".join(f"{k} {seal.get(k, '?')}" for k in ("git_commit", "frozen_source_check", "denylist_lines_added", "src_uncommitted_changes", "gemini_status")),
          f"Scored on {today}. The worksheet is the one frozen for the Brave-only score; the recorded Brave-only line is unchanged by this set."]
    text = "\n".join(L) + "\n"

    os.makedirs(out_dir, exist_ok=False)                                                 # written once, at the end
    with open(os.path.join(out_dir, "FREEZE.txt"), "w", encoding="utf-8") as f:
        f.write(f"frozen_at: {datetime.datetime.now().isoformat(timespec='seconds')}\n")
        f.write(f"worksheet sha256: {ws_hash}\n")
        f.write(f"lead list sha256: {clean_hash}\n")
        f.write(f"export sha256: {seal.get('sha256 enriched_leads.csv', '')}\n")
        f.write(f"database sha256: {seal.get('sha256 leads.db', '')}\n")
        f.write("matches the Brave-only FREEZE.txt: worksheet and lead list\n")
    with open(os.path.join(out_dir, "score_c2.txt"), "w", encoding="utf-8") as f:
        f.write(text)
    return text


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--worksheet", default=DEFAULT_WORKSHEET, help="the finished, frozen verification worksheet")
    ap.add_argument("--sealed", default=C2_SEALED, help="the sealed folder of the Gemini-on run (under <repo>/data/)")
    ap.add_argument("--clean", default=DEFAULT_CLEAN, help="atlanta_leads_clean.csv (the list that was run)")
    ap.add_argument("--out", default=DEFAULT_OUT, help="a NEW score folder (under <repo>/data/)")
    ap.add_argument("--label", default=DEFAULT_LABEL)
    a = ap.parse_args(argv)
    try:
        print(score_c2(a.worksheet, a.sealed, a.clean, a.out, label=a.label))
    except (ValueError, OSError, sqlite3.Error, KeyError) as e:
        print(f"STOP: {e}")
        return 2
    print("Wrote score_c2.txt (counts only) and FREEZE.txt to", a.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
