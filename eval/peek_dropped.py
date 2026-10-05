"""Retrieval-only: results the relevance gate DROPS that still contain the brand word."""
import os, sys
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0,ROOT)
import first_graph as fg

wanted = [n.lower() for n in sys.argv[1:]]
for lead in fg.LeadDatabase().get_all_leads():
    if not any (w in lead.business_name.lower() for w in wanted):
        continue
    state = fg.prep_lead({"business_name": lead.business_name, "lead_id": lead.id,
                          "website": lead.website, "address": lead.address,
                          "phone": lead.phone, "city": lead.city, "state": lead.state})
    state.update(fg.build_queries(state))
    for i in range(5):
        state["search_results"] += fg._brave_search(state, i)["search_results"]
    brand = fg._brand_tokens(lead.business_name)
    print("\n=== " + lead.business_name, "| brand tokens:", brand)
    seen = set()
    for search_set in state["search_results"]:
        for r in search_set["results"]:
            url, title, desc = r.get("url", ""), r.get("title", ""), r.get("description", "")
            if url in seen:
                continue
            seen.add(url)
            if fg._is_anchor_result(title, desc, url.lower(), lead.phone, lead.address):
                continue
            if fg._result_is_about_shop(title, desc, url.lower(), lead.business_name):
                continue
            squash = "".join(fg._norm_words(f"{title} {url}"))
            if any(t in squash for t in brand):
                print(" DROPPED |", title[:60], "|", url[:80])