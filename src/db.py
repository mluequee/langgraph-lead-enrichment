"""Lead and LeadDatabase (moved unchanged from first_graph.py, except the logger: it keeps the pipeline logger
"graph" so the migration message still reaches the console handler that graph.py attaches)."""
import logging
import os
import sqlite3
from dataclasses import dataclass

import paths
import setup_database

DB_PATH = paths.DB_PATH


@dataclass
class Lead:
    """Represents a lead from the database."""
    id: int
    business_name: str
    website: str
    address: str = ""
    phone: str = ""
    city: str = ""
    state: str = ""
    place_id: str = ""

class LeadDatabase:
    """Database abstraction layer — SQLite now, Postgres-ready by design."""

    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        if os.path.exists(db_path):
            added = setup_database.migrate_results(db_path)     # stage A columns; no-op once present
            if added:
                logging.getLogger("graph").info(f"Migrated results table: added {', '.join(added)}")

    def get_all_leads(self) -> list[Lead]:
        """Fetch all unprocessed leads."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        cursor.execute("SELECT id, place_id, business_name, website, address, phone, city, state FROM leads")
        rows = cursor.fetchall()
        conn.close()

        return [
            Lead(
                id=row['id'],
                business_name=row['business_name'],
                website=row['website'] or "",
                address=row['address'] or "",
                phone=row['phone'] or "",
                city=row['city'] or "",
                state=row['state'] or "",
                place_id=row['place_id'] or "",
            )
            for row in rows
        ]

    def write_result(self, lead_id: int, result: dict) -> None:
        """Write extraction results for a lead. FIXED: corrected typos from Phase 6."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        cursor.execute("""
            INSERT INTO results (
                lead_id, decision_maker_first_name, decision_maker_last_name,
                title, instagram, tiktok, facebook, linkedin, confidence, 
                why_this_person, about_page_text,
                goes_by_name, evidence_url, evidence_domain, evidence_source, evidence_url_kind,
                other_evidence_urls, source_tier, delivered_confidence, status, error_detail,
                reconcile_case, review_candidates, brave_first_name, brave_last_name,
                gemini_first_name, gemini_last_name, gemini_goes_by_name, gemini_title, gemini_confidence,
                gemini_status, gemini_error, gemini_evidence_url, gemini_seconds, gemini_cost_usd
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            lead_id,
            result.get('decision_maker_first_name'),  # FIXED: was 'decision_make_first_name'
            result.get('decision_maker_last_name'),   # FIXED: was 'decison_maker_last_name'
            result.get('title'),
            result.get('instagram'),
            result.get('tiktok'),
            result.get('facebook'),
            result.get('linkedin'),
            result.get('confidence'),
            result.get('why_this_person'),
            result.get('about_page_text', ''),
            result.get('goes_by_name'),
            result.get('evidence_url'),
            result.get('evidence_domain'),
            result.get('evidence_source'),
            result.get('evidence_url_kind'),
            result.get('other_evidence_urls'),
            result.get('source_tier'),
            result.get('delivered_confidence'),
            result.get('status'),
            result.get('error_detail'),
            result.get('reconcile_case'),
            result.get('review_candidates'),
            (result.get('brave_answer') or {}).get('first'),
            (result.get('brave_answer') or {}).get('last'),
            (result.get('gemini') or {}).get('first'),
            (result.get('gemini') or {}).get('last'),
            (result.get('gemini') or {}).get('goes_by'),
            (result.get('gemini') or {}).get('title'),
            (result.get('gemini') or {}).get('confidence'),
            (result.get('gemini') or {}).get('status'),
            (result.get('gemini') or {}).get('error'),
            ((result.get('gemini') or {}).get('evidence') or {}).get('evidence_url'),
            (result.get('gemini') or {}).get('seconds'),
            (result.get('gemini') or {}).get('cost_usd'),
        ))

        conn.commit()
        conn.close()

    def get_results(self) -> list[dict]:
        """Fetch all processed results."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row 
        cursor = conn.cursor()

        cursor.execute("""
            SELECT
                l.business_name,
                r.decision_maker_first_name,
                r.decision_maker_last_name,
                r.confidence
            FROM results r
            JOIN leads l ON r.lead_id = l.id
            ORDER BY r.processed_at DESC
        """)

        rows = cursor.fetchall()
        conn.close()

        return [dict(row) for row in rows]
