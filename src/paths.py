"""Where the data files live. The repo root is the parent of src/, whatever folder a command is run from.
(setup_database.py, intake.py and the debug log still use paths relative to the folder you run from, as before.)"""
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(ROOT, "leads.db")
EXPORT_PATH = os.path.join(ROOT, "enriched_leads.csv")
BATCH_STATS_PATH = os.path.join(ROOT, "batch_stats.csv")


# --- Stage F: a per-dataset data folder -----------------------------------------------------------
# `--data-dir` (setup_database.py, run.py) puts the database, export, batch stats, skipped-leads file and
# debug log in ONE folder, so a second city can never touch the Charlotte files above. Without the
# argument nothing changes: every path is the root path it always was.
DATA_ROOT_NAME = "data"          # the only place a data folder may live (gitignored by `data/`)


def resolve_data_dir(data_dir, root=ROOT):
    """Absolute path of a dataset folder, which must sit inside <root>/data/ (never the root itself).

    Raises ValueError for anything else: the repo root, a sibling folder, a path that climbs out with '..'.
    A relative path is read from the folder the command runs in, as every other relative path here is."""
    base = os.path.realpath(os.path.join(root, DATA_ROOT_NAME))
    path = os.path.realpath(os.path.abspath(data_dir))
    if path == base or not path.startswith(base + os.sep):
        raise ValueError(f"--data-dir must be a folder inside {base} (got {path}); "
                         f"that keeps it gitignored and away from the Charlotte files")
    return path


def data_paths(data_dir=None, root=ROOT):
    """The five data files as a dict: db, export, batch_stats, skipped, log.

    data_dir=None gives the repo-root files the pipeline has always used. The debug log of the root
    layout is graph.py's own relative 'langgraph_debug.log' (the current folder), so it is reported as
    that name; run.py redirects it only when a data folder is given."""
    if data_dir is None:
        return {"db": DB_PATH, "export": EXPORT_PATH, "batch_stats": BATCH_STATS_PATH,
                "skipped": "skipped_leads.csv", "log": "langgraph_debug.log"}
    d = resolve_data_dir(data_dir, root)
    return {"db": os.path.join(d, "leads.db"), "export": os.path.join(d, "enriched_leads.csv"),
            "batch_stats": os.path.join(d, "batch_stats.csv"), "skipped": os.path.join(d, "skipped_leads.csv"),
            "log": os.path.join(d, "langgraph_debug.log")}
