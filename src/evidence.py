"""Evidence, source-tier and status rules shared by the graph, the export and the Gemini script.

Pure Python, no API calls, no heavy imports, so it can be tested for free.

Stage A is RECORD-KEEPING ONLY. `source_tier` is a label: nothing in confidence, routing or
delivery reads it. (The source-authority rule in CLAUDE.md stays a policy, not enforced.)
"""
from urllib.parse import urlparse

# The one "nothing found" value everywhere in the pipeline's outputs is None.
# These are the strings models and older code used for it.
NO_NAME = {"", "<UNKNOWN>", "<E>", "<ERROR>", "UNKNOWN", "N/A", "NA", "NONE"}

SRC_BRAVE = "brave_search"
SRC_ABOUT = "about_page"
SRC_GEMINI = "gemini_grounding"

GEMINI_REDIRECT_MARK = "vertexaisearch.cloud.google.com/grounding-api-redirect"

# Tier labels. Recorded only; see the module docstring.
TIER_PRIMARY, TIER_STRONG, TIER_SUPPORTING, TIER_WEAK, TIER_UNCLASSIFIED = (
    "primary", "strong", "supporting", "weak", "unclassified")
PRIMARY_DOMAINS = ("bbb.org",)          # state registries would go here; we never search them
SUPPORTING_DOMAINS = ("linkedin.com", "instagram.com", "facebook.com", "tiktok.com", "yelp.com")
WEAK_DOMAINS = ("booksy.com", "thecut.co", "squareup.com", "square.site", "vagaro.com",
                "styleseat.com", "fresha.com")


def is_none_like(s):
    """True for None and every string older code or a model used to mean 'no value'."""
    return s is None or str(s).strip().upper() in NO_NAME


def none_if_empty(s):
    """None for any 'no value', else the stripped string. Use on name, title and social fields."""
    return None if is_none_like(s) else str(s).strip()


def domain_of(url):
    """Host without 'www.' and without a port, lowercased. '' if there is none."""
    if not url:
        return ""
    u = str(url).strip()
    host = urlparse(u if "//" in u else f"//{u}").netloc.lower()
    host = host.split("@")[-1].split(":")[0]
    return host[4:] if host.startswith("www.") else host


def _matches(domain, suffixes):
    return any(domain == s or domain.endswith("." + s) for s in suffixes)


def url_kind(url):
    """'redirect' for a Google grounding link (the real page is not resolved yet), else 'direct'."""
    return "redirect" if GEMINI_REDIRECT_MARK in (url or "") else "direct"


def source_tier(domain, own_website=""):
    """A label for how much a source can be trusted. Label only.

    domain: the evidence site's domain (for a Gemini redirect link, the grounding chunk's title).
    own_website: the lead's own site; a match is the shop's own page. Booking and social platforms
    keep their own tier even when the lead lists one as its 'website'."""
    d = domain_of(domain) if "/" in (domain or "") else (domain or "").lower().strip()
    if not d:
        return None
    if _matches(d, PRIMARY_DOMAINS):
        return TIER_PRIMARY
    if _matches(d, SUPPORTING_DOMAINS):
        return TIER_SUPPORTING
    if _matches(d, WEAK_DOMAINS):
        return TIER_WEAK
    own = domain_of(own_website)
    if own and (d == own):
        return TIER_STRONG
    return TIER_UNCLASSIFIED


def evidence_fields(url, source, own_website="", domain=None, other_urls=None):
    """The evidence columns for one delivered name. url None means no evidence: every field None.

    domain: pass it when the URL carries no usable host (a Gemini redirect link; use the chunk title)."""
    if not url:
        return empty_evidence()
    dom = domain if domain is not None else domain_of(url)
    return {
        "evidence_url": url,
        "evidence_domain": dom or None,
        "evidence_source": source,
        "evidence_url_kind": url_kind(url),
        "source_tier": source_tier(dom, own_website),
        "other_evidence_urls": " | ".join(other_urls) if other_urls else None,
    }


def empty_evidence():
    return {"evidence_url": None, "evidence_domain": None, "evidence_source": None,
            "evidence_url_kind": None, "source_tier": None, "other_evidence_urls": None}


def compute_status(has_name, brave_errors=0, n_queries=5, haiku_errors=0, sonnet_errors=0):
    """('ok'|'error', detail). An error is a BLANK that could be a failure rather than a real
    'no owner found': every search failed, or a model call raised. A delivered name is always ok
    (earlier failures stay in the cost log). A blank where everything ran is ok: a real blank."""
    if has_name:
        return "ok", None
    why = []
    if n_queries and brave_errors >= n_queries:
        why.append(f"all {n_queries} Brave searches failed")
    if haiku_errors:
        why.append(f"{haiku_errors} Haiku call(s) raised")
    if sonnet_errors:
        why.append(f"{sonnet_errors} Sonnet call(s) raised")
    return ("error", "; ".join(why)) if why else ("ok", None)
