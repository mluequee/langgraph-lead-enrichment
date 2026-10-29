"""Production entry point: run the graph over every lead in leads.db, record the results, write the export.

    python src/run.py [--gemini] [--workers N] [--data-dir data/<city>/<folder>]

--data-dir (stage F) reads leads from, and writes every output to, one folder under <repo>/data/, so a second
city never touches the Charlotte leads.db / enriched_leads.csv / langgraph_debug.log. Without it nothing changes.

Moved from first_graph.py. lead_to_input() is the ONE place that builds the graph's input; the eval harness uses it too.
"""
import argparse
import csv
import logging
import os
import pathlib
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import export
import gemini_path
import intake
import paths
import graph
from cost import LeadCost
from db import LeadDatabase
from graph import DEFAULT_WORKERS, app, logger


def lead_to_input(lead):
    """What the graph receives for one lead. Used by _invoke_lead (production) and by eval/run_regression.py."""
    return {
        "business_name": lead.business_name,
        "lead_id": lead.id,
        "website": lead.website,
        "address": lead.address,
        "phone": lead.phone,
        "city": lead.city,
        "state": lead.state,
    }


def _invoke_lead(lead):
    """Run the graph for one lead. Returns (result, error); never raises, so one bad lead cannot stop a batch."""
    try:
        return app.invoke(lead_to_input(lead)), None
    except Exception as e:
        return None, e

def gemini_error_rate(results):
    """(ok, error, rate) over the results whose Gemini path ran (disabled or missing results do not count)."""
    st = [((r or {}).get("gemini") or {}).get("status") for r in results]
    ok, err = st.count("ok"), st.count("error")
    return ok, err, (err / (ok + err) if ok + err else 0.0)

def redirect_debug_log(log_path):
    """Point the pipeline's debug log at log_path (stage F).

    graph.py attaches a FileHandler for a relative 'langgraph_debug.log' to the root logger when it is imported,
    and the gate forbids editing graph.py. A data-folder run must not write owner names into the repo-root log
    (it would leak answers before verification and mix cities), so this removes that handler and attaches one
    that writes to log_path with the same format. Safe to call twice. Returns the absolute path."""
    root = logging.getLogger()
    target = os.path.abspath(log_path)
    formatter, present = None, False
    for h in list(root.handlers):
        if not isinstance(h, logging.FileHandler):
            continue
        if h.baseFilename == target:
            present = True
        elif os.path.basename(h.baseFilename) == "langgraph_debug.log":
            formatter = formatter or h.formatter
            root.removeHandler(h)
            h.close()
    if not present:
        h = logging.FileHandler(target, encoding="utf-8")
        h.setFormatter(formatter or logging.Formatter("%(levelname)s - %(message)s"))
        root.addHandler(h)
    return target


def _place_ids(db_path):
    """Place IDs in a database, opened read-only (this can never modify the file)."""
    uri = pathlib.Path(os.path.abspath(db_path)).as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        return {r[0] for r in conn.execute("SELECT place_id FROM leads") if r[0]}
    finally:
        conn.close()


def preflight_data_dir(db_path, charlotte_db=paths.DB_PATH):
    """Checks before a data-folder run spends anything. Returns the lead count, or raises SystemExit.

    Refuses: a missing or empty database, and any Place ID that is also in the Charlotte database (a city
    mix-up; the Charlotte file is opened read-only)."""
    if not os.path.exists(db_path):
        raise SystemExit(f"No database at {db_path}. Build it first: python src/setup_database.py --data-dir ... --csv ...")
    ids = _place_ids(db_path)
    if not ids:
        raise SystemExit(f"{db_path} has no leads. Nothing was run.")
    if os.path.exists(charlotte_db) and os.path.abspath(charlotte_db) != os.path.abspath(db_path):
        overlap = ids & _place_ids(charlotte_db)
        if overlap:
            raise SystemExit(f"{len(overlap)} Place ID(s) in {db_path} are also in the Charlotte leads.db "
                             f"({', '.join(sorted(overlap)[:3])}...). Wrong city or wrong file. Nothing was run.")
    return len(ids)


def process_leads_batch(workers: int = DEFAULT_WORKERS, data_dir=None):
    """Process every lead in the database: run the graph, record the results, write the enriched export.
    The comparison against the n8n baseline lives in eval/compare_n8n.py (it reads leads.db afterwards).
    data_dir (stage F): read and write everything in that folder; None keeps the repo-root files."""
    p = paths.data_paths(data_dir)
    if data_dir is not None:
        redirect_debug_log(p["log"])        # before anything logs, so no names reach the repo-root log
    db = LeadDatabase(p["db"])
    gemini_path.reset()          # a 402 disables Gemini for ONE batch only
    leads, skipped = intake.filter_leads(db.get_all_leads(), log=logger)
    skipped_path = intake.write_skipped_file(skipped, p["skipped"])
    logger.info(f"Skipped leads written to {skipped_path}")

    logger.info(f"\n{'='*80}")
    logger.info(f"BATCH PROCESSING + COST TRACKING")
    logger.info(f"{'='*80}\n")
    logger.info(f"Processing {len(leads)} leads from database\n")

    cost_summary = LeadCost(lead_id=0, business_name="BATCH_TOTAL")
    langgraph_errors = 0

    # Leads run concurrently (the graph is thread-safe per call); database writes stay on this thread.
    batch_start = time.time()
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        outcomes = list(pool.map(_invoke_lead, leads))
    batch_seconds = time.time() - batch_start

    for i, (lead, (result, run_error)) in enumerate(zip(leads, outcomes), 1):
        logger.info(f"\n[{i}/{len(leads)}] {lead.business_name}")

        try:
            if run_error is not None:
                raise run_error
            db.write_result(lead.id, result)

            confidence = result.get('confidence', 'unknown')
            first_name = result.get('decision_maker_first_name', 'unknown')
            last_name = result.get('decision_maker_last_name', 'unknown')
            cost = result.get('cost')

            # Accumulate costs
            if cost:
                cost_summary.haiku_input_tokens += cost.haiku_input_tokens
                cost_summary.haiku_output_tokens += cost.haiku_output_tokens
                cost_summary.sonnet_input_tokens += cost.sonnet_input_tokens
                cost_summary.sonnet_output_tokens += cost.sonnet_output_tokens
                cost_summary.brave_calls += cost.brave_calls

            logger.info(f"  Result: {first_name} {last_name} ({confidence})")
            if cost:
                logger.info(f"  Cost: {cost.total_tokens} tokens, {cost.brave_calls} Brave calls, est. ${cost.estimated_cost_usd():.4f}")

        except Exception as e:
            logger.error(f"✗ Error processing {lead.business_name}: {e}")
            langgraph_errors += 1
            try:     # a crashed lead is an error row, not a missing one, so the export shows it
                db.write_result(lead.id, {"status": "error",
                                          "error_detail": f"graph crashed: {type(e).__name__}: {e}"[:300]})
            except Exception as db_err:
                logger.error(f"Could not record the crash for {lead.business_name}: {db_err}")

    g_ok, g_err, g_rate = gemini_error_rate([r for r, _ in outcomes])
    logger.info(f"Gemini path: {g_ok} ok, {g_err} error ({g_rate:.0%} error rate); {workers} workers, {batch_seconds:.0f}s")
    _costs = [r["cost"] for r, _ in outcomes if r and r.get("cost")]
    logger.info(f"Brave: {sum(c.brave_calls for c in _costs)} searches, {sum(c.brave_429 for c in _costs)} rate-limited (429), "
                f"{sum(c.brave_429_recovered for c in _costs)} recovered on retry, {sum(len(c.brave_errors) for c in _costs)} searches errored")
    try:     # stored per batch next to the per-lead gemini_status/gemini_error columns
        stats_path = p["batch_stats"]
        new = not os.path.exists(stats_path)
        with open(stats_path, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if new:
                w.writerow(["finished", "leads", "workers", "seconds", "gemini_ok", "gemini_error", "gemini_error_rate",
                            "gemini_requests", "gemini_search_queries", "brave_searches", "brave_429", "brave_429_recovered",
                            "brave_search_errors"])
            costs = [r["cost"] for r, _ in outcomes if r and r.get("cost")]
            gem = [(r or {}).get("gemini") or {} for r, _ in outcomes]
            w.writerow([datetime.now().isoformat(timespec="seconds"), len(leads), workers, round(batch_seconds),
                        g_ok, g_err, round(g_rate, 3), sum(g.get("attempts") or 0 for g in gem),
                        sum(g.get("searches") or 0 for g in gem), sum(c.brave_calls for c in costs),
                        sum(c.brave_429 for c in costs), sum(c.brave_429_recovered for c in costs),
                        sum(len(c.brave_errors) for c in costs)])
    except Exception as e:
        logger.error(f"Could not write batch_stats.csv: {e}")

    # SUMMARY
    logger.info(f"\n{'='*80}")
    logger.info("BATCH RESULTS SUMMARY")
    logger.info(f"{'='*80}\n")

    # Cost summary
    logger.info("COST TRACKING:")
    logger.info(f"  Haiku tokens: {cost_summary.haiku_input_tokens:,} in + {cost_summary.haiku_output_tokens:,} out")
    logger.info(f"  Sonnet tokens: {cost_summary.sonnet_input_tokens:,} in + {cost_summary.sonnet_output_tokens:,} out")
    logger.info(f"  Total tokens: {cost_summary.total_tokens:,}")
    logger.info(f"  Brave API calls: {cost_summary.brave_calls}")
    logger.info(f"  Estimated cost: ${cost_summary.estimated_cost_usd():.2f}")
    logger.info(f"  Avg cost per lead: ${cost_summary.estimated_cost_usd() / len(leads):.4f}\n")

    logger.info(f"  LangGraph processing errors: {langgraph_errors}\n")
    logger.info(f"{'='*80}\n")

    # Stage A: the enriched CSV (one row per lead, errors apart from blanks, skips listed)
    try:
        out_path, counts = export.write_export(db.db_path, p["export"])
        logger.info(f"Enriched export written to {out_path}: {counts}\n")
    except Exception as e:
        logger.error(f"Export failed: {e}\n")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Run the enrichment pipeline on every lead in leads.db.")
    ap.add_argument("--gemini", action="store_true",
                    help="OPT IN to the Gemini path (costs money; ~$0.45/lead observed Oct 7, cause unconfirmed). Default: off")
    ap.add_argument("--no-gemini", action="store_true", help="accepted for compatibility; Gemini is off unless --gemini is given")
    ap.add_argument("--workers", type=int, default=DEFAULT_WORKERS, help="leads processed at the same time")
    ap.add_argument("--yes", action="store_true", help="skip the Gemini cost confirmation")
    ap.add_argument("--data-dir", metavar="DIR",
                    help="stage F: read leads from, and write every output to, DIR (a folder under <repo>/data/)")
    cli = ap.parse_args()
    try:
        file_paths = paths.data_paths(cli.data_dir)
    except ValueError as e:
        raise SystemExit(f"{e}. Nothing was run.")
    if cli.data_dir:
        n_leads = preflight_data_dir(file_paths["db"])
        print(f"Data folder: {os.path.dirname(file_paths['db'])}")
        print(f"Leads in it: {n_leads}   Workers: {cli.workers}   "
              f"Gemini: {'ON' if cli.gemini and not cli.no_gemini else 'off'}")
    if cli.gemini and not cli.no_gemini:
        if not gemini_path.confirm_opt_in(len(LeadDatabase(file_paths["db"]).get_all_leads()), cli.yes):
            raise SystemExit("Cancelled. Nothing was spent.")
        graph.GEMINI_ENABLED = True            # the node reads the flag in graph.py, not this module
    process_leads_batch(workers=cli.workers, data_dir=cli.data_dir)
