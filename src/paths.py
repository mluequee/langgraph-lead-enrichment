"""Where the data files live. The repo root is the parent of src/, whatever folder a command is run from.
(setup_database.py, intake.py and the debug log still use paths relative to the folder you run from, as before.)"""
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(ROOT, "leads.db")
EXPORT_PATH = os.path.join(ROOT, "enriched_leads.csv")
BATCH_STATS_PATH = os.path.join(ROOT, "batch_stats.csv")
