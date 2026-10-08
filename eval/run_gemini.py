"""Stage C experiment: Gemini with Grounding with Google Search vs the Brave path.

NOT wired into src/graph.py. For each audit row that survives the intake filter,
send Gemini the business name, address, city, state and phone (no website), let it
search Google, and grade the answer with the same scorer as run_regression.py.

From the repo root, venv active:
    python eval/run_gemini.py --limit 3                        # smoke test, asks first
    python eval/run_gemini.py                                  # every filtered audit row, asks first
    python eval/run_gemini.py --compare eval/data/results_gemini_<stamp>.csv   # free
    python eval/run_regression.py --rescore eval/data/results_gemini_<stamp>.csv  # same grader

Two results CSVs per run, both in the harness format (gitignored: real names):
    results_gemini_<stamp>.csv       GATED: a name is delivered only if Gemini's own
                                     grounding metadata says search results support it
                                     (the Gemini equivalent of the Brave path's citation gate)
    results_gemini_<stamp>_raw.csv   RAW: whatever name Gemini answered, ungated

Cost: grounding is NOT available on the free tier for Gemini 3.x (pricing page, 2026-10-05).
It needs a billing-enabled project: 5,000 grounded searches/month free (shared across 3.x),
then $14 per 1,000 searches, plus tokens. Always asks before spending, unless --yes.
The key is read from the repo's usual secrets file via load_dotenv and is never printed.

Like the Brave path, these 55 rows are the DEVELOPMENT set: nothing here is a listing number.
"""
import argparse
import csv
import json
import os
import re
import sqlite3
import sys
import time
from datetime import datetime

import requests
from dotenv import load_dotenv

EVAL = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(EVAL)
sys.path.insert(0, EVAL)
sys.path.insert(0, os.path.join(ROOT, "src"))
from scoring import judge, summarize, norm_tokens, match_person, is_blank  # noqa: E402
from run_regression import (GT, DB, FIELDS, key, select, excluded_rows,   # noqa: E402
                            print_excluded, truth_text, rescore)
import intake                                                              # noqa: E402
import evidence                                                            # noqa: E402

load_dotenv(r"C:\Users\marvi\.secrets\langgraph.env")

# The prompt, parsing, grounding reader and cost estimate live in gemini_path.py (the pipeline's Gemini path)
# and are imported here UNCHANGED, so the experiment script and the pipeline can never drift apart.
import gemini_path                                                         # noqa: E402
from gemini_path import (MODEL as DEFAULT_MODEL, ENDPOINT, PRICES, NULLISH, PROMPT, build_prompt, _nullish,   # noqa: E402,F401
                         parse_answer, extract_grounding, response_shape, diagnostics, answer_text, is_empty,
                         estimate_cost)
EST_COST_PER_ROW = 0.045   # list price incl. searches; smoke test: $0.043/row (token cost alone ~$0.01)
A0_RUNS = ["eval/data/results_20261005_074654.csv", "eval/data/results_20261005_080444.csv"]
EXTRA = ["title", "source_urls", "source_titles", "model_urls", "supported", "gate_reason",
         "search_queries", "raw_first", "raw_last", "tokens",
         "finish_reason", "block_reason", "prompt_tokens", "tooluse_tokens", "thought_tokens", "output_tokens",
         "status", "retries"]
def gate(answer, grounding):
    """Deliver a name only if a grounded segment of Gemini's answer contains it.

    Returns (supported, sources, reason). sources are the chunks behind the supporting segments
    (Google redirect URIs; the title is the site's domain). The URLs Gemini typed into its own
    JSON are untrusted and never used here: with structured answers it can invent URL paths."""
    if not answer or (not answer["first"] and not answer["last"]):
        return False, [], "no name answered"
    if not grounding["present"]:
        return False, [], "no groundingMetadata in the response"
    if not grounding["supports"]:
        return False, [], "no groundingSupports"
    toks = norm_tokens(f"{answer['first'] or ''} {answer['last'] or ''}")
    sources, seen = [], set()
    for s in grounding["supports"]:
        if not s["indices"] or not toks:
            continue
        words = set(norm_tokens(s["text"]))
        if all(t in words for t in toks):
            for i in s["indices"]:
                if 0 <= i < len(grounding["chunks"]) and i not in seen:
                    seen.add(i)
                    sources.append(grounding["chunks"][i])
    if not sources:
        return False, [], "owner name not in any grounded segment"
    return True, sources, "ok"


def call_gemini(prompt, model, api_key):
    """One grounded generateContent call. Returns (json, error). The key goes in a header, never in a URL."""
    body = {"contents": [{"parts": [{"text": prompt}]}], "tools": [{"google_search": {}}]}
    last = ""
    for attempt, wait in enumerate((0, 3, 9)):
        time.sleep(wait)
        if not gemini_path.is_allowed():            # last line of defence: no network access without --gemini
            return None, "Gemini is off: pass --gemini to allow calls"
        try:
            r = requests.post(ENDPOINT.format(model=model), json=body, timeout=120,
                              headers={"x-goog-api-key": api_key, "Content-Type": "application/json"})
        except requests.RequestException as e:
            last = f"{type(e).__name__}"
            continue
        if r.status_code == 200:
            return r.json(), ""
        try:
            msg = r.json().get("error", {}).get("message", "")
        except ValueError:
            msg = ""
        last = f"HTTP {r.status_code}: {msg[:200]}"
        if r.status_code not in (429, 500, 502, 503, 504):
            break
    return None, last


def ask_with_retry(prompt, model, api_key, name, call=None, log=print):
    """One grounded call. If the response comes back EMPTY, retry once; nothing else is retried here
    (HTTP errors are already retried inside call_gemini). The prompt and request are identical.

    Returns (resp, error, retries, spent): error is "" on success. An empty response that is still empty
    after the retry is an ERROR, never a blank; a blank is a real answer ("no owner found")."""
    call = call or call_gemini
    resp, err = call(prompt, model, api_key)
    if err or not is_empty(resp):
        return resp, err, 0, 0.0
    spent = estimate_cost(model, resp.get("usageMetadata") or {}, 0)     # an empty reply still bills tokens
    d = diagnostics(resp)
    log(f"RETRY row={name!r} attempt=2 reason=empty response (finish={d['finish_reason']}, block={d['block_reason'] or '-'})")
    resp2, err2 = call(prompt, model, api_key)
    if err2:
        log(f"RETRY row={name!r} attempt=2 outcome=http error ({err2[:100]}) -> error")
        return None, f"empty response, then the retry failed: {err2}", 1, spent
    if is_empty(resp2):
        d2 = diagnostics(resp2)
        log(f"RETRY row={name!r} attempt=2 outcome=still empty (finish={d2['finish_reason']}, block={d2['block_reason'] or '-'}) -> error")
        spent += estimate_cost(model, resp2.get("usageMetadata") or {}, 0)
        return resp2, f"empty response after retry (finish={d2['finish_reason']}, block={d2['block_reason'] or '-'})", 1, spent
    log(f"RETRY row={name!r} attempt=2 outcome=recovered")
    return resp2, "", 1, spent


def load_leads():
    """Same lookup run_regression uses: keyed on the normalized name, last lead wins."""
    if not os.path.exists(DB):
        raise SystemExit(f"{DB} not found. Seed the database first (python setup_database.py --reset).")
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT id, place_id, business_name, website, address, phone, city, state "
                        "FROM leads ORDER BY id").fetchall()
    conn.close()
    return {key(r["business_name"]): dict(r) for r in rows}


def make_rec(row, ans, first, last, why, extra, cost, secs, err):
    rec = {"id": row["id"], "business_name": row["business_name"], "subset": row["subset"],
           "truth": truth_text(row), "first": first or "<UNKNOWN>", "last": last or "<UNKNOWN>",
           "confidence": ans["confidence"] if ans else "", "verified_confidence": "",
           "why_this_person": why[:300], "cost_usd": round(cost, 4), "seconds": round(secs, 1), "error": err}
    rec.update(extra)
    rec["grade"] = "error" if err else judge(rec["first"], rec["last"], row)
    return rec


def run(rows, leads, args, api_key, retry_log=None):
    gated, raw = [], []
    streak = 0
    retry_log = retry_log if retry_log is not None else []

    def log(line):
        print(line)
        retry_log.append(line)

    for i, row in enumerate(rows, 1):
        lead = leads[row["key"]]
        t0 = time.time()
        resp, err, retries, spent = ask_with_retry(build_prompt(lead), args.model, api_key,
                                                   row["business_name"], call=call_gemini, log=log)
        secs = time.time() - t0
        if err:
            streak += 1
            blank = {"goes_by": "", "title": "", "source_urls": "", "source_titles": "", "model_urls": "",
                     "supported": "", "gate_reason": "", "search_queries": "", "raw_first": "", "raw_last": "", "tokens": 0,
                     "status": "error", "retries": retries}
            if resp:
                blank.update(diagnostics(resp))
            for out in (gated, raw):
                out.append(make_rec(row, None, "", "", "", blank, spent, secs, err))
            print(f"[{i}/{len(rows)}] {row['business_name'][:40]:40} -> ERROR {err[:80]}")
            if streak >= args.max_errors:
                print(f"\nStopping: {streak} errors in a row. Fix the cause before spending more.")
                break
            continue
        streak = 0
        text = answer_text(resp)
        ans, prose = parse_answer(text)
        g = extract_grounding(resp)
        usage = resp.get("usageMetadata") or {}
        cost = estimate_cost(args.model, usage, len(g["queries"])) + spent
        if ans is None:
            ok, sources, reason = False, [], "answer had no parseable JSON"
        else:
            ok, sources, reason = gate(ans, g)
        first_src = sources[0] if sources else None
        extra = {"goes_by_name": (ans or {}).get("goes_by") or "", "title": (ans or {}).get("title") or "",
                 # stage A evidence columns. Google redirect links are recorded as returned (kind 'redirect',
                 # NOT resolved); the domain is the grounding chunk's title. Label only.
                 **{k: (v or "") for k, v in (evidence.evidence_fields(
                     first_src["uri"], evidence.SRC_GEMINI, domain=first_src["title"],
                     other_urls=[s["uri"] for s in sources[1:]]) if first_src else evidence.empty_evidence()).items()},
                 "delivered_confidence": (ans or {}).get("confidence") if ok else "",
                 "source_urls": " | ".join(s["uri"] for s in sources),
                 "source_titles": " | ".join(s["title"] for s in sources),
                 "model_urls": " | ".join((ans or {}).get("model_urls", [])),
                 "supported": ok, "gate_reason": reason, "search_queries": " | ".join(g["queries"]),
                 "raw_first": (ans or {}).get("first") or "", "raw_last": (ans or {}).get("last") or "",
                 "tokens": usage.get("totalTokenCount", 0), "status": "ok", "retries": retries}
        diag = diagnostics(resp)
        extra.update(diag)
        why = prose or text
        a_first, a_last = (ans or {}).get("first"), (ans or {}).get("last")
        gated.append(make_rec(row, ans, a_first if ok else None, a_last if ok else None,
                              why, extra, cost, secs, ""))
        raw.append(make_rec(row, ans, a_first, a_last, why, extra, cost, secs, ""))
        r = gated[-1]
        print(f"[{i}/{len(rows)}] {row['business_name'][:40]:40} -> "
              f"{(r['first'] + ' ' + r['last']).replace('<UNKNOWN>', '').strip()[:28]:28} "
              f"({r['confidence']}) {r['grade']}  [{reason}; {len(g['queries'])} searches]")
        if args.show_shape:
            print(f"    shape: {response_shape(resp)} | tokens {usage.get('totalTokenCount', 0)} | est ${cost:.4f}")
            print(f"    sources: {extra['source_urls'] or '(none)'} [{extra['source_titles']}]")
            print(f"    diag: {diag} | response keys {sorted(resp)}")
    return gated, raw


def errors_with_retry(errors):
    return [r for r in errors if int(r.get("retries") or 0) > 0]


def write_csv(path, recs):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS + [c for c in EXTRA if c not in FIELDS], extrasaction="ignore")
        w.writeheader()
        w.writerows(recs)


def same_person(a, b):
    """True if two delivered answers name the same person (blank == blank)."""
    ab, bb = is_blank(a["first"], a["last"]), is_blank(b["first"], b["last"])
    if ab or bb:
        return ab and bb
    person = {"first": b["first"], "last": None if is_blank(b["last"], "") else b["last"], "aliases": []}
    other = {"first": a["first"], "last": None if is_blank(a["last"], "") else a["last"], "aliases": []}
    return bool(match_person(a["first"], a["last"], person) or match_person(b["first"], b["last"], other))


def table(label_recs):
    print(f"{'':26} {'named':>9} {'lenient':>9} {'strict':>9} {'prec len':>9} {'prec str':>9} {'wrong':>6} {'blanks':>7}")
    for label, recs in label_recs:
        s = summarize([r["grade"] for r in recs if r["grade"] != "error"])
        n_owner = sum(1 for r in recs if r["truth"] not in ("(no owner public)", "(ambiguous)") and r["grade"] != "excluded")
        n_blank = sum(1 for r in recs if r["truth"] == "(no owner public)" and r["grade"] != "excluded")
        print(f"{label:26} {s['named']:>3}/{s['rows']:<5} {s['correct_lenient']:>4}/{n_owner:<4} "
              f"{s['correct_strict']:>4}/{n_owner:<4} {s['precision_lenient']:>9.1%} {s['precision_strict']:>9.1%} "
              f"{s['wrong']:>6} {s['correct_blanks']:>3}/{n_blank:<3}")


def compare(gem_path, brave_paths, gt, excluded):
    """Side-by-side numbers and every row where Gemini and the Brave path disagree."""
    gem = {r["id"]: r for r in rescore(gem_path, gt, excluded) if r["grade"] != "excluded"}
    raw_path = gem_path.replace(".csv", "_raw.csv")
    sets = [(f"Brave A0 run {i + 1}", rescore(p, gt, excluded)) for i, p in enumerate(brave_paths)]
    sets.append(("Gemini gated", list(gem.values())))
    if os.path.exists(raw_path):
        sets.append(("Gemini raw (ungated)", [r for r in rescore(raw_path, gt, excluded) if r["grade"] != "excluded"]))
    print("\n" + "=" * 100)
    table(sets)
    print("Counts are over the rows Gemini actually answered (a stopped run covers fewer rows); "
          "'lenient'/'strict' are correct answers out of the owner-bearing rows, 'blanks' correct blanks out of no-owner rows.")

    err_rows = [r for r in rescore(gem_path, gt, excluded) if r["grade"] == "error"]
    if err_rows:
        print(f"ERROR rows (status 'error', NOT scored as blanks, left out of the disagreement list): "
              + ", ".join(r["business_name"] for r in err_rows))
    brave = [{r["id"]: r for r in rs} for _, rs in sets[:len(brave_paths)]]
    said = lambda r: (f"{r['first']} {r['last']}".replace("<UNKNOWN>", "").strip() or "-")[:22]
    rows = []
    for rid, g in gem.items():
        bs = [b[rid] for b in brave if rid in b]
        if bs and g["grade"] != "error" and any(not same_person(g, b) for b in bs):
            rows.append((g, bs))
    print(f"\nRows where Gemini and the Brave path disagree ({len(rows)} of {len(gem)}):")
    print(f"{'business':36} {'truth':26} {'Brave run1':26} {'Brave run2':26} {'Gemini gated':26}")
    for g, bs in rows:
        cells = [f"{said(b)} [{b['grade']}]" for b in bs] + [""] * (2 - len(bs))
        print(f"{g['business_name'][:35]:36} {g['truth'][:25]:26} {cells[0]:26} {cells[1]:26} {said(g) + ' [' + g['grade'] + ']':26}")
    wins = sum(1 for g, bs in rows if g["grade"] in ("full", "partial", "correct_blank")
               and all(b["grade"] in ("wrong", "blank") for b in bs))
    losses = sum(1 for g, bs in rows if g["grade"] in ("wrong", "blank")
                 and all(b["grade"] in ("full", "partial", "correct_blank") for b in bs))
    print(f"Gemini right where both Brave runs were wrong or blank: {wins}. "
          f"Brave right where Gemini was wrong or blank: {losses}.")
    print("Note: strict and lenient precision need the noise-band caveat; one run of either path moves ~1-2 answers.")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--subset", choices=["audit", "partial", "all"], default="audit")
    ap.add_argument("--limit", type=int, default=0, help="only the first N shops (smoke test)")
    ap.add_argument("--compare", metavar="CSV", help="compare a saved Gemini results CSV with the Brave A0 runs, no API calls")
    ap.add_argument("--brave", nargs="+", default=A0_RUNS, metavar="CSV", help="Brave-path results CSVs to compare against")
    ap.add_argument("--max-errors", type=int, default=3, help="stop after this many errors in a row")
    ap.add_argument("--show-shape", action="store_true",
                    help="per row, print which grounding fields the response carried, its sources and tokens")
    ap.add_argument("--only", nargs="+", metavar="NAME", help="only rows whose business name contains one of these (case-insensitive)")
    ap.add_argument("--yes", action="store_true", help="skip the cost confirmation")
    ap.add_argument("--gemini", action="store_true", help="REQUIRED to call Gemini (this script's only purpose; costs money)")
    ap.add_argument("--no-gemini", action="store_true", help="accepted for compatibility; has no effect")
    args = ap.parse_args()

    with open(GT, encoding="utf-8") as f:
        gt = json.load(f)
    excluded = excluded_rows(gt)

    if args.compare:
        compare(args.compare, args.brave, gt, excluded)
        return

    if not args.gemini:
        raise SystemExit("This script calls Gemini. Pass --gemini to allow it. Nothing was spent.")
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise SystemExit("GEMINI_API_KEY is not set (add it to the secrets file the repo loads).")

    print_excluded(gt, excluded)
    leads = load_leads()
    rows = []
    for r in select(gt, args.subset, 0):
        lead = leads.get(r["key"])
        if r["id"] in excluded:
            continue
        if lead is None:
            print(f"WARNING: {r['business_name']} not in leads.db, skipped")
            continue
        if intake.skip_reason(lead["business_name"], lead["place_id"]):
            print(f"INTAKE SKIP: {lead['business_name']}")
            continue
        rows.append(r)
    if args.only:
        rows = [r for r in rows if any(o.lower() in r["business_name"].lower() for o in args.only)]
    if args.limit:
        rows = rows[:args.limit]

    print(f"\nAbout to ask {args.model} (Grounding with Google Search) about {len(rows)} shop(s). "
          f"Estimated ~${len(rows) * EST_COST_PER_ROW:.2f} at list price (tokens plus $14 per 1,000 searches; "
          f"the first 5,000 searches a month are free on a paid tier).")
    if not gemini_path.confirm_opt_in(len(rows), args.yes):
        raise SystemExit("Cancelled. Nothing was spent.")

    retry_log = []
    gated, raw = run(rows, leads, args, api_key, retry_log)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(EVAL, "data", f"results_gemini_{stamp}.csv")
    write_csv(path, gated)
    write_csv(path.replace(".csv", "_raw.csv"), raw)
    if retry_log:
        with open(path.replace(".csv", "_retries.log"), "w", encoding="utf-8") as f:
            f.write("\n".join(retry_log) + "\n")
    errors = [r for r in gated if r["grade"] == "error"]
    retried = sum(int(r.get("retries") or 0) for r in gated)
    print(f"Empty-response retries: {retried} ({retried - len(errors_with_retry(errors))} recovered, "
          f"{len(errors_with_retry(errors))} ended as errors). Errors are status 'error', not blanks.")
    print(f"\nCost this run (list price, upper bound): ${sum(r['cost_usd'] for r in gated):.2f}"
          f"; {len(errors)} error row(s) are not scored. Saved: {path}")
    if len(errors) < len(gated):
        compare(path, args.brave, gt, excluded)


if __name__ == "__main__":
    main()
