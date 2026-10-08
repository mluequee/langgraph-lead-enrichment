"""Reconcile the Brave answer and the Gemini answer for one lead (stage C2, option 5).

Pure Python, no API calls. Confidence labels and source_tier never decide anything here.

    Gemini unavailable (error / disabled)  -> the Brave answer, unchanged (a Gemini failure never costs an answer)
    both blank                             -> blank
    both name the same person              -> deliver the fuller name, confidence high (medium if no surname),
                                              Brave's direct URL as evidence when Brave's name is delivered,
                                              the other path's URL goes in other_evidence_urls
    only one path names someone            -> deliver it at medium, but only with a surname (and, for Gemini,
                                              only with an evidence URL); a first-name-only answer from one
                                              path alone is held back (blank)
    the paths name different people        -> deliver nobody: outcome needs_review, both candidates and their
                                              sources kept (co-owners can both be right; the client decides)

A first-name-only answer is delivered only when BOTH paths independently name that same first name.
"""
import difflib
import re
import unicodedata

SUFFIXES = {"jr", "sr", "ii", "iii", "iv"}
FUZZY = 0.85        # tolerates a spelling slip such as Vancannon / Voncannon

AGREE, BRAVE_ONLY, GEMINI_ONLY, CONFLICT, BOTH_BLANK, UNAVAILABLE = (
    "agree", "brave_only", "gemini_only", "conflict", "both_blank", "gemini_unavailable")


def _tokens(text):
    t = unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode().lower()
    t = t.replace("'", "").replace("’", "")
    return [w for w in re.sub(r"[^a-z\s]", " ", t).split() if w not in SUFFIXES]


def _close(a, b):
    return a == b or difflib.SequenceMatcher(None, a, b).ratio() >= FUZZY


def has_surname(n):
    """A real last name: not empty and not a single initial ('Laura D.')."""
    toks = _tokens(n.get("last"))
    return bool(toks) and len(toks[-1]) > 1


def same_person(a, b):
    """The same person? First names must match (fuzzily). If both have a surname it must match too; one
    surname missing, or one only an initial that starts the other, still counts as the same person."""
    fa, fb = _tokens(a.get("first")), _tokens(b.get("first"))
    if not fa or not fb or not _close(fa[0], fb[0]):
        return False
    la, lb = _tokens(a.get("last")), _tokens(b.get("last"))
    if not la or not lb:
        return True
    x, y = la[-1], lb[-1]
    if len(x) == 1 or len(y) == 1:
        return x[0] == y[0]
    return _close(x, y)


def _blank_ev():
    return {"evidence_url": None, "evidence_domain": None, "evidence_source": None, "evidence_url_kind": None,
            "source_tier": None, "other_evidence_urls": None}


def _candidate(path, n):
    ev = n.get("evidence") or {}
    return {"source": path, "first": n.get("first"), "last": n.get("last"), "title": n.get("title"),
            "confidence": n.get("confidence"), "evidence_url": ev.get("evidence_url"),
            "evidence_domain": ev.get("evidence_domain"), "evidence_url_kind": ev.get("evidence_url_kind"),
            "source_tier": ev.get("source_tier")}


def _merge_other(winner_ev, other_ev):
    """The loser's URLs ride along in other_evidence_urls; the winner's own URL stays the evidence.
    If the winner has no URL at all, the other path's evidence is promoted (a named lead never lacks one)."""
    if not (winner_ev or {}).get("evidence_url") and (other_ev or {}).get("evidence_url"):
        return dict(other_ev)
    urls =[u for u in [(other_ev or {}).get("evidence_url")] +
            [x.strip() for x in ((other_ev or {}).get("other_evidence_urls") or "").split("|")] if u]
    urls += [x.strip() for x in ((winner_ev or {}).get("other_evidence_urls") or "").split("|") if x.strip()]
    seen, out = set(), []
    for u in urls:
        if u not in seen and u != (winner_ev or {}).get("evidence_url"):
            seen.add(u)
            out.append(u)
    ev = dict(winner_ev or _blank_ev())
    ev["other_evidence_urls"] = " | ".join(out) if out else None
    return ev


def reconcile(brave, gemini):
    """brave: None (no name) or {first, last, title, confidence, evidence: stage A evidence fields}.
    gemini: {status, first, last, goes_by, title, confidence, evidence} (status 'ok', else it is ignored).

    Returns {case, deliver, first, last, title, confidence, goes_by, evidence, candidates, keep_brave}.
    keep_brave True means: leave the Brave answer exactly as it is (the fallback)."""
    g = gemini if (gemini or {}).get("status") == "ok" and ((gemini.get("first")) or (gemini.get("last"))) else None
    out = {"case": None, "deliver": False, "first": None, "last": None, "title": None, "confidence": None,
           "goes_by": None, "evidence": _blank_ev(), "candidates": [], "keep_brave": False}
    if (gemini or {}).get("status") != "ok":
        out.update(case=UNAVAILABLE, keep_brave=True, deliver=bool(brave))
        return out
    if not brave and not g:
        out["case"] = BOTH_BLANK
        return out
    if brave and g:
        if same_person(brave, g):
            fuller_is_gemini = has_surname(g) and not has_surname(brave)
            win, lose = (g, brave) if fuller_is_gemini else (brave, g)
            out.update(case=AGREE, deliver=True, first=win["first"], last=win["last"],
                       title=win.get("title") or lose.get("title"),
                       confidence="high" if has_surname(win) else "medium",
                       goes_by=g.get("goes_by"), evidence=_merge_other(win.get("evidence"), lose.get("evidence")))
            return out
        out.update(case=CONFLICT, candidates=[_candidate("brave", brave), _candidate("gemini", g)])
        return out
    if brave:
        out["case"] = BRAVE_ONLY
        if has_surname(brave):
            out.update(deliver=True, first=brave["first"], last=brave["last"], title=brave.get("title"),
                       confidence="medium", evidence=dict(brave.get("evidence") or _blank_ev()))
        return out
    out["case"] = GEMINI_ONLY
    if has_surname(g) and (g.get("evidence") or {}).get("evidence_url"):
        out.update(deliver=True, first=g["first"], last=g["last"], title=g.get("title"), confidence="medium",
                   goes_by=g.get("goes_by"), evidence=dict(g["evidence"]))
    return out
