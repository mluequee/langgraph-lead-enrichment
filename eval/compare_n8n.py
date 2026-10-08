"""Compare the pipeline's results in leads.db with the n8n baseline CSV. Free: reads files only, calls nothing.

    python eval/compare_n8n.py [--n8n-csv PATH] [--db PATH]

Decoupled from the product in stage E: src/run.py no longer knows about n8n. AccuracyComparison below is moved
UNCHANGED from first_graph.py, known bugs included (see CLAUDE.md "Known bugs": a sentinel-string n8n blank scores
EXACT against a blank, the lg_only branch cannot fire). The script around it is new: it reads the latest result of
each lead from leads.db instead of receiving the graph's output, and writes phase7_accuracy_comparison_<stamp>.csv.
"""
import argparse
import csv
import logging
import os
import sqlite3
from datetime import datetime

EVAL = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(EVAL)
DEFAULT_DB = os.path.join(ROOT, "leads.db")
DEFAULT_N8N_CSV = os.path.join(ROOT, "MBG_-_Charlotte_Barbershops_-_Charlotte_Barbershops__7_.csv")
logger = logging.getLogger("compare_n8n")


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


LATEST_RESULT_SQL = """
    SELECT l.business_name, r.decision_maker_first_name, r.decision_maker_last_name, r.confidence, r.status
    FROM leads l JOIN results r ON r.lead_id = l.id
    WHERE r.id = (SELECT MAX(id) FROM results WHERE lead_id = l.id)
    ORDER BY l.id
"""


def load_latest_results(db_path):
    """Latest result per lead, read-only. Returns [dict(business_name, decision_maker_first_name, ..., status)]."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(LATEST_RESULT_SQL)]
    finally:
        conn.close()


def compare_all(results, n8n_extractions):
    """(rows, metrics): one comparison row per result; a status=error result counts as a LangGraph error."""
    metrics = {"exact": 0, "partial": 0, "miss": 0, "both_unknown": 0, "langgraph_errors": 0, "lg_only": 0}
    rows = []
    for res in results:
        base = n8n_extractions.get(res["business_name"], {"first": "", "last": "", "confidence": ""})
        if res.get("status") == "error":
            metrics["langgraph_errors"] += 1
            rows.append({"business_name": res["business_name"], "langgraph_owner": "ERROR", "langgraph_confidence": "failed",
                         "n8n_owner": f"{base['first']} {base['last']}".strip(), "n8n_confidence": base["confidence"], "match": "error"})
            continue
        cmp_ = AccuracyComparison.compare(res, base)
        metrics[cmp_["match"]] += 1
        rows.append({"business_name": res["business_name"],
                     "langgraph_owner": f"{res.get('decision_maker_first_name') or ''} {res.get('decision_maker_last_name') or ''}".strip(),
                     "langgraph_confidence": res.get("confidence") or "", "n8n_owner": f"{base['first']} {base['last']}".strip(),
                     "n8n_confidence": base["confidence"], "match": cmp_["match"]})
    return rows, metrics


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--n8n-csv", default=DEFAULT_N8N_CSV)
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--out-dir", default=ROOT, help="where phase7_accuracy_comparison_<stamp>.csv is written (default: repo root)")
    a = ap.parse_args()
    n8n = AccuracyComparison.load_n8n_extractions(a.n8n_csv)
    print(f"Loaded {len(n8n)} n8n extractions from {a.n8n_csv}")
    results = load_latest_results(a.db)
    rows, m = compare_all(results, n8n)
    done = len(rows) - m["langgraph_errors"]
    pct = lambda n: f"{n / done * 100:.1f}%" if done else "n/a"
    print(f"\nACCURACY COMPARISON vs n8n baseline ({len(rows)} leads with a result in {a.db}):")
    print(f"  Exact matches: {m['exact']}/{done} ({pct(m['exact'])})")
    print(f"  Partial matches (first or last name): {m['partial']}/{done} ({pct(m['partial'])})")
    print(f"  Misses: {m['miss']}/{done} ({pct(m['miss'])})")
    print(f"  Both systems unknown: {m['both_unknown']}/{done} ({pct(m['both_unknown'])})")
    print(f"  LangGraph processing errors: {m['langgraph_errors']}")
    print(f"  Combined hit rate (exact + partial): {pct(m['exact'] + m['partial'])}")
    if rows:
        out = os.path.join(a.out_dir, f"phase7_accuracy_comparison_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv")
        with open(out, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
        print(f"Detailed comparison saved to: {out}")


if __name__ == "__main__":
    main()
