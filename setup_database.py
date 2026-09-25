import argparse
import sqlite3
import os
import csv

DB_PATH = "leads.db"


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


def init_database(reset: bool = False):
    """Create SQLite database with leads and results tables.

    Refuses to touch an existing DB unless reset=True (was previously
    an unconditional os.remove that destroyed the DB on every run).
    """
    if os.path.exists(DB_PATH):
        if not reset:
            raise SystemExit(
                f"{DB_PATH} already exists. Pass --reset to wipe and recreate it."
            )
        os.remove(DB_PATH)

    conn = sqlite3.connect(DB_PATH)
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
            processed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    conn.commit()
    conn.close()
    print(f"Database initialized at {DB_PATH}")


def seed_data_from_csv(csv_path):
    """Load leads from CSV file (business_name, website, address, phone, place_id).

    Dedupes on place_id (business_name is not unique in the source sheet -
    e.g. "Modern Man" and "The Cut Barbershop" each appear twice).
    """
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    count = 0
    skipped = 0
    unparsed_address = 0

    with open(csv_path, 'r', encoding='utf-8') as f:
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


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--reset", action="store_true", help="wipe and recreate an existing leads.db")
    args = ap.parse_args()

    init_database(reset=args.reset)

    csv_path = "MBG_-_Charlotte_Barbershops_-_Charlotte_Barbershops__7_.csv"

    if not os.path.exists(csv_path):
        print(f"    CSV file not found: {csv_path}")
        print(f"    Place the CSV in: {os.getcwd()}")
    else:
        seed_data_from_csv(csv_path)
