import argparse
import sqlite3
import os
import csv

DB_PATH = "leads.db"

# Stage A: evidence, tier and status columns on `results`. Added to old databases by
# migrate_results (ALTER TABLE ADD COLUMN, safe to repeat). Existing rows keep NULL in them.
RESULTS_NEW_COLUMNS = [
    ("goes_by_name", "TEXT"), ("evidence_url", "TEXT"), ("evidence_domain", "TEXT"),
    ("evidence_source", "TEXT"), ("evidence_url_kind", "TEXT"), ("other_evidence_urls", "TEXT"),
    ("source_tier", "TEXT"), ("delivered_confidence", "TEXT"), ("status", "TEXT"), ("error_detail", "TEXT"),
    # stage C2: reconcile outcome, each path's own answer, and the Gemini path's status
    ("reconcile_case", "TEXT"), ("review_candidates", "TEXT"), ("brave_first_name", "TEXT"), ("brave_last_name", "TEXT"),
    ("gemini_first_name", "TEXT"), ("gemini_last_name", "TEXT"), ("gemini_goes_by_name", "TEXT"), ("gemini_title", "TEXT"),
    ("gemini_confidence", "TEXT"), ("gemini_status", "TEXT"), ("gemini_error", "TEXT"), ("gemini_evidence_url", "TEXT"),
    ("gemini_seconds", "REAL"), ("gemini_cost_usd", "REAL"),
]


def migrate_results(db_path: str) -> list:
    """Add any missing stage-A columns to `results`. Returns the names it added (empty if none).

    Never drops or rewrites data. Back the database up first (the first migration of leads.db
    was preceded by a copy, leads_pre_stage_a_backup.db)."""
    conn = sqlite3.connect(db_path)
    try:
        have = {r[1] for r in conn.execute("PRAGMA table_info(results)")}
        if not have:
            return []                       # no results table yet; init_database creates it with the columns
        added = []
        for name, ctype in RESULTS_NEW_COLUMNS:
            if name not in have:
                conn.execute(f"ALTER TABLE results ADD COLUMN {name} {ctype}")
                added.append(name)
        conn.commit()
        return added
    finally:
        conn.close()


def parse_city_state(address: str) -> tuple[str, str]:
    """Parse 'street, city, ST zip, USA' into (city, state). Returns ('', '') if it doesn't fit."""
    if not address:
        return "", ""
    parts = [p.strip() for p in address.split(",")]
    if len(parts) < 3:
        return "", ""
    city = parts[-3]
    state_zip = parts[-2].split()
    state = state_zip[0] if state_zip else ""
    return city, state


def init_database(reset: bool = False, db_path: str = None):
    """Create SQLite database with leads and results tables.

    Refuses to touch an existing DB unless reset=True (was previously
    an unconditional os.remove that destroyed the DB on every run).
    db_path defaults to DB_PATH (read at call time); stage F passes a per-dataset path.
    """
    db_path = db_path or DB_PATH
    if os.path.exists(db_path):
        if not reset:
            raise SystemExit(
                f"{db_path} already exists. Pass --reset to wipe and recreate it."
            )
        os.remove(db_path)

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE leads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            place_id TEXT UNIQUE,
            business_name TEXT NOT NULL,
            website TEXT,
            address TEXT,
            phone TEXT,
            city TEXT,
            state TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cursor.execute("""
        CREATE TABLE results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lead_id INTEGER NOT NULL,
            decision_maker_first_name TEXT,
            decision_maker_last_name TEXT,
            title TEXT,
            instagram TEXT,
            tiktok TEXT,
            facebook TEXT,
            linkedin TEXT,
            confidence TEXT,
            why_this_person TEXT,
            about_page_text TEXT,
            processed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            goes_by_name TEXT,
            evidence_url TEXT,
            evidence_domain TEXT,
            evidence_source TEXT,
            evidence_url_kind TEXT,
            other_evidence_urls TEXT,
            source_tier TEXT,
            delivered_confidence TEXT,
            status TEXT,
            error_detail TEXT,
            reconcile_case TEXT,
            review_candidates TEXT,
            brave_first_name TEXT,
            brave_last_name TEXT,
            gemini_first_name TEXT,
            gemini_last_name TEXT,
            gemini_goes_by_name TEXT,
            gemini_title TEXT,
            gemini_confidence TEXT,
            gemini_status TEXT,
            gemini_error TEXT,
            gemini_evidence_url TEXT,
            gemini_seconds REAL,
            gemini_cost_usd REAL
        )
    """)

    conn.commit()
    conn.close()
    print(f"Database initialized at {db_path}")


def seed_data_from_csv(csv_path, db_path: str = None):
    """Load leads from CSV file (business_name, website, address, phone, place_id).

    Dedupes on place_id (business_name is not unique in the source sheet -
    e.g. "Modern Man" and "The Cut Barbershop" each appear twice).
    """
    conn = sqlite3.connect(db_path or DB_PATH)
    cursor = conn.cursor()

    count = 0
    skipped = 0
    unparsed_address = 0

    # utf-8-sig: a Drive/Excel export that starts with a byte-order mark would otherwise make the first
    # header read as 'business_name' with a hidden BOM in front, and every row would look like it has no name and be skipped.
    with open(csv_path, 'r', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        for row in reader:
            business_name = row.get('business_name', '').strip()
            website = row.get('website', '').strip()
            address = row.get('address', '').strip()
            phone = row.get('phone', '').strip()
            place_id = row.get('id', '').strip()

            if not business_name or not place_id:
                skipped += 1
                continue

            city, state = parse_city_state(address)
            if address and not city:
                unparsed_address += 1
                print(f"    Could not parse city/state from address: {address!r} ({business_name})")

            try:
                cursor.execute(
                    "INSERT OR IGNORE INTO leads (place_id, business_name, website, address, phone, city, state) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (place_id, business_name, website or None, address or None,
                     phone or None, city or None, state or None)
                )
                if cursor.rowcount:
                    count += 1
                else:
                    print(f"    Duplicate place_id: {business_name}")
                    skipped += 1
            except sqlite3.IntegrityError as e:
                print(f"    Skipped {business_name}: {e}")
                skipped += 1

    conn.commit()
    conn.close()
    print(f"    Seeded {count} leads from CSV")
    if skipped > 0:
        print(f"    ({skipped} skipped - duplicates, missing business_name, or missing place_id)")
    if unparsed_address > 0:
        print(f"    ({unparsed_address} leads have an address that didn't parse into city/state)")


def main(argv=None):
    import paths
    ap = argparse.ArgumentParser()
    ap.add_argument("--reset", action="store_true", help="wipe and recreate an existing database")
    ap.add_argument("--data-dir", metavar="DIR",
                    help="stage F: build the database inside DIR (must be a folder under <repo>/data/) instead of "
                         "leads.db in the current folder. Requires --csv.")
    ap.add_argument("--csv", metavar="FILE", help="lead CSV to seed from (required with --data-dir)")
    args = ap.parse_args(argv)

    if args.csv and not args.data_dir:
        raise SystemExit("--csv needs --data-dir. Without it the target is the Charlotte leads.db, and "
                         "--reset would wipe it. Nothing was touched.")
    if args.data_dir and not args.csv:
        raise SystemExit("--data-dir needs --csv. Seeding the default Charlotte CSV into another city's "
                         "database would be a mistake. Nothing was touched.")

    if args.data_dir:
        try:
            folder = paths.resolve_data_dir(args.data_dir)
        except ValueError as e:
            raise SystemExit(f"{e}. Nothing was touched.")
        db_path, csv_path = os.path.join(folder, "leads.db"), args.csv
        if not os.path.exists(csv_path):
            raise SystemExit(f"CSV file not found: {csv_path}. Nothing was touched.")
        os.makedirs(folder, exist_ok=True)
        init_database(reset=args.reset, db_path=db_path)
        seed_data_from_csv(csv_path, db_path=db_path)
        return

    init_database(reset=args.reset)

    csv_path = "MBG_-_Charlotte_Barbershops_-_Charlotte_Barbershops__7_.csv"

    if not os.path.exists(csv_path):
        print(f"    CSV file not found: {csv_path}")
        print(f"    Place the CSV in: {os.getcwd()}")
    else:
        seed_data_from_csv(csv_path)


if __name__ == "__main__":
    main()
