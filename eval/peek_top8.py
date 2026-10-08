"""Retrieval-only peek: which 8 pages reach the model for the named leads."""
import os, sys
from collections import Counter
from urllib.parse import urlparse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
import graph as fg

wanted = [n.lower() for n in sys.argv[1:]]
for lead in fg.LeadDatabase().get_all_leads():
    if not any(w in lead.business_name.lower() for w in wanted):
        continue
    state = fg.prep_lead({"business_name": lead.business_name, "lead_id": lead.id,
                          "website": lead.website, "address": lead.address,
                          "phone": lead.phone, "city": lead.city, "state": lead.state})
    state.update(fg.build_queries(state))
    for i in range(5):
        state["search_results"] += fg._brave_search(state, i)["search_results"]
    state.update(fg.combine_and_score(state))
    print("\n===" + lead.business_name)
    domains = Counter()
    for block in state["combined_source_text"].split("\n---\n"):
        lines = block.splitlines()
        url = next((l[5:].strip() for l in lines if l.startswith("URL:")), "")
        domains[urlparse(url).netloc.replace("www.", "")] += 1
        print(" ", lines[0][:60], "|", url[:90])
    print("   domains:", dict(domains))