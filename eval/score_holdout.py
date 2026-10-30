"""Score the stage F holdout: join the finished blind worksheet to the sealed pipeline export and compute the
ONE reported number, exactly as eval/HOLDOUT_PROTOCOL.md section 5 defines it. eval/scoring.py is used as is.

    python eval/score_holdout.py --worksheet "data/atlanta/verify/Atlanta Verification Worksheet.xlsx" \
        --sealed data/atlanta/sealed --clean data/atlanta/intake/atlanta_leads_clean.csv --out data/atlanta/score

Order of work (so it cannot be used to peek):
  1. the worksheet is checked FIRST, before the sealed folder is touched: every row needs a verdict from the
     list; 'Owner found' needs an owner and a source type; 'No owner public' needs checked = Yes. Any problem
     stops the script with the row numbers and nothing from the sealed folder has been read;
  2. SEAL.txt must exist and the export and database must still match its hashes;
  3. the worksheet, the lead list and the export must hold the same Place IDs;
  4. the first successful run FREEZES the worksheet: FREEZE.txt records its hash, and a later run with an edited
     worksheet is refused.

Writes score.txt (counts and the headline; no names) and graded_rows.csv (per-lead detail WITH names) to --out,
which must be under <repo>/data/. Nothing here is committed: eval/HOLDOUT_RESULT.md is written by hand from
score.txt, counts only.
"""
import argparse
import csv
import datetime
import math
import os
import sys
from collections import Counter

import openpyxl

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))
import holdout_common as hc                       # noqa: E402
import paths                                      # noqa: E402
import seal_holdout                               # noqa: E402
from scoring import judge, summarize              # noqa: E402

SCORED = ("full", "partial", "wrong", "blank", "correct_blank")


def wilson(k, n, z=1.96):
    """Wilson 95% interval for k successes out of n (protocol section 5). (0, 0) when n is 0."""
    if n == 0:
        return 0.0, 0.0
    p = k / n
    d = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return centre - half, centre + half


# ---- the worksheet ---------------------------------------------------------------------------------
def read_worksheet(path):
    wb = openpyxl.load_workbook(path, data_only=True)
    if "Verify" not in wb.sheetnames:
        raise ValueError("the worksheet has no 'Verify' sheet")
    ws = wb["Verify"]
    header = [str(c.value or "").strip() for c in ws[1]]
    if header != hc.VERIFY_COLUMNS:
        raise ValueError("the worksheet's columns are not the ones the generator wrote; was it edited structurally?")
    rows = []
    for r in ws.iter_rows(min_row=2, values_only=True):
        if not any(v not in (None, "") for v in r):
            continue
        rows.append({h: ("" if v is None else str(v).strip()) for h, v in zip(header, r)})
    return rows


def check_worksheet(rows):
    """Raise ValueError listing every problem (by row number # only, never by name)."""
    problems = []
    seen = Counter(r["place_id"] for r in rows)
    if any(c > 1 for c in seen.values()):
        problems.append("duplicate Place IDs in the worksheet")
    missing = [r["#"] for r in rows if not r["verdict"]]
    if missing:
        problems.append(f"{len(missing)} row(s) have no verdict (#{', #'.join(missing[:15])}{'...' if len(missing) > 15 else ''})")
    for r in rows:
        v, n = r["verdict"], r["#"]
        if v and v not in hc.VERDICTS:
            problems.append(f"row #{n}: verdict {v!r} is not one of the four")
        elif v == hc.VERDICT_OWNER:
            if not r["owner_first"]:
                problems.append(f"row #{n}: Owner found but owner_first is empty")
            if not r["source_type"]:
                problems.append(f"row #{n}: Owner found but source_type is empty")
            elif r["source_type"] not in hc.SOURCE_TYPES:
                problems.append(f"row #{n}: source_type {r['source_type']!r} is not in the list")
        elif v == hc.VERDICT_NO_OWNER and r["checked"] != "Yes":
            problems.append(f"row #{n}: No owner public is allowed only after every source type was checked (checked = Yes)")
        if r["source_type_2"] and r["source_type_2"] not in hc.SOURCE_TYPES:
            problems.append(f"row #{n}: source_type_2 {r['source_type_2']!r} is not in the list")
    if problems:
        raise ValueError("the worksheet is not finished:\n  - " + "\n  - ".join(problems))


def truth_of(w):
    if w["verdict"] == hc.VERDICT_OWNER:
        owners = [{"first": w["owner_first"], "last": w["owner_last"] or None, "aliases": [w["goes_by"]] if w["goes_by"] else []}]
        if w["co_owner_first"]:
            owners.append({"first": w["co_owner_first"], "last": w["co_owner_last"] or None, "aliases": []})
        return {"kind": "owner", "owners": owners}
    if w["verdict"] == hc.VERDICT_NO_OWNER:
        return {"kind": "no_owner", "owners": []}
    return None


def truth_text(w):
    if w["verdict"] == hc.VERDICT_OWNER:
        t = f"{w['owner_first']} {w['owner_last']}".strip()
        return t + (f" / {w['co_owner_first']} {w['co_owner_last']}".rstrip() if w["co_owner_first"] else "")
    return "(no owner public)" if w["verdict"] == hc.VERDICT_NO_OWNER else f"({w['verdict'].lower()})"


# ---- grading ----------------------------------------------------------------------------------------
def grade_all(worksheet, export_rows, clean_rows):
    """One graded dict per lead. Exclusion precedence (mutually exclusive counts): error, skipped, ambiguous,
    not determined. Everything else is graded by scoring.judge."""
    exp = {r["place_id"]: r for r in export_rows}
    clean = {r["id"]: r for r in clean_rows}
    out = []
    for w in worksheet:
        pid = w["place_id"]
        e, c = exp[pid], clean[pid]
        row = {"place_id": pid, "business_name": w["business_name"], "city": c.get("city", ""), "verdict": w["verdict"],
               "truth": truth_text(w), "outcome": e["outcome"], "first": e.get("owner_first_name", ""), "last": e.get("owner_last_name", ""),
               "confidence": e.get("confidence", ""), "source_tier": e.get("source_tier", ""), "evidence_url": e.get("evidence_url", ""),
               "chain_overlap": "chain_overlap" in (c.get("flags") or ""), "kind": None}
        if e["outcome"] == "error":
            row["grade"] = "error"
        elif e["outcome"] == "skipped":
            row["grade"] = "skipped"
        elif w["verdict"] == hc.VERDICT_AMBIGUOUS:
            row["grade"] = "ambiguous"
        elif w["verdict"] == hc.VERDICT_UNDETERMINED:
            row["grade"] = "undetermined"
        else:
            truth = truth_of(w)
            row["kind"] = truth["kind"]
            row["grade"] = judge(row["first"], row["last"], truth)
        out.append(row)
    return out


def pct(x):
    return f"{x:.1%}"


def headline(rows, sha, today, label="Atlanta holdout, Brave-only"):
    scored = [r for r in rows if r["grade"] in SCORED]
    s = summarize([r["grade"] for r in scored])
    O = sum(r["kind"] == "owner" for r in scored)
    N = sum(r["kind"] == "no_owner" for r in scored)
    errors = sum(r["grade"] == "error" for r in rows)
    excluded = sum(r["grade"] in ("ambiguous", "undetermined", "skipped") for r in rows)
    a, S = s["named"], s["rows"]

    def prec(k):
        if a == 0:
            return "n/a"
        lo, hi = wilson(k, a)
        return f"{k}/{a} = {pct(k / a)} [{lo:.0%}-{hi:.0%}]"

    line = (f"{label}, commit {sha[:7]}, {today}: named {a}/{S} ({s['match_rate']:.0%}) | lenient {prec(s['correct_lenient'])} | "
            f"strict {prec(s['correct_strict'])} | wrong {s['wrong']} | correct blanks {s['correct_blanks']}/{N} | "
            f"coverage {s['correct_lenient']}/{O} lenient, {s['correct_strict']}/{O} strict | errors {errors}, excluded {excluded}")
    return line, s, O, N


def score(worksheet_path, sealed_dir, clean_csv, out_dir, today=None):
    """Score everything. Returns the text of score.txt. Raises ValueError for any refusal."""
    today = today or datetime.date.today().isoformat()
    # 1. worksheet first: nothing from the sealed folder is read until it is complete
    ws_rows = read_worksheet(worksheet_path)
    check_worksheet(ws_rows)

    sealed_dir = paths.resolve_data_dir(sealed_dir)
    out_dir = paths.resolve_data_dir(out_dir)
    # 2. the seal
    if not os.path.exists(os.path.join(sealed_dir, "SEAL.txt")):
        raise ValueError(f"{sealed_dir} has no SEAL.txt. Run eval/seal_holdout.py right after the cold run.")
    seal = seal_holdout.read_seal(sealed_dir)
    for name in ("enriched_leads.csv", "leads.db"):
        p = os.path.join(sealed_dir, name)
        if not os.path.exists(p) or hc.file_sha256(p) != seal.get(f"sha256 {name}"):
            raise ValueError(f"{name} does not match SEAL.txt. The answers changed after sealing; this score would not be the holdout.")

    # 3. the same leads on all three sides
    clean_rows = hc.read_clean_csv(clean_csv)
    with open(os.path.join(sealed_dir, "enriched_leads.csv"), newline="", encoding="utf-8") as f:
        export_rows = list(csv.DictReader(f))
    w_ids, c_ids, e_ids = ({r["place_id"] for r in ws_rows}, {r["id"] for r in clean_rows}, {r["place_id"] for r in export_rows})
    if w_ids != c_ids:
        raise ValueError(f"the worksheet and the lead list hold different Place IDs ({len(w_ids - c_ids)} only in the worksheet, "
                         f"{len(c_ids - w_ids)} only in the lead list)")
    if e_ids != c_ids:
        raise ValueError(f"the sealed export and the lead list hold different Place IDs ({len(e_ids - c_ids)} only in the export, "
                         f"{len(c_ids - e_ids)} only in the lead list). Wrong city or wrong folder?")

    # 4. the freeze
    os.makedirs(out_dir, exist_ok=True)
    hashes = {"worksheet": hc.file_sha256(worksheet_path), "lead list": hc.file_sha256(clean_csv),
              "export": seal.get("sha256 enriched_leads.csv", ""), "database": seal.get("sha256 leads.db", "")}
    freeze = os.path.join(out_dir, "FREEZE.txt")
    if os.path.exists(freeze):
        old = {l.split(": ", 1)[0]: l.rstrip("\n").split(": ", 1)[1] for l in open(freeze, encoding="utf-8") if ": " in l}
        if old.get("worksheet sha256") != hashes["worksheet"]:
            raise ValueError("the worksheet changed after the freeze (its hash differs from FREEZE.txt). The headline uses the worksheet "
                             "as frozen; a correction is a footnote (protocol sections 5 and 7), not an edit.")
    else:
        with open(freeze, "w", encoding="utf-8") as f:
            f.write(f"frozen_at: {datetime.datetime.now().isoformat(timespec='seconds')}\n")
            for k, v in hashes.items():
                f.write(f"{k} sha256: {v}\n")

    rows = grade_all(ws_rows, export_rows, clean_rows)
    sha = seal.get("git_commit", "unknown")
    main_line, s, O, N = headline(rows, sha, today)
    scored = [r for r in rows if r["grade"] in SCORED]

    L = [main_line, ""]
    L.append(f"Population: the output of Ai Lead Qualification (n8n), Atlanta GA suburbs included; leads in the sample {len(rows)}.")
    cnt = Counter(r["grade"] for r in rows)
    L.append(f"Excluded from every denominator (mutually exclusive, in this order): errors {cnt['error']}, skipped at intake {cnt['skipped']}, "
             f"Ambiguous {cnt['ambiguous']}, Not determined {cnt['undetermined']}")
    unresolved = cnt["ambiguous"] + cnt["undetermined"]
    if rows and unresolved / len(rows) > 0.20:
        L.append(f"WARNING: {unresolved} of {len(rows)} rows ({unresolved / len(rows):.0%}) are Ambiguous or Not determined, more than 20%.")
    L.append(f"Scored set S = {s['rows']} (owner-bearing O = {O}, no-owner N = {N}); named {s['named']}; missed (blank on an owner shop) {s['missed']}")
    L.append(f"Grades: full {cnt['full']}, partial {cnt['partial']}, wrong {cnt['wrong']}, blank {cnt['blank']}, correct blank {cnt['correct_blank']}")
    L.append("")
    L.append("Precision by the pipeline's own confidence (lenient):")
    for c in ("high", "medium", "low"):
        g = [r["grade"] for r in scored if r["confidence"] == c and r["grade"] in ("full", "partial", "wrong")]
        if g:
            L.append(f"  {c:6} {sum(x in ('full', 'partial') for x in g)}/{len(g)} = {sum(x in ('full', 'partial') for x in g) / len(g):.0%}")
    named = [r for r in scored if r["grade"] in ("full", "partial", "wrong")]
    L.append(f"Named rows without an evidence URL: {sum(not r['evidence_url'] for r in named)} of {len(named)}")
    L.append(f"needs_review rows (cannot occur with Gemini off): {sum(r['outcome'] == 'needs_review' for r in rows)}")
    L.append("City mix (scored set): " + ", ".join(f"{c or '?'} {n}" for c, n in Counter(r['city'] for r in scored).most_common()))
    no_chain = [r for r in rows if not r["chain_overlap"]]
    if len(no_chain) != len(rows):
        line2, _, _, _ = headline(no_chain, sha, today, label=f"WITHOUT the {len(rows) - len(no_chain)} chain-overlap lead(s)")
        L += ["", line2]
    else:
        L += ["", "No lead is flagged as a chain-name overlap with Charlotte; the with/without figures are the same."]
    L += ["", "Seal facts: " + "; ".join(f"{k} {seal.get(k, '?')}" for k in ("git_commit", "frozen_source_check", "denylist_lines_added", "src_uncommitted_changes", "gemini_status")),
          "Frozen inputs (sha256, first 16): " + "; ".join(f"{k} {v[:16]}" for k, v in hashes.items()),
          f"Scored on {today}. D6: blind truth only; any later correction is a footnote, not a change to the line above."]
    text = "\n".join(L) + "\n"

    with open(os.path.join(out_dir, "score.txt"), "w", encoding="utf-8") as f:
        f.write(text)
    with open(os.path.join(out_dir, "graded_rows.csv"), "w", newline="", encoding="utf-8") as f:
        cols = ["place_id", "business_name", "city", "verdict", "truth", "first", "last", "outcome", "confidence", "source_tier",
                "evidence_url", "chain_overlap", "grade"]
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    return text


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--worksheet", required=True)
    ap.add_argument("--sealed", required=True, help="the sealed data folder (under <repo>/data/)")
    ap.add_argument("--clean", required=True, help="atlanta_leads_clean.csv (the list that was run)")
    ap.add_argument("--out", required=True, help="score output folder (under <repo>/data/)")
    a = ap.parse_args(argv)
    try:
        print(score(a.worksheet, a.sealed, a.clean, a.out))
    except (ValueError, OSError) as e:
        print(f"STOP: {e}")
        return 2
    print("Wrote score.txt (no names) and graded_rows.csv (names) to", a.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
