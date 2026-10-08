"""Stage C2 reporting for run_regression.py: Brave alone, Gemini alone and combined from the SAME run,
reachable-correct, the gate, and row-by-row differences against earlier runs. No API calls.

Definitions
  delivered   the name the pipeline delivers after reconcile (the CSV's first/last, graded in `grade`)
  reachable   delivered-correct, plus rows where nothing correct was delivered but the correct person is among
              the needs_review candidates (the client can pick them). Lenient and strict like everything else.
  Gemini alone is graded on the rows where the Gemini path returned an answer; error rows are listed apart.
"""
import json

from scoring import judge, summarize

NAMED = ("full", "partial", "wrong")
NOISE_BAND = 2          # identical code moves ~1-2 correct answers between runs
VENDOR_SIDE = ("timeout", "empty response", "HTTP 5xx")


def _owner_rows(recs):
    return [r for r in recs if r["grade"] != "excluded" and r["truth"] not in ("(no owner public)", "(ambiguous)")]


def _blank_rows(recs):
    return [r for r in recs if r["grade"] != "excluded" and r["truth"] == "(no owner public)"]


def alone_grades(recs, which):
    """Grades for 'brave' or 'gemini' alone; Gemini rows without an answer are dropped (counted by the caller)."""
    col = f"{which}_grade"
    return [r[col] for r in recs if r["grade"] != "excluded" and r.get(col) not in (None, "", "error", "disabled", "off")]


def candidates(rec):
    try:
        return json.loads(rec.get("review_candidates") or "[]")
    except ValueError:
        return []


def reachable(recs, gt_by_id):
    """(lenient, strict) reachable counts over the scored rows."""
    lenient = strict = 0
    for r in recs:
        if r["grade"] in ("excluded", "error"):
            continue
        hit_l, hit_s = r["grade"] in ("full", "partial"), r["grade"] == "full"
        if not hit_l and r.get("reconcile_case") == "conflict":
            row = gt_by_id[int(r["id"])]
            grades = [judge(c.get("first") or "", c.get("last") or "", row) for c in candidates(r)]
            hit_l, hit_s = any(g in ("full", "partial") for g in grades), any(g == "full" for g in grades)
        lenient += hit_l
        strict += hit_s
    return lenient, strict


def _line(label, grades, n_owner, n_blank, extra=""):
    s = summarize(grades)
    print(f"  {label:26} named {s['named']:>2}/{s['rows']:<3} lenient {s['correct_lenient']:>2}/{n_owner} "
          f"strict {s['correct_strict']:>2}/{n_owner} wrong {s['wrong']:>2} correct blanks {s['correct_blanks']}/{n_blank}{extra}")
    return s


def error_classes(g_err):
    """Gemini error rows grouped by what went wrong."""
    out = {}
    for r in g_err:
        e = (r.get("gemini_error") or "").lower()
        cls = ("timeout" if e.startswith("timeout") else "HTTP 402 credits depleted" if "http 402" in e else
               "HTTP 5xx" if e.startswith("http 5") else "other HTTP error" if e.startswith("http") else "empty response" if "empty response" in e else
               "unparseable answer" if "json" in e else "other")
        out.setdefault(cls, []).append(r)
    return out


def gemini_rule_ok(g_err, g_off, rate, n_run=0, holdout=False):
    """PASS up to 15% errors if every error is vendor-side (timeout, 5xx, empty response) and the Brave fallback
    delivered. FAIL (investigate) for any other 4xx (a 402 disables Gemini: g_off), any failed fallback, a rate over
    15%, or (holdout runs only) 5xx above about 5% of the run."""
    classes = error_classes(g_err)
    vendor_only = all(c in VENDOR_SIDE for c in classes)
    fallback_ok = all(r.get("reconcile_case") == "gemini_unavailable" and r["grade"] != "error" for r in g_err)
    five_xx_ok = not (holdout and n_run and len(classes.get("HTTP 5xx", [])) / n_run > 0.05)
    return rate <= 0.15 and vendor_only and fallback_ok and not g_off and five_xx_ok


def gemini_rule(g_err, g_off, g_run, rate):
    cls = {k: len(v) for k, v in error_classes(g_err).items()} or "none"
    return (f"Gemini error rate {len(g_err)}/{len(g_run)} = {rate:.0%}: pass up to 15% if every error is vendor-side "
            f"(timeout, 5xx, empty response) and the Brave fallback delivered; else investigate "
            f"(classes: {cls}{', DISABLED rows present (402)' if g_off else ''})")


def report_gemini_off(recs, scored):
    """A run made without --gemini (every row gemini_status=off): Brave only, the C2 gate does not apply."""
    n_owner, n_blank = len(_owner_rows(recs)), len(_blank_rows(recs))
    print("\nGemini off by choice, C2 gate not applicable (the Brave answer was delivered on every row).")
    sb = _line("Brave alone (delivered)", [r["grade"] for r in scored if r["grade"] != "error"], n_owner, n_blank)
    named = [r for r in scored if r["grade"] in NAMED]
    no_ev = [r for r in named if not r.get("evidence_url")]
    n_err = sum(r.get("status") == "error" for r in scored)
    requests_made = sum(int(float(r.get("gemini_attempts") or 0)) for r in recs)
    print("\nChecks that still apply:")
    for text, ok in (
            (f"delivered wrong names <= 3: {sb['wrong']}", sb["wrong"] <= 3),
            (f"correct blanks >= 2 of {n_blank}: {sb['correct_blanks']}", sb["correct_blanks"] >= min(2, n_blank)),
            (f"named rows without an evidence URL: {len(no_ev)} of {len(named)}", not no_ev),
            (f"leads with status=error: {n_err}", n_err == 0),
            (f"Gemini requests made: {requests_made}", requests_made == 0)):
        print(f"  {'PASS' if ok else 'FAIL'}  {text}")
    print("  n/a   Gemini alone, combined vs Brave, reachable, Gemini error rate")


def report(recs, gt_by_id, holdout=False):
    """Three-way table, reachable, gate. recs need the stage C2 columns (brave_grade, gemini_grade, ...)."""
    if not recs or not any("brave_grade" in r for r in recs):
        return
    n_owner, n_blank = len(_owner_rows(recs)), len(_blank_rows(recs))
    scored = [r for r in recs if r["grade"] not in ("excluded",)]
    if scored and all(r.get("gemini_status") == "off" for r in scored):
        return report_gemini_off(recs, scored)
    g_err = [r for r in scored if r.get("gemini_status") == "error"]
    g_run = [r for r in scored if r.get("gemini_status") in ("ok", "error")]
    print("\nSame run, three views (correct counts out of the owner-bearing rows):")
    sb = _line("Brave alone", alone_grades(recs, "brave"), n_owner, n_blank)
    sg = _line("Gemini alone", alone_grades(recs, "gemini"), n_owner, n_blank,
               f"   [{len(g_err)} Gemini error row(s), not graded]" if g_err else "")
    sc = _line("Combined (delivered)", [r["grade"] for r in scored if r["grade"] != "error"], n_owner, n_blank)
    rl, rs = reachable(recs, gt_by_id)
    review = [r for r in scored if r.get("reconcile_case") == "conflict" and r["grade"] != "error"]
    print(f"  {'Reachable (delivered + review candidates)':26} lenient {rl}/{n_owner} strict {rs}/{n_owner}   "
          f"[{len(review)} needs_review row(s)]")
    cases = {}
    for r in scored:
        cases[r.get("reconcile_case") or "-"] = cases.get(r.get("reconcile_case") or "-", 0) + 1
    print(f"  reconcile cases: {cases}")

    rate = (len(g_err) / len(g_run)) if g_run else 0.0
    g_off = [r for r in scored if r.get("gemini_status") == "disabled"]
    if g_off:
        print(f"  !!! Gemini was DISABLED mid-run (HTTP 402, credits depleted): {len(g_off)} row(s) got the Brave answer only, "
              "and are not in the error rate below")
    print(f"  Gemini error rate this run: {len(g_err)}/{len(g_run)} = {rate:.0%}"
          + ("" if not g_err else " -> " + "; ".join(f"{r['business_name'][:24]} ({(r.get('gemini_error') or '')[:40]})" for r in g_err)))
    named = [r for r in scored if r["grade"] in NAMED]
    classes = error_classes(g_err)
    if g_err:
        print("  Gemini errors by class (row, call duration):")
        for cls, cls_rows in classes.items():
            print(f"    {cls}: {len(cls_rows)}")
            for r in cls_rows:
                print(f"      {r['business_name'][:32]:32} {float(r.get('gemini_seconds') or 0):6.1f}s  fallback: {r.get('reconcile_case')} -> "
                      f"{(r['first'] + ' ' + r['last']).strip() or 'blank'} [{r['grade']}]")
    totals = {k: sum(int(float(r.get(k) or 0)) for r in recs) for k in
              ("gemini_attempts", "gemini_searches", "brave_calls", "brave_429", "brave_429_recovered", "brave_search_errors")}
    if "brave_calls" in recs[0]:
        print(f"  Brave: {totals['brave_calls']} searches, {totals['brave_429']} rate-limited (429), "
              f"{totals['brave_429_recovered']} recovered on retry, {totals['brave_search_errors']} searches errored")
        print(f"  Gemini: {totals['gemini_attempts']} requests, {totals['gemini_searches']} grounded search queries "
              f"(all rows, errors included where the count is known)")
    no_ev = [r for r in named if not r.get("evidence_url")]
    best_strict = max(sb["correct_strict"], sg["correct_strict"])
    n_err = sum(r.get("status") == "error" for r in scored)
    print("\nGate (final):")
    checks = [
        (f"delivered wrong names <= 3: {sc['wrong']}", sc["wrong"] <= 3),
        (f"correct blanks >= 2 of {n_blank}: {sc['correct_blanks']}", sc["correct_blanks"] >= min(2, n_blank)),
        (f"delivered strict-correct {sc['correct_strict']} above Brave alone {sb['correct_strict']} by more than the noise band "
         f"({NOISE_BAND})", sc["correct_strict"] - sb["correct_strict"] > NOISE_BAND),
        (f"reachable strict-correct {rs} above the better single path {best_strict} (Brave {sb['correct_strict']}, "
         f"Gemini {sg['correct_strict']}) by more than the noise band ({NOISE_BAND})", rs - best_strict > NOISE_BAND),
        (f"named rows without an evidence URL: {len(no_ev)} of {len(named)}", not no_ev),
        (f"leads with status=error: {n_err}", n_err == 0),
        (gemini_rule(g_err, g_off, g_run, rate), gemini_rule_ok(g_err, g_off, rate, len(g_run), holdout)),
    ]
    for text, ok in checks:
        print(f"  {'PASS' if ok else 'FAIL'}  {text}")
    print(f"  INFO  reachable lenient {rl}, strict {rs}; better single path strict {best_strict}; "
          f"5xx rate {len(classes.get('HTTP 5xx', []))}/{len(g_run)}"
          + ("" if holdout else " (5% limit applies to holdout runs only; pass holdout=True)"))


def _answer(r):
    return (f"{r.get('first', '')} {r.get('last', '')}".replace("<UNKNOWN>", "").strip() or "-")


def diff_vs(recs, ref_recs, label):
    """Rows whose answer, grade, confidence, delivered confidence or evidence differ from a reference run."""
    ref = {int(r["id"]): r for r in ref_recs}
    rows = []
    for r in recs:
        if r["grade"] == "excluded" or int(r["id"]) not in ref:
            continue
        o, diffs = ref[int(r["id"])], []
        if _answer(r) != _answer(o):
            diffs.append("answer")
        if r["grade"] != o["grade"]:
            diffs.append("grade")
        for col, name in (("confidence", "confidence"), ("delivered_confidence", "delivered_conf")):
            if col in o and col in r and (r.get(col) or "") != (o.get(col) or ""):
                diffs.append(name)
        if "evidence_url" in o and "evidence_url" in r and bool(r.get("evidence_url")) != bool(o.get("evidence_url")):
            diffs.append("evidence")
        elif "evidence_url" in o and "evidence_url" in r and (r.get("evidence_url") or "") != (o.get("evidence_url") or ""):
            diffs.append("evidence_url")
        if diffs:
            rows.append((r, o, diffs))
    print(f"\nRows that differ from {label} ({len(rows)}):")
    if rows:
        print("  names: " + "; ".join(r["business_name"] for r, _, _ in rows))
    for r, o, diffs in rows:
        print(f"  {r['business_name'][:30]:30} | ref {_answer(o)[:18]:18} [{o['grade']}] | now {_answer(r)[:18]:18} [{r['grade']}]"
              f" ({r.get('reconcile_case') or '-'}) | {', '.join(diffs)}")
