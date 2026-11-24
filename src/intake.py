"""Intake filter: drop records that are not shops before the pipeline sees them.

One rule set, used by the pipeline (first_graph.process_leads_batch) and by the
regression scorer (eval/run_regression.py), so the two can never disagree about
which leads count.

A skip is never silent: each one is logged and written to a skipped-leads file.
Known limit: no pattern catches every person-as-business record in a new city,
so eyeball the lead list before a run.
"""
import csv
import logging

SKIPPED_FILE = "skipped_leads.csv"       # gitignored by *.csv; holds real business names

# Non-shop records with no '@' in the name, keyed on Google Place ID.
# Add a line per record. Keep the reason neutral; names stay in your local notes, not here.
PLACE_ID_DENYLIST = {
    "ChIJn0eh8yihVogR7mNmS1xrqTU": "not a storefront shop",
    "ChIJQ8HwirId9YgRe35o2JyXgIo": "not a storefront shop",
    "ChIJcRZdkIb99IgR_7S8cmvhSkc": "not a storefront shop",
    "ChIJ00xVaYMD9YgRl1_VkfvSHF0": "not a storefront shop",
    "ChIJL00QyDAF9YgRkSeshWjNHCg": "not a storefront shop",
    "ChIJ92J4AGkD9YgRVG9U0_6EWN0": "not a storefront shop",
    "ChIJzb3jvaH59IgRxcS-Po-OP_Q": "not a storefront shop",
    "ChIJP1u9CJoD9YgRI0UpIdXYwfY": "not a storefront shop",
}

AT_SIGN_REASON = "business name contains '@' (mobile or independent barber, or a person-as-business record)"


def skip_reason(business_name, place_id=""):
    """Why this lead is skipped, or None to keep it.

    The '@' rule deliberately also drops real shops such as 'Legendary Cuts @
    University'. Losing one lead is the accepted cost of a rule that works on a
    city nobody has audited yet.
    """
    if place_id and place_id in PLACE_ID_DENYLIST:
        return f"Place ID denylist: {PLACE_ID_DENYLIST[place_id]}"
    if "@" in (business_name or ""):
        return AT_SIGN_REASON
    return None


def filter_leads(leads, log=None):
    """Split leads (objects with .business_name and .place_id) into (kept, skipped).

    skipped is a list of (lead, reason). Every skip is logged.
    """
    log = log or logging.getLogger("intake")
    kept, skipped = [], []
    for lead in leads:
        reason = skip_reason(lead.business_name, getattr(lead, "place_id", ""))
        if reason is None:
            kept.append(lead)
        else:
            skipped.append((lead, reason))
            log.info(f"INTAKE SKIP: {lead.business_name!r} (place_id {getattr(lead, 'place_id', '') or 'n/a'}) - {reason}")
    log.info(f"Intake: {len(kept)} kept, {len(skipped)} skipped")
    return kept, skipped


def write_skipped_file(skipped, path=SKIPPED_FILE):
    """Write the skipped leads so nothing disappears silently. Overwrites each run."""
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["place_id", "business_name", "website", "reason"])
        for lead, reason in skipped:
            w.writerow([getattr(lead, "place_id", ""), lead.business_name, lead.website, reason])
    return path
