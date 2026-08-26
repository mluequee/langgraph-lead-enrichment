import sqlite3
import os
import csv

DB_PATH = "leads.db"

def init_database():
    """Create SQLite database with leads and results tables."""

    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE leads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            business_name TEXT NOT NULL UNIQUE,
            website TEXT,
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
    """Load leads from CSV file (only business_name, website)."""
    conn =sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    count = 0
    skipped = 0

    with open(csv_path, 'r', encoding='utf=8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            business_name = row.get('business_name', '').strip()
            website = row.get('website', '').strip()

            if not business_name:
                skipped += 1
                continue

            try:
                cursor.execute(
                    "INSERT INTO leads (business_name, website) VALUES (?, ?)",
                    (business_name, website if website else None)
                )
                count += 1
            except sqlite3.IntegrityError:
                print(f"    Duplicate: {business_name}")
                skipped += 1

    conn.commit()
    conn.close()
    print(f"    Seeded {count} leads from CSV")
    if skipped > 0:
        print(f"    ({skipped} skipped - deuplicates or empty)")

if __name__ == "__main__":
    init_database()

    csv_path = "MBG_-_Charlotte_Barbershops_-_Charlotte_Barbershops__7_.csv"

    if not os.path.exists(csv_path):
        print(f"    CSV file not found: csv_path")
        print(f"    Place the CSV in: {os.getcwd()}")
    else:
        seed_data_from_csv(csv_path)