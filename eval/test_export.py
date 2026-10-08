"""Free self-check for stage A (evidence, None sentinel, status, source tier, migration, export).
No API calls: the models and Brave are replaced by fakes, and real keys are never used.
    python eval/test_export.py

Checks that
  - None is the one 'nothing found' value, and a blank carries no evidence or confidence;
  - every named lead gets an evidence URL (Brave, About page, Gemini redirect link);
  - status is 'error' only for a blank that could be a failure, never for a real 'no owner found';
  - source_tier is a label: it is computed, recorded, and read by nothing in the graph;
  - the prompts the models see are unchanged (a missing name is still '<UNKNOWN>', or '<ERROR>' after a Haiku failure);
  - the results-table migration is safe to repeat and keeps old rows;
  - the export keeps found, no_owner_found, error and skipped apart, one row per lead.
"""
import csv
import logging
import os
import sqlite3
import sys
import tempfile
from types import SimpleNamespace

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, HERE)
# Dummy keys first: load_dotenv does not override variables that are already set, so this test can never
# use (or even see) the real keys.
os.environ["ANTHROPIC_API_KEY"] = "test-key-not-real"
os.environ["BRAVE_API_KEY"] = "test-key-not-real"
os.environ["GEMINI_API_KEY"] = "test-key-not-real"      # the Gemini path must never reach the network here

import evidence as ev                                   # noqa: E402
import export                                           # noqa: E402
import setup_database as sdb                            # noqa: E402

failures = []


def check(label, ok):
    print(("PASS " if ok else "FAIL ") + label)
    if not ok:
        failures.append(label)


# --- The None sentinel and helpers ------------------------------------------------------------
for s in (None, "", "  ", "<UNKNOWN>", "unknown", "UNKNOWN", "<ERROR>", "<E>", "N/A", "na", "None"):
    check(f"none_if_empty({s!r}) is None", ev.none_if_empty(s) is None)
check("a real name is kept and stripped", ev.none_if_empty("  Greg ") == "Greg")
check("domain_of strips www, port and path", ev.domain_of("https://www.BBB.org:443/us/nc/x") == "bbb.org")
check("url_kind: Google grounding link is a redirect",
      ev.url_kind("https://vertexaisearch.cloud.google.com/grounding-api-redirect/AUZ") == "redirect"
      and ev.url_kind("https://bbb.org/x") == "direct")

# --- Source tier is a label --------------------------------------------------------------------
T = ev.source_tier
check("tier: bbb.org primary", T("bbb.org") == "primary")
check("tier: linkedin/instagram/facebook/yelp supporting",
      all(T(d) == "supporting" for d in ("linkedin.com", "www.instagram.com", "facebook.com", "yelp.com")))
check("tier: booking platforms weak", all(T(d) == "weak" for d in ("booksy.com", "book.thecut.co", "vagaro.com", "fresha.com")))
check("tier: the lead's own site is strong", T("zephyrcuts.com", own_website="https://www.zephyrcuts.com/") == "strong")
check("tier: a booking platform stays weak even if the lead lists it as its website",
      T("booksy.com", own_website="https://booksy.com/en-us/x") == "weak")
check("tier: anything else, including news, is unclassified", T("charlotteobserver.com") == "unclassified")
check("tier: no domain, no tier", T("") is None)

e = ev.evidence_fields("https://vertexaisearch.cloud.google.com/grounding-api-redirect/AUZ1", ev.SRC_GEMINI,
                       domain="bbb.org", other_urls=["https://vertexaisearch.cloud.google.com/grounding-api-redirect/AUZ2"])
check("gemini redirect link: kept as returned, kind redirect, domain from the chunk title, tier from the domain",
      e["evidence_url"].endswith("AUZ1") and e["evidence_url_kind"] == "redirect" and e["evidence_domain"] == "bbb.org"
      and e["source_tier"] == "primary" and e["evidence_source"] == "gemini_grounding"
      and e["other_evidence_urls"].endswith("AUZ2"))
check("no URL means every evidence field is None", set(ev.evidence_fields(None, ev.SRC_BRAVE).values()) == {None})

# --- Status ------------------------------------------------------------------------------------
check("status: a name is always ok, even after an earlier failure", ev.compute_status(True, brave_errors=5, haiku_errors=2) == ("ok", None))
check("status: a clean blank is ok (a real 'no owner found')", ev.compute_status(False) == ("ok", None))
check("status: all searches failed + blank is error", ev.compute_status(False, brave_errors=5, n_queries=5)[0] == "error")
check("status: some searches failed + blank is still ok", ev.compute_status(False, brave_errors=2, n_queries=5) == ("ok", None))
check("status: a Haiku exception + blank is error", ev.compute_status(False, haiku_errors=1)[0] == "error")
check("status: a Sonnet exception + blank is error", ev.compute_status(False, sonnet_errors=1)[0] == "error")

# --- Migration ---------------------------------------------------------------------------------
OLD_RESULTS = """CREATE TABLE results (id INTEGER PRIMARY KEY AUTOINCREMENT, lead_id INTEGER NOT NULL,
  decision_maker_first_name TEXT, decision_maker_last_name TEXT, title TEXT, instagram TEXT, tiktok TEXT,
  facebook TEXT, linkedin TEXT, confidence TEXT, why_this_person TEXT, about_page_text TEXT,
  processed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"""
LEADS = """CREATE TABLE leads (id INTEGER PRIMARY KEY AUTOINCREMENT, place_id TEXT UNIQUE, business_name TEXT NOT NULL,
  website TEXT, address TEXT, phone TEXT, city TEXT, state TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"""

with tempfile.TemporaryDirectory() as d:
    old = os.path.join(d, "old.db")
    c = sqlite3.connect(old)
    c.execute(LEADS)
    c.execute(OLD_RESULTS)
    c.execute("INSERT INTO results (lead_id, decision_maker_first_name, decision_maker_last_name, confidence) VALUES (1,'Greg','Furr','high')")
    c.commit()
    c.close()
    added = sdb.migrate_results(old)
    check("migration adds all stage A columns", sorted(added) == sorted(n for n, _ in sdb.RESULTS_NEW_COLUMNS))
    check("migration is safe to repeat (adds nothing the second time)", sdb.migrate_results(old) == [])
    c = sqlite3.connect(old)
    row = c.execute("SELECT decision_maker_first_name, decision_maker_last_name, confidence, status, evidence_url FROM results").fetchone()
    c.close()
    check("migration keeps old rows; new columns are NULL", row == ("Greg", "Furr", "high", None, None))
    check("migration on a DB with no results table does nothing", sdb.migrate_results(os.path.join(d, "nothing.db")) == [])

    fresh = os.path.join(d, "fresh.db")
    real_path, sdb.DB_PATH = sdb.DB_PATH, fresh
    try:
        sdb.init_database(reset=False)
    finally:
        sdb.DB_PATH = real_path
    conn = sqlite3.connect(fresh)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(results)")}
    conn.close()                                         # Windows will not delete a file that is still open
    check("a freshly created database already has the stage A columns", {n for n, _ in sdb.RESULTS_NEW_COLUMNS} <= cols)

# --- Export ------------------------------------------------------------------------------------
with tempfile.TemporaryDirectory() as d:
    db = os.path.join(d, "t.db")
    c = sqlite3.connect(db)
    c.execute(LEADS)
    c.execute(OLD_RESULTS)
    c.commit()
    c.close()
    sdb.migrate_results(db)
    c = sqlite3.connect(db)
    leads = [("p1", "Found Shop"), ("p2", "Blank Shop"), ("p3", "Error Shop"), ("p4", "Mobile @ Barber"),
             ("p5", "No Result Shop"), ("p6", "Legacy Error Shop"), ("p7", "Legacy Found Shop"), ("p8", "Rerun Shop")]
    for pid, name in leads:
        c.execute("INSERT INTO leads (place_id, business_name, website) VALUES (?,?,?)", (pid, name, "https://x.example"))
    ins = ("INSERT INTO results (lead_id, decision_maker_first_name, decision_maker_last_name, title, confidence, "
           "delivered_confidence, status, error_detail, evidence_url, evidence_url_kind, evidence_domain, "
           "evidence_source, source_tier, goes_by_name, instagram) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)")
    c.execute(ins, (1, "Jane", "Rivera", "Owner", "medium", "high", "ok", None, "https://bbb.org/x", "direct", "bbb.org",
                    "brave_search", "primary", None, "Unknown"))
    c.execute(ins, (2, None, None, None, "low", None, "ok", None, None, None, None, None, None, None, None))
    c.execute(ins, (3, None, None, None, "low", None, "error", "all 5 Brave searches failed", None, None, None, None, None, None, None))
    c.execute(ins, (6, "<ERROR>", "<ERROR>", "", "low", None, None, None, None, None, None, None, None, None, None))
    c.execute(ins, (7, "Old", "Timer", "Owner", "high", None, None, None, None, None, None, None, None, None, None))
    c.execute(ins, (8, "Stale", "Name", "Owner", "high", "high", "ok", None, "https://old.example", "direct", "old.example", "brave_search", "unclassified", None, None))
    c.execute(ins, (8, None, None, None, "low", None, "ok", None, None, None, None, None, None, None, None))   # the re-run: now a blank
    c.commit()
    c.close()

    rows = export.build_rows(db)
    by = {r["business_name"]: r for r in rows}
    check("one row per lead, no duplicates", len(rows) == len(leads) == len({r["place_id"] for r in rows}))
    check("found: name, delivered confidence and evidence carried over",
          by["Found Shop"]["outcome"] == "found" and by["Found Shop"]["owner_first_name"] == "Jane"
          and by["Found Shop"]["confidence"] == "high" and by["Found Shop"]["evidence_url"] == "https://bbb.org/x"
          and by["Found Shop"]["source_tier"] == "primary")
    check("a model's 'Unknown' social becomes an empty cell", by["Found Shop"]["instagram"] is None)
    check("blank is its own outcome with empty name cells (no placeholder)",
          by["Blank Shop"]["outcome"] == "no_owner_found" and by["Blank Shop"]["owner_first_name"] is None
          and by["Blank Shop"]["evidence_url"] is None)
    check("error is not a blank: outcome error, reason kept, name and confidence empty",
          by["Error Shop"]["outcome"] == "error" and "Brave" in by["Error Shop"]["error_detail"]
          and by["Error Shop"]["owner_first_name"] is None and by["Error Shop"]["confidence"] is None)
    check("skipped by intake, with the reason",
          by["Mobile @ Barber"]["outcome"] == "skipped" and "@" in by["Mobile @ Barber"]["skip_reason"])
    check("a lead with no result is an error, not silently missing",
          by["No Result Shop"]["outcome"] == "error" and "no result" in by["No Result Shop"]["error_detail"])
    check("an old '<ERROR>' name is exported as an error, never as a name",
          by["Legacy Error Shop"]["outcome"] == "error" and by["Legacy Error Shop"]["owner_first_name"] is None)
    check("an old row without evidence still exports as found (and will be counted below)",
          by["Legacy Found Shop"]["outcome"] == "found" and by["Legacy Found Shop"]["confidence"] == "high")
    check("the latest result wins on a re-run", by["Rerun Shop"]["outcome"] == "no_owner_found")
    order = [r["outcome"] for r in rows]
    check("sorted found, no_owner_found, error, skipped", order == sorted(order, key=lambda o: export.OUTCOME_ORDER[o]))

    out = os.path.join(d, "out.csv")
    path, counts = export.write_export(db, out)
    check("counts add up and the found-without-evidence gate counts the old row",
          counts["found"] == 2 and counts["no_owner_found"] == 2 and counts["error"] == 3 and counts["skipped"] == 1
          and counts["found_without_evidence"] == 1)
    back = list(csv.DictReader(open(out, encoding="utf-8")))
    check("CSV columns are exactly the export columns", list(back[0].keys()) == export.COLUMNS)
    check("blank cells in the CSV are empty strings, not 'None' or '<UNKNOWN>'",
          not any(v in ("None", "<UNKNOWN>", "<ERROR>") for r in back for v in r.values()))

# --- The graph, offline: fakes for Haiku, Sonnet and the web ------------------------------------
sys.stdout.flush()                                       # graph swaps sys.stdout; keep the lines printed so far
import requests                                          # noqa: E402
import graph as fg                                 # noqa: E402
logging.disable(logging.CRITICAL)


def owner(first, last, title="Owner", conf="high", snippet="", url="", **kw):
    f = dict(linkedin="Unknown", instagram="Unknown", tiktok="Unknown", facebook="Unknown", why_this_person="because")
    f.update(kw)
    return fg.OwnerResult(decision_maker_first_name=first, decision_maker_last_name=last, title=title,
                          confidence=conf, evidence_snippet=snippet, evidence_url=url, **f)


class FakeHaiku:
    def __init__(self, search=None, about=None, boom=False):
        self.search, self.about, self.boom, self.prompts = search, about, boom, []

    def invoke(self, prompt):
        self.prompts.append(prompt)
        if self.boom and "identifying business decision-makers" in prompt:
            raise RuntimeError("haiku down")
        parsed = self.about if prompt.startswith("From this About page") else self.search
        return {"raw": SimpleNamespace(usage_metadata={"input_tokens": 10, "output_tokens": 5}),
                "parsed": parsed or owner("Unknown", "Unknown", "Unknown", "low")}


class FakeSonnet:
    def __init__(self, text='{"verified_confidence":"low","verified_first_name":"Unknown","verified_last_name":"Unknown",'
                            '"verified_evidence_snippet":"","verified_evidence_url":"","alternative_first_name":"",'
                            '"alternative_last_name":"","verification_reasoning":"none"}'):
        self.text, self.prompts = text, []

    def invoke(self, prompt):
        self.prompts.append(prompt)
        return SimpleNamespace(content=self.text, usage_metadata={"input_tokens": 20, "output_tokens": 8})


BBB = "https://www.bbb.org/us/nc/charlotte/profile/barber/Zephyr-Cuts-0473-1"
BRAVE_HIT = [{"url": BBB, "title": "Zephyr Cuts | BBB Business Profile", "description": "Zephyr Cuts owner Jane Rivera, Charlotte NC."}]
SHOP_ONLY = [{"url": "https://www.yelp.com/biz/zephyr-cuts-charlotte", "title": "Zephyr Cuts - Charlotte, NC - Yelp",
              "description": "Haircuts at Zephyr Cuts."}]
ABOUT_HTML = "Our founder and owner Jane Rivera started the shop in 2011."


def run_lead(haiku, sonnet=None, brave=None, brave_fails=False, website="", about_ok=False):
    sonnet = sonnet or FakeSonnet()

    def fake_get(url, **kw):
        if "api.search.brave.com" in url:
            if brave_fails:
                raise requests.exceptions.Timeout()
            return SimpleNamespace(status_code=200, raise_for_status=lambda: None,
                                   json=lambda: {"web": {"results": brave or []}, "locations": {"results": []}})
        if about_ok and url.endswith("/about"):
            return SimpleNamespace(status_code=200, text=ABOUT_HTML)
        return SimpleNamespace(status_code=404, text="")

    def no_network(*a, **k):
        raise AssertionError("a test tried to make a real POST")

    saved = (fg.structured_model, fg.reasoning_model, requests.get, fg.brave_key, requests.post, fg.gemini_path.ask_gemini)
    fg.structured_model, fg.reasoning_model, requests.get, fg.brave_key = haiku, sonnet, fake_get, "k"
    requests.post = no_network                           # any real Gemini call would fail this test loudly
    fg.gemini_path.ask_gemini = lambda *a, **k: {"status": "error", "error": "fake: offline test", "cost_usd": 0.0, "seconds": 0.0}
    try:
        res = fg.app.invoke({"business_name": "Zephyr Cuts", "lead_id": 1, "website": website, "address": "1 Main St, Charlotte, NC 28202, USA",
                             "phone": "", "city": "Charlotte", "state": "NC"})
    finally:
        fg.structured_model, fg.reasoning_model, requests.get, fg.brave_key, requests.post, fg.gemini_path.ask_gemini = saved
    return res, haiku, sonnet


# 1. Haiku high with evidence: delivered at once; verification skipped
res, h, s = run_lead(FakeHaiku(search=owner("Jane", "Rivera", conf="high", snippet="owner Jane Rivera", url=BBB.lower())), brave=BRAVE_HIT)
check("found via Haiku: names, high confidence, status ok",
      (res["decision_maker_first_name"], res["decision_maker_last_name"], res["delivered_confidence"], res["status"])
      == ("Jane", "Rivera", "high", "ok") and res["error_detail"] is None and not s.prompts)
check("evidence URL is recorded exactly as the source block prints it (original case), kind direct, tier primary",
      res["evidence_url"] == BBB and res["evidence_url_kind"] == "direct" and res["evidence_domain"] == "bbb.org"
      and res["source_tier"] == "primary" and res["evidence_source"] == "brave_search")
check("a model's 'Unknown' title/socials become None; goes_by_name stays None on the Brave path",
      res["instagram"] is None and res["linkedin"] is None and res["goes_by_name"] is None and res["title"] == "Owner")

# 2. Nothing found anywhere: a real blank
res, h, s = run_lead(FakeHaiku(), brave=SHOP_ONLY)
check("real blank: names None, no evidence, no confidence, status ok",
      res["decision_maker_first_name"] is None and res["decision_maker_last_name"] is None and res["title"] is None
      and res["evidence_url"] is None and res["source_tier"] is None and res["delivered_confidence"] is None
      and res["status"] == "ok" and res["error_detail"] is None)
check("no '<UNKNOWN>' or '<ERROR>' string anywhere in a blank result",
      not any(isinstance(v, str) and v in ("<UNKNOWN>", "<ERROR>", "") for k, v in res.items()
              if k in ("decision_maker_first_name", "decision_maker_last_name", "title", "instagram", "tiktok", "facebook", "linkedin",
                       "alternative_first_name", "alternative_last_name", "evidence_url")))

# 3. Every Brave search fails
res, h, s = run_lead(FakeHaiku(), brave_fails=True)
check("all searches failed + blank -> status error, not a blank",
      res["status"] == "error" and "all 5 Brave searches failed" in res["error_detail"] and res["decision_maker_first_name"] is None)

# 4. Haiku raises: the failure is a status, never a name; Sonnet still sees '<ERROR>' exactly as before
res, h, s = run_lead(FakeHaiku(boom=True), brave=SHOP_ONLY)
check("Haiku exception + blank -> status error, names None", res["status"] == "error" and "Haiku" in res["error_detail"]
      and res["decision_maker_first_name"] is None)
check("the verify prompt still shows '<ERROR> <ERROR>' after a Haiku failure (prompt unchanged)",
      "Haiku's extraction: <ERROR> <ERROR>" in s.prompts[0] and "support <ERROR> <ERROR> as primary" in s.prompts[0])

# 5. Sonnet delivers a fuller name than Haiku: its evidence, and Haiku's title/socials no longer apply
sonnet_json = ('{"verified_confidence":"medium","verified_first_name":"Jane","verified_last_name":"Rivera",'
               '"verified_evidence_snippet":"owner Jane Rivera","verified_evidence_url":"%s","alternative_first_name":"",'
               '"alternative_last_name":"","verification_reasoning":"ok"}' % BBB.lower())
res, h, s = run_lead(FakeHaiku(search=owner("Jane", "Unknown", conf="medium", snippet="owner Jane Rivera", url=BBB.lower(),
                                            instagram="@jane")), FakeSonnet(sonnet_json), brave=BRAVE_HIT)
check("verify path: Sonnet's name delivered at its verified confidence with its evidence",
      (res["decision_maker_first_name"], res["decision_maker_last_name"], res["delivered_confidence"]) == ("Jane", "Rivera", "medium")
      and res["evidence_url"] == BBB and res["status"] == "ok")
check("a different person than Haiku's: Haiku's title and socials are dropped", res["title"] is None and res["instagram"] is None)
check("the verify prompt shows a missing surname as '<UNKNOWN>' (prompt unchanged)",
      "Haiku's extraction: Jane <UNKNOWN>" in s.prompts[0] and "support Jane <UNKNOWN> as primary" in s.prompts[0])

# 6. About page delivers: the evidence URL is the page actually fetched, tier strong
about = owner("Jane", "Rivera", conf="high", snippet="founder and owner Jane Rivera")
res, h, s = run_lead(FakeHaiku(about=about), brave=SHOP_ONLY, website="https://zephyrcuts.example/", about_ok=True)
check("about path: name delivered with the fetched About URL as evidence",
      (res["decision_maker_first_name"], res["decision_maker_last_name"], res["status"]) == ("Jane", "Rivera", "ok")
      and res["evidence_url"] == "https://zephyrcuts.example/about" and res["evidence_source"] == "about_page"
      and res["evidence_url_kind"] == "direct")
check("source_tier is strong for the shop's own page", res["source_tier"] == "strong")

# 7. source_tier never influences anything: same answer when the label is forced
real_tier = ev.source_tier
try:
    ev.source_tier = lambda *a, **k: "weak"
    res_w, _, _ = run_lead(FakeHaiku(search=owner("Jane", "Rivera", conf="high", snippet="owner Jane Rivera", url=BBB.lower())), brave=BRAVE_HIT)
finally:
    ev.source_tier = real_tier
check("forcing every tier to 'weak' changes the label only, not the name, confidence or status",
      res_w["source_tier"] == "weak" and (res_w["decision_maker_first_name"], res_w["delivered_confidence"], res_w["status"])
      == ("Jane", "high", "ok"))

print("\nStage A tests passed." if not failures else f"\n{len(failures)} FAILED: {failures}")
sys.exit(1 if failures else 0)
