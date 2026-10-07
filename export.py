"""Enriched CSV: one row per lead, with evidence, and blanks, errors and skips kept apart.

    python export.py                       # leads.db -> enriched_leads.csv
    python export.py --db other.db --out out.csv

Also called at the end of process_leads_batch. Every lead in the database appears exactly once, with
its most recent result (the `results` table has no uniqueness rule, so re-runs are collapsed here).

`outcome` is the column to filter on:
    found           a name was delivered; evidence_url should be filled (the count of found rows
                    without one is printed and returned, the stage A gate wants 0)
    no_owner_found  a real blank: everything ran and no owner could be backed with evidence.
                    The name cells are EMPTY, not a placeholder.
    error           the lead could not be processed (every search failed, a model call raised, the
                    graph crashed, or no result was recorded). NOT a blank: retry it.
    skipped         dropped at intake (an '@' in the name, or a denylisted Place ID); skip_reason says why.

source_tier is a label for the evidence's site. Nothing in the pipeline reads it.
Output holds real people's names: *.csv is gitignored, keep it that way.
"""
import argparse
import csv
import os
import sqlite3

import evidence
import intake

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.path.join(HERE, "leads.db")
DEFAULT_OUT = os.path.join(HERE, "enriched_leads.csv")

COLUMNS = [
    "place_id", "business_name", "address", "phone", "website",
    "outcome", "owner_first_name", "owner_last_name", "goes_by_name", "title", "confidence",
    "source_tier", "evidence_url", "evidence_url_kind", "evidence_domain", "evidence_source",
    "other_evidence_urls", "instagram", "tiktok", "facebook", "linkedin",
    "error_detail", "skip_reason", "processed_at",
]
OUTCOME_ORDER = {"found": 0, "no_owner_found": 1, "error": 2, "skipped": 3}


def _rows(conn, sql):
    conn.row_factory = sqlite3.Row
    return [dict(r) for r in conn.execute(sql)]


def build_rows(db_path):
    """One dict per lead, sorted found, no_owner_found, error, skipped."""
    conn = sqlite3.connect(db_path)
    try:
        leads = _rows(conn, "SELECT id, place_id, business_name, website, address, phone FROM leads ORDER BY id")
        results = {}
        for r in _rows(conn, "SELECT r.* FROM results r JOIN (SELECT lead_id, MAX(id) AS mid FROM results "
                             "GROUP BY lead_id) m ON r.id = m.mid"):
            results[r["lead_id"]] = r
    finally:
        conn.close()

    out = []
    for lead in leads:
        row = {c: None for c in COLUMNS}
        row.update(place_id=lead["place_id"], business_name=lead["business_name"], address=lead["address"],
                   phone=lead["phone"], website=lead["website"])
        skip = intake.skip_reason(lead["business_name"], lead["place_id"] or "")
        res = results.get(lead["id"])
        if skip:
            row.update(outcome="skipped", skip_reason=skip)
        elif res is None:
            row.update(outcome="error", error_detail="no result recorded for this lead")
        else:
            row["processed_at"] = res.get("processed_at")
            first = evidence.none_if_empty(res.get("decision_maker_first_name"))
            last = evidence.none_if_empty(res.get("decision_maker_last_name"))
            legacy_error = "<ERROR>" in (str(res.get("decision_maker_first_name")).upper(),
                                         str(res.get("decision_maker_last_name")).upper())
            if res.get("status") == "error" or legacy_error:
                row.update(outcome="error", error_detail=res.get("error_detail")
                           or ("legacy '<ERROR>' name from an older run" if legacy_error else "error"))
            elif first or last:
                row.update(
                    outcome="found", owner_first_name=first, owner_last_name=last,
                    goes_by_name=evidence.none_if_empty(res.get("goes_by_name")),
                    title=evidence.none_if_empty(res.get("title")),
                    confidence=res.get("delivered_confidence") or res.get("confidence"),
                    source_tier=res.get("source_tier"), evidence_url=res.get("evidence_url"),
                    evidence_url_kind=res.get("evidence_url_kind"), evidence_domain=res.get("evidence_domain"),
                    evidence_source=res.get("evidence_source"), other_evidence_urls=res.get("other_evidence_urls"),
                    **{k: evidence.none_if_empty(res.get(k)) for k in ("instagram", "tiktok", "facebook", "linkedin")})
            else:
                row["outcome"] = "no_owner_found"
        out.append(row)
    out.sort(key=lambda r: (OUTCOME_ORDER[r["outcome"]], (r["business_name"] or "").lower()))
    return out


def write_export(db_path=None, out_path=None):
    """Write the CSV. Returns (path, counts), counts including 'found_without_evidence'."""
    db_path, out_path = db_path or DEFAULT_DB, out_path or DEFAULT_OUT
    rows = build_rows(db_path)
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        w.writerows(rows)
    counts = {o: sum(r["outcome"] == o for r in rows) for o in OUTCOME_ORDER}
    counts["found_without_evidence"] = sum(r["outcome"] == "found" and not r["evidence_url"] for r in rows)
    return out_path, counts


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--out", default=DEFAULT_OUT)
    a = ap.parse_args()
    path, counts = write_export(a.db, a.out)
    print(f"Wrote {path}")
    print("  " + ", ".join(f"{k}: {v}" for k, v in counts.items()))


if __name__ == "__main__":
    main()
