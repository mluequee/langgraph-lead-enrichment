"""Stage C experiment: Gemini with Grounding with Google Search vs the Brave path.

NOT wired into first_graph.py. For each audit row that survives the intake filter,
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
sys.path.insert(0, ROOT)
from scoring import judge, summarize, norm_tokens, match_person, is_blank  # noqa: E402
from run_regression import (GT, DB, FIELDS, key, select, excluded_rows,   # noqa: E402
                            print_excluded, truth_text, rescore)
import intake                                                              # noqa: E402

load_dotenv(r"C:\Users\marvi\.secrets\langgraph.env")

DEFAULT_MODEL = "gemini-3.8-flash"
ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
# USD per 1M tokens (input, output incl. thinking), and per search. LIST price, an upper bound:
# the 5,000 free searches/month are not subtracted. 3.8 Flash token prices double on 2027-01-01.
PRICES = {
    "gemini-3.8-flash": {"in": 0.75, "out": 3.75, "search": 14 / 1000},   # billed per search query run
    "gemini-2.5-flash": {"in": 0.30, "out": 2.50, "search": 35 / 1000},   # billed per grounded prompt
}
EST_COST_PER_ROW = 0.045   # list price incl. searches; smoke test: $0.043/row (token cost alone ~$0.01)
A0_RUNS = ["eval/data/results_20261005_074654.csv", "eval/data/results_20261005_080444.csv"]
EXTRA = ["goes_by", "title", "source_urls", "source_titles", "model_urls", "supported", "gate_reason",
         "search_queries", "raw_first", "raw_last", "tokens"]
NULLISH = {"", "null", "none", "n/a", "na", "unknown", "<unknown>"}

PROMPT = """You are researching a local business to find its owner.

Business: {business_name}
Address: {address}
City and state: {city}, {state}
Phone: {phone}

Use Google Search. Rules:
- Find the owner (or founder or co-founder) of THIS business at THIS address. Businesses elsewhere with the same or a similar name are NOT this business: check the city, address or phone before you rely on a source.
- Only name a person that a source explicitly says owns, founded or runs this business. Never infer a name from the business name.
- Report one person, the primary owner. If you cannot find one with a source, say so. A blank is better than a guess.

Answer in two parts.
1. Two or three plain sentences saying who the owner is and which source says so (name the site).
2. On the final line, one JSON object and nothing after it:
{{"owner_first_name": string or null, "owner_last_name": string or null, "goes_by_name": string or null, "title": string or null, "confidence": "high" or "medium" or "low", "source_urls": [string]}}
Use null where unknown. Use confidence "high" only when a source explicitly names this person as the owner of this business."""


def build_prompt(lead):
    """Name, address, city, state, phone. The website is deliberately not sent."""
    v = lambda x: x or "(not provided)"
    return PROMPT.format(business_name=lead["business_name"], address=v(lead["address"]),
                         city=v(lead["city"]), state=v(lead["state"]), phone=v(lead["phone"]))


def _nullish(s):
    return s is None or str(s).strip().lower() in NULLISH


def parse_answer(text):
    """Pull the final JSON object out of Gemini's text. Returns (dict, prose) or (None, text).

    Scans from the last '{' backwards for an object that parses and carries the expected key,
    so braces in the prose cannot confuse it."""
    dec = json.JSONDecoder()
    for m in reversed([m.start() for m in re.finditer(r"\{", text or "")]):
        try:
            obj, _ = dec.raw_decode(text[m:])
        except ValueError:
            continue
        if isinstance(obj, dict) and "owner_first_name" in obj:
            clean = lambda k: None if _nullish(obj.get(k)) else str(obj[k]).strip()
            conf = str(obj.get("confidence", "low")).strip().lower()
            urls = obj.get("source_urls") if isinstance(obj.get("source_urls"), list) else []
            return {"first": clean("owner_first_name"), "last": clean("owner_last_name"),
                    "goes_by": clean("goes_by_name"), "title": clean("title"),
                    "confidence": conf if conf in ("high", "medium", "low") else "low",
                    "model_urls": [str(u) for u in urls]}, text[:m].strip()
    return None, (text or "").strip()


def extract_grounding(resp):
    """Read groundingMetadata off the first candidate. Tolerates it being absent.

    Fields (REST, camelCase): webSearchQueries[], groundingChunks[].web.{uri,title},
    groundingSupports[].{segment.text, groundingChunkIndices[]}. A support's segment is a span of
    the MODEL'S answer, and its indices point at the chunks (sources) backing that span."""
    cand = (resp.get("candidates") or [{}])[0]
    gm = cand.get("groundingMetadata")
    out = {"present": bool(gm), "queries": [], "chunks": [], "supports": []}
    if not gm:
        return out
    out["queries"] = list(gm.get("webSearchQueries") or [])
    for c in gm.get("groundingChunks") or []:
        web = c.get("web") or {}
        out["chunks"].append({"uri": web.get("uri", ""), "title": web.get("title", "")})
    for s in gm.get("groundingSupports") or []:
        out["supports"].append({"text": (s.get("segment") or {}).get("text", ""),
                                "indices": list(s.get("groundingChunkIndices") or [])})
    return out


def response_shape(resp):
    """Which grounding fields the response actually carried (counts and key names only, no content)."""
    cand = (resp.get("candidates") or [{}])[0]
    gm = cand.get("groundingMetadata")
    shape = {"candidate_keys": sorted(cand), "finish": cand.get("finishReason"),
             "groundingMetadata": bool(gm)}
    if gm:
        shape.update(gm_keys=sorted(gm), groundingChunks=len(gm.get("groundingChunks") or []),
                     groundingSupports=len(gm.get("groundingSupports") or []),
                     webSearchQueries=len(gm.get("webSearchQueries") or []))
    return shape


def answer_text(resp):
    cand = (resp.get("candidates") or [{}])[0]
    parts = (cand.get("content") or {}).get("parts") or []
    return "".join(p.get("text", "") for p in parts if not p.get("thought"))


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


def estimate_cost(model, usage, n_queries):
    p = PRICES.get(model)
    if not p:
        return 0.0
    tin = (usage.get("promptTokenCount", 0) or 0) + (usage.get("toolUsePromptTokenCount", 0) or 0)
    tout = (usage.get("candidatesTokenCount", 0) or 0) + (usage.get("thoughtsTokenCount", 0) or 0)
    searches = n_queries if model.startswith("gemini-3") else (1 if n_queries else 0)
    return (tin * p["in"] + tout * p["out"]) / 1_000_000 + searches * p["search"]


def call_gemini(prompt, model, api_key):
    """One grounded generateContent call. Returns (json, error). The key goes in a header, never in a URL."""
    body = {"contents": [{"parts": [{"text": prompt}]}], "tools": [{"google_search": {}}]}
    last = ""
    for attempt, wait in enumerate((0, 3, 9)):
        time.sleep(wait)
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


def run(rows, leads, args, api_key):
    gated, raw = [], []
    streak = 0
    for i, row in enumerate(rows, 1):
        lead = leads[row["key"]]
        t0 = time.time()
        resp, err = call_gemini(build_prompt(lead), args.model, api_key)
        secs = time.time() - t0
        if err:
            streak += 1
            blank = {"goes_by": "", "title": "", "source_urls": "", "source_titles": "", "model_urls": "",
                     "supported": "", "gate_reason": "", "search_queries": "", "raw_first": "", "raw_last": "", "tokens": 0}
            for out in (gated, raw):
                out.append(make_rec(row, None, "", "", "", blank, 0.0, secs, err))
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
        cost = estimate_cost(args.model, usage, len(g["queries"]))
        if ans is None:
            ok, sources, reason = False, [], "answer had no parseable JSON"
        else:
            ok, sources, reason = gate(ans, g)
        extra = {"goes_by": (ans or {}).get("goes_by") or "", "title": (ans or {}).get("title") or "",
                 "source_urls": " | ".join(s["uri"] for s in sources),
                 "source_titles": " | ".join(s["title"] for s in sources),
                 "model_urls": " | ".join((ans or {}).get("model_urls", [])),
                 "supported": ok, "gate_reason": reason, "search_queries": " | ".join(g["queries"]),
                 "raw_first": (ans or {}).get("first") or "", "raw_last": (ans or {}).get("last") or "",
                 "tokens": usage.get("totalTokenCount", 0)}
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
    return gated, raw


def write_csv(path, recs):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS + EXTRA, extrasaction="ignore")
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

    brave = [{r["id"]: r for r in rs} for _, rs in sets[:len(brave_paths)]]
    said = lambda r: (f"{r['first']} {r['last']}".replace("<UNKNOWN>", "").strip() or "-")[:22]
    rows = []
    for rid, g in gem.items():
        bs = [b[rid] for b in brave if rid in b]
        if bs and any(not same_person(g, b) for b in bs):
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
    ap.add_argument("--yes", action="store_true", help="skip the cost confirmation")
    args = ap.parse_args()

    with open(GT, encoding="utf-8") as f:
        gt = json.load(f)
    excluded = excluded_rows(gt)

    if args.compare:
        compare(args.compare, args.brave, gt, excluded)
        return

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
    if args.limit:
        rows = rows[:args.limit]

    print(f"\nAbout to ask {args.model} (Grounding with Google Search) about {len(rows)} shop(s). "
          f"Estimated ~${len(rows) * EST_COST_PER_ROW:.2f} at list price (tokens plus $14 per 1,000 searches; "
          f"the first 5,000 searches a month are free on a paid tier).")
    if not args.yes and input("Proceed? [y/N] ").strip().lower() != "y":
        raise SystemExit("Cancelled. Nothing was spent.")

    gated, raw = run(rows, leads, args, api_key)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(EVAL, "data", f"results_gemini_{stamp}.csv")
    write_csv(path, gated)
    write_csv(path.replace(".csv", "_raw.csv"), raw)
    errors = [r for r in gated if r["grade"] == "error"]
    print(f"\nCost this run (list price, upper bound): ${sum(r['cost_usd'] for r in gated):.2f}"
          f"; {len(errors)} error row(s) are not scored. Saved: {path}")
    if len(errors) < len(gated):
        compare(path, args.brave, gt, excluded)


if __name__ == "__main__":
    main()
