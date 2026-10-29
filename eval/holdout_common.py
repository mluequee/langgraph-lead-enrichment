"""Shared pieces of the stage F holdout tooling. Standard library plus eval/scoring.py only: no network, no
graph import, no keys. Imported by holdout_intake.py, make_holdout_worksheet.py, seal_holdout.py and
score_holdout.py so the rules in eval/HOLDOUT_PROTOCOL.md exist in exactly one place.
"""
import csv
import hashlib
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
from scoring import norm_tokens                                   # noqa: E402

PROTOCOL_SEED = 20261011            # protocol section 3, decision D13
SAMPLE_SIZE = 45                    # protocol section 3, decision D2

# The columns of the clean lead CSV. setup_database.py reads business_name, website, address, phone and id
# and ignores the rest; the other columns travel with the lead for the worksheet and the scorer.
CLEAN_COLUMNS = ["business_name", "website", "address", "phone", "id", "google_url", "city", "state", "flags"]


def draw_key(seed, place_id):
    """Protocol section 3: sort the filtered population by sha256("<seed>:<place_id>") hex digest, ascending."""
    return hashlib.sha256(f"{seed}:{place_id}".encode("utf-8")).hexdigest()


def order_key(seed, place_id):
    """Protocol section 3: worksheet row order, sha256("<seed>:order:<place_id>") hex digest, ascending."""
    return hashlib.sha256(f"{seed}:order:{place_id}".encode("utf-8")).hexdigest()


def draw_sample(place_ids, n, seed=PROTOCOL_SEED):
    """The first n Place IDs by draw_key. If n is None or at least the population size, all of them (in draw order).
    Independent of the order the IDs arrive in."""
    ordered = sorted(set(place_ids), key=lambda p: (draw_key(seed, p), p))
    return ordered if n is None or n >= len(ordered) else ordered[:n]


def name_key(name):
    """Stable lookup key for a business name (case, punctuation, accents, digits)."""
    return " ".join(t for t in norm_tokens(name) if not t.isdigit())


_SEPARATORS = (" - ", " – ", " — ", " | ", " @ ", " (", ", ")


def chain_stem(name):
    """The brand part of a business name: everything before a location suffix.
    "Roosters Men's Grooming Center - Ballantyne" -> "roosters mens grooming center"."""
    text = name or ""
    cut = len(text)
    for sep in _SEPARATORS:
        i = text.find(sep)
        if i > 0:
            cut = min(cut, i)
    return name_key(text[:cut])


def file_sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 16), b""):
            h.update(block)
    return h.hexdigest()


def read_clean_csv(path):
    """The clean lead CSV as a list of dicts (utf-8-sig, so a byte-order mark never breaks the first header)."""
    with open(path, newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


# --- the verification worksheet (protocol section 6) -----------------------------------------------------
VERDICT_OWNER = "Owner found"
VERDICT_NO_OWNER = "No owner public"
VERDICT_AMBIGUOUS = "Ambiguous"
VERDICT_UNDETERMINED = "Not determined"
VERDICTS = [VERDICT_OWNER, VERDICT_NO_OWNER, VERDICT_AMBIGUOUS, VERDICT_UNDETERMINED]

# Decision D5: the six original types plus four more.
SOURCE_TYPES = ["LinkedIn", "News", "BBB", "Booksy", "Video", "Directory",
                "Own website", "Social (IG/FB)", "GA registry", "Other"]

# One row per lead. Everything left of `verdict` comes from the lead list; everything from `verdict` on is
# filled in by hand, blind. `place_id` is the join key to the pipeline's export.
VERIFY_COLUMNS = ["#", "place_id", "business_name", "address", "phone", "website", "google_url",
                  "verdict", "owner_first", "owner_last", "goes_by", "co_owner_first", "co_owner_last",
                  "source_type", "source_url", "source_type_2", "source_url_2", "checked", "minutes", "notes"]
FIRST_HAND_COLUMN = VERIFY_COLUMNS.index("verdict")
CHECKED_VALUES = ["Yes", "No"]
