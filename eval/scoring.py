"""Name matching and precision scoring for the owner-name eval.

Standard library only. No API calls. Imported by build_ground_truth.py,
test_scoring.py and run_regression.py.

Two precision numbers are always reported, never one:
  lenient - counts first-name-only, initials and goes-by names as correct.
            This is how the 2026-09-21 hand audit was graded, so it is the
            number comparable to the 17/41 baseline.
  strict  - requires first AND last name to match a real owner.
            This is the number that matters for outreach.
"""
import difflib
import re
import unicodedata

SENTINELS = {"", "unknown", "<unknown>", "na", "n/a", "none", "<error>", "error", "nan"}
SUFFIXES = {"jr", "sr", "ii", "iii", "iv"}
TITLES = {"dr", "mr", "mrs", "ms"}
FUZZY = 0.85  # tolerates spelling slips like Vancannon / Voncannon


def norm_tokens(text):
    """Lowercase, strip accents and punctuation, drop titles and suffixes."""
    if text is None:
        return []
    text = unicodedata.normalize("NFKD", str(text)).encode("ascii", "ignore").decode()
    text = re.sub(r"[\"\u201c\u201d]", " ", text.lower())
    text = text.replace("'", "").replace("\u2019", "")
    text = re.sub(r"[^a-z\s]", " ", text)
    toks = [t for t in text.split() if t not in SUFFIXES and t not in TITLES]
    return toks


def name_tokens(first, last=""):
    """Name tokens with sentinels removed. The pipeline writes "<UNKNOWN>" in the
    surname field when it only found a first name ("Danny", "<UNKNOWN>")."""
    return [t for t in norm_tokens(first) + norm_tokens(last) if t not in SENTINELS]


def is_blank(first, last=""):
    """True when no real name was given. The pipeline writes a sentinel in BOTH
    fields ("<UNKNOWN>", "<UNKNOWN>"), so every token must be checked, not the
    joined string ("unknown unknown" is not a sentinel)."""
    toks = norm_tokens(first) + norm_tokens(last)
    return all(t in SENTINELS for t in toks)


def _close(a, b):
    return a == b or difflib.SequenceMatcher(None, a, b).ratio() >= FUZZY


def match_person(first, last, person):
    """Compare one output name to one real owner.

    person: {"first": str, "last": str|None, "aliases": [str]}
    Returns "full", "partial" or None.
    """
    toks = name_tokens(first, last)
    if not toks:
        return None
    t_first = norm_tokens(person["first"])
    t_first = t_first[0] if t_first else ""
    t_last = norm_tokens(person.get("last") or "")
    t_last = t_last[-1] if t_last else ""
    aliases = [" ".join(norm_tokens(a)) for a in person.get("aliases", [])]

    whole = " ".join(toks)
    # A goes-by name recorded in the audit ("Kons", "Jody Hawk") counts as partial.
    if whole in aliases:
        return "full" if not t_last and whole == t_first else "partial"

    o_first, o_last = toks[0], (toks[-1] if len(toks) > 1 else "")
    first_ok = _close(o_first, t_first) or o_first in aliases

    if not o_last:                       # output is a single name
        if first_ok:
            return "full" if not t_last else "partial"
        return None
    if not t_last:                       # truth has no known surname (e.g. "Wize")
        return "partial" if first_ok else None
    if len(o_last) == 1:                 # an initial, e.g. "Aaron L"
        return "partial" if first_ok and t_last.startswith(o_last) else None
    if first_ok and _close(o_last, t_last):
        return "full"
    return None


def judge(first, last, truth):
    """Grade one output against one ground-truth row.

    Returns one of: "full", "partial", "wrong", "blank", "correct_blank",
    or "excluded" (ambiguous rows never count toward precision).
    """
    if truth["kind"] == "ambiguous":
        return "excluded"
    if is_blank(first, last):
        return "correct_blank" if truth["kind"] == "no_owner" else "blank"
    if truth["kind"] == "no_owner":
        return "wrong"                   # any name on a no-owner shop is invented
    best = None
    for person in truth["owners"]:
        m = match_person(first, last, person)
        if m == "full":
            return "full"
        best = best or m
    return best or "wrong"


def summarize(grades):
    """grades: list of judge() results for one system over a set of rows."""
    g = [x for x in grades if x != "excluded"]
    named = sum(x in ("full", "partial", "wrong") for x in g)
    full = g.count("full")
    partial = g.count("partial")
    return {
        "rows": len(g),
        "named": named,
        "match_rate": named / len(g) if g else 0.0,
        "correct_lenient": full + partial,
        "correct_strict": full,
        "precision_lenient": (full + partial) / named if named else 0.0,
        "precision_strict": full / named if named else 0.0,
        "wrong": g.count("wrong"),
        "correct_blanks": g.count("correct_blank"),
        "missed": g.count("blank"),
    }


def fmt(s, label):
    return (f"{label}: named {s['named']}/{s['rows']} (match rate {s['match_rate']:.0%}) | "
            f"precision lenient {s['correct_lenient']}/{s['named']} = {s['precision_lenient']:.1%} | "
            f"strict {s['correct_strict']}/{s['named']} = {s['precision_strict']:.1%} | "
            f"wrong {s['wrong']} | correct blanks {s['correct_blanks']}")
