"""Production entry point: run the graph over every lead in leads.db, record the results, write the export.

    python src/run.py [--gemini] [--workers N]

Moved from first_graph.py. lead_to_input() is the ONE place that builds the graph's input; the eval harness uses it too.
"""
import argparse
import csv
import os
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


# Phase 7: Accuracy comparison utilities
class AccuracyComparison:
    """Compare LangGraph results against n8n baseline."""
    
    @staticmethod
    def load_n8n_extractions(csv_path: str) -> dict:
        """Load n8n's owner extractions from CSV.
        Returns: {business_name: {'first': ..., 'last': ..., 'confidence': ...}}"""
        extractions = {}
        try:
            with open(csv_path, 'r', encoding='utf-8') as f:
                reader = csv.DictReader(f)
                for row in reader:
                    business_name = row.get('business_name', '').strip()
                    first = row.get('decision_maker_first_name', '').strip()
                    last = row.get('decision_maker_last_name', '').strip()
                    conf = row.get('identity_confidence', 'low').strip()
                    
                    if business_name:
                        extractions[business_name] = {
                            'first': first,
                            'last': last,
                            'confidence': conf
                        }
        except Exception as e:
            logger.error(f"Failed to load n8n extractions: {e}")
        
        return extractions
    
    @staticmethod
    def compare(langgraph_result: dict, n8n_baseline: dict) -> dict:
        """Compare one LangGraph result to n8n baseline.
        Returns match status: 'exact', 'partial', 'miss'."""
        def _norm(name: str) -> str:
            n = (name or '').strip()
            return '' if n.upper() in ('<UNKNOWN>', '<UNKNOWN>', '<ERROR>', 'NA', 'N/A', 'NONE') else n
        lg_first = _norm(langgraph_result.get('decision_maker_first_name', ''))
        lg_last = _norm((langgraph_result.get('decision_maker_last_name', '') or '').strip())
        lg_conf = langgraph_result.get('confidence', 'low')

        n8n_first = n8n_baseline.get('first', '')
        n8n_last = n8n_baseline.get('last', '')
        n8n_conf = n8n_baseline.get('confidence', '')
        
        # Both missing = no match
        if not lg_first and not n8n_first:
            return {'match': 'both_unknown', 'notes': 'Both systems found no owner'}
        
        # LG found nothing = miss
        if not lg_first:
            n8n_first = _norm(n8n_first)
            n8n_last = _norm(n8n_last)
            if lg_first and not n8n_first:
                return {'match': 'lg_only',
                         'notes': f'LG found {lg_first} {lg_last} ({lg_conf}); n8n found nothing - manual verify'}
        
        # Exact match (first + last)
        if lg_first.lower() == n8n_first.lower() and lg_last.lower() == n8n_last.lower():

            rank = {'low': 0, 'medium':1, 'high':2}
            lg_rank = rank.get(lg_conf, 0)
            n8n_rank = rank.get(n8n_conf, 0)

            if lg_conf == 'high' and n8n_conf == 'high':
                return {'match': 'exact',
                        'confidence_alignment': 'both_high_pass',
                        'notes': 'Full name match, both high confidence'}

            elif lg_rank >= n8n_rank:
                return {'match': 'exact',
                        'confidence_alignment': 'lg_better_or_equal',
                        'notes': f'Full name match, LG: {lg_conf} >= n8n: {n8n_conf}'}

            elif lg_rank < n8n_rank:
                return {'match': 'exact',
                        'confidence_alignment': 'flag_review',
                        'notes': f'REVIEW: Full name match but LG: {lg_conf} < n8n: {n8n_conf} (why is thorough system less confident?)'}

            return {'match': 'exact',
                    'notes': 'Full name match'}
        
        # First name match only
        if lg_first.lower() == n8n_first.lower() and n8n_first:
            return {'match': 'partial', 
                    'notes': f'First name match: {lg_first} (LG: {lg_conf}, n8n: {n8n_conf})'}
        
        # Last name match only
        if lg_last.lower() == n8n_last.lower() and n8n_last:
            return {'match': 'partial',
                    'notes': f'Last name match: {lg_last} (LG: {lg_conf}, n8n: {n8n_conf})'}
        
        # Different person
        return {'match': 'miss',
                'notes': f'LG: {lg_first} {lg_last} ({lg_conf}) vs n8n: {n8n_first} {n8n_last} ({n8n_conf})'}


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

def process_leads_batch(csv_path: str = "MBG_-_Charlotte_Barbershops_-_Charlotte_Barbershops__7_.csv",
                        workers: int = DEFAULT_WORKERS):
    """Process multiple leads from database and compare to n8n baseline."""
    db = LeadDatabase()
    gemini_path.reset()          # a 402 disables Gemini for ONE batch only
    leads, skipped = intake.filter_leads(db.get_all_leads(), log=logger)
    skipped_path = intake.write_skipped_file(skipped)
    logger.info(f"Skipped leads written to {skipped_path}")

    logger.info(f"\n{'='*80}")
    logger.info(f"PHASE 7: BATCH PROCESSING + COST TRACKING + ACCURACY COMPARISON")
    logger.info(f"{'='*80}\n")
    logger.info(f"Processing {len(leads)} leads from database")
    logger.info(f"Comparing against n8n baseline from CSV: {csv_path}\n")

    # Load n8n baseline
    n8n_extractions = AccuracyComparison.load_n8n_extractions(csv_path)
    logger.info(f"Loaded {len(n8n_extractions)} n8n extractions from CSV\n")

    results_summary = []
    cost_summary = LeadCost(lead_id=0, business_name="BATCH_TOTAL")
    accuracy_metrics = {
        'exact': 0,
        'partial': 0,
        'miss': 0,
        'both_unknown': 0,
        'langgraph_errors': 0,
        'lg_only': 0
    }

    # Leads run concurrently (the graph is thread-safe per call); database writes and the comparison stay on this thread.
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

            # Accuracy comparison
            n8n_baseline = n8n_extractions.get(lead.business_name, {'first': '', 'last': '', 'confidence': ''})
            comparison = AccuracyComparison.compare(result, n8n_baseline)
            match = comparison['match']
            accuracy_metrics[match] += 1

            logger.info(f"  Comparison: {match.upper()} — {comparison['notes']}")

            results_summary.append({
                'business_name': lead.business_name,
                'langgraph_owner': f"{first_name or ''} {last_name or ''}".strip(),
                'langgraph_confidence': confidence,
                'n8n_owner': f"{n8n_baseline.get('first', '')} {n8n_baseline.get('last', '')}".strip(),
                'n8n_confidence': n8n_baseline.get('confidence', ''),
                'match': match,
                'tokens': cost.total_tokens if cost else 0,
                'brave_calls': cost.brave_calls if cost else 0,
                'estimated_cost_usd': f"${cost.estimated_cost_usd():.4f}" if cost else "$0.00"
            })

        except Exception as e:
            logger.error(f"✗ Error processing {lead.business_name}: {e}")
            accuracy_metrics['langgraph_errors'] += 1
            try:     # a crashed lead is an error row, not a missing one, so the export shows it
                db.write_result(lead.id, {"status": "error",
                                          "error_detail": f"graph crashed: {type(e).__name__}: {e}"[:300]})
            except Exception as db_err:
                logger.error(f"Could not record the crash for {lead.business_name}: {db_err}")
            results_summary.append({
                'business_name': lead.business_name,
                'langgraph_owner': 'ERROR',
                'langgraph_confidence': 'failed',
                'n8n_owner': n8n_extractions.get(lead.business_name, {}).get('first', ''),
                'n8n_confidence': n8n_extractions.get(lead.business_name, {}).get('confidence', ''),
                'match': 'error',
                'tokens': 0,
                'brave_calls': 0,
                'estimated_cost_usd': '$0.00'
            })

    g_ok, g_err, g_rate = gemini_error_rate([r for r, _ in outcomes])
    logger.info(f"Gemini path: {g_ok} ok, {g_err} error ({g_rate:.0%} error rate); {workers} workers, {batch_seconds:.0f}s")
    _costs = [r["cost"] for r, _ in outcomes if r and r.get("cost")]
    logger.info(f"Brave: {sum(c.brave_calls for c in _costs)} searches, {sum(c.brave_429 for c in _costs)} rate-limited (429), "
                f"{sum(c.brave_429_recovered for c in _costs)} recovered on retry, {sum(len(c.brave_errors) for c in _costs)} searches errored")
    try:     # stored per batch next to the per-lead gemini_status/gemini_error columns
        stats_path = paths.BATCH_STATS_PATH
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

    # Accuracy summary
    total_processed = len(results_summary) - accuracy_metrics['langgraph_errors']
    logger.info("ACCURACY COMPARISON vs n8n baseline:")
    logger.info(f"  Exact matches: {accuracy_metrics['exact']}/{total_processed} ({accuracy_metrics['exact']/total_processed*100:.1f}%)")
    logger.info(f"  Partial matches (first or last name): {accuracy_metrics['partial']}/{total_processed} ({accuracy_metrics['partial']/total_processed*100:.1f}%)")
    logger.info(f"  Misses: {accuracy_metrics['miss']}/{total_processed} ({accuracy_metrics['miss']/total_processed*100:.1f}%)")
    logger.info(f"  Both systems unknown: {accuracy_metrics['both_unknown']}/{total_processed} ({accuracy_metrics['both_unknown']/total_processed*100:.1f}%)")
    logger.info(f"  LangGraph processing errors: {accuracy_metrics['langgraph_errors']}\n")

    hit_rate = (accuracy_metrics['exact'] + accuracy_metrics['partial']) / total_processed * 100
    logger.info(f"  Combined hit rate (exact + partial): {hit_rate:.1f}%")
    logger.info(f"  System availability: {(total_processed / len(leads)) * 100:.1f}%\n")

    logger.info(f"{'='*80}\n")

    # Write detailed comparison to file
    comparison_file = f"phase7_accuracy_comparison_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    try:
        with open(comparison_file, 'w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=results_summary[0].keys())
            writer.writeheader()
            writer.writerows(results_summary)
        logger.info(f"Detailed comparison saved to: {comparison_file}\n")
    except Exception as e:
        logger.error(f"Failed to write comparison file: {e}\n")

    # Stage A: the enriched CSV (one row per lead, errors apart from blanks, skips listed)
    try:
        out_path, counts = export.write_export(db.db_path)
        logger.info(f"Enriched export written to {out_path}: {counts}\n")
    except Exception as e:
        logger.error(f"Export failed: {e}\n")

    # Sample output
    logger.info("Sample results (first 10):")
    for result in results_summary[:10]:
        logger.info(f"  {result['business_name']}: LG={result['langgraph_owner']} ({result['langgraph_confidence']}) vs n8n={result['n8n_owner']} ({result['n8n_confidence']}) → {result['match'].upper()}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Run the enrichment pipeline on every lead in leads.db.")
    ap.add_argument("--gemini", action="store_true",
                    help="OPT IN to the Gemini path (costs money; ~$0.45/lead observed Oct 7, cause unconfirmed). Default: off")
    ap.add_argument("--no-gemini", action="store_true", help="accepted for compatibility; Gemini is off unless --gemini is given")
    ap.add_argument("--workers", type=int, default=DEFAULT_WORKERS, help="leads processed at the same time")
    ap.add_argument("--yes", action="store_true", help="skip the Gemini cost confirmation")
    cli = ap.parse_args()
    if cli.gemini and not cli.no_gemini:
        if not gemini_path.confirm_opt_in(len(LeadDatabase().get_all_leads()), cli.yes):
            raise SystemExit("Cancelled. Nothing was spent.")
        graph.GEMINI_ENABLED = True            # the node reads the flag in graph.py, not this module
    process_leads_batch(workers=cli.workers)
