"""Free self-check for stage C2 (reconcile, Gemini timeouts and error fallback, redirect resolution, the join).
No network: every request is replaced by a fake, and a real POST or GET fails the test loudly.
    python eval/test_c2.py
"""
import json
import logging
import os
import sys
import threading
import time
from types import SimpleNamespace

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)
for k in ("ANTHROPIC_API_KEY", "BRAVE_API_KEY", "GEMINI_API_KEY"):
    os.environ[k] = "test-key-not-real"                 # load_dotenv never overrides these, so real keys are never used

import requests                                         # noqa: E402
import evidence as ev                                   # noqa: E402
import gemini_path as gp                                # noqa: E402
import reconcile as rc                                  # noqa: E402

failures = []


def check(label, ok):
    print(("PASS " if ok else "FAIL ") + label)
    if not ok:
        failures.append(label)


def boom(*a, **k):
    raise AssertionError("a test tried to use the real network")


requests.post, requests.get = boom, boom

# --- reconcile (option 5) ----------------------------------------------------------------------
def N(first, last, conf="high", url="https://bbb.org/x", title="Owner", src="brave_search", other=None, goes_by=None, status=None):
    d = {"first": first, "last": last, "title": title, "confidence": conf, "goes_by": goes_by,
         "evidence": ev.evidence_fields(url, src, other_urls=other) if url else ev.empty_evidence()}
    if status:
        d["status"] = status
    return d


def G(first, last, **k):
    return N(first, last, status="ok", url=k.pop("url", "https://example.com/g"), src="gemini_grounding", **k)


R = rc.reconcile
r = R(N("Jane", "Rivera"), {"status": "error", "error": "timeout"})
check("Gemini error: the Brave answer stands untouched (keep_brave)", r["keep_brave"] and r["case"] == "gemini_unavailable" and r["deliver"])
check("Gemini disabled behaves the same", R(None, {"status": "disabled"})["keep_brave"])
check("Gemini error and Brave blank: still a blank, nothing invented", not R(None, {"status": "error"})["deliver"])
check("both blank", R(None, G(None, None))["case"] == "both_blank" and not R(None, G(None, None))["deliver"])

r = R(N("Greg", "Furr", url="https://bbb.org/f"), G("Greg", "Furr", url="https://example.com/g"))
check("agree: delivered at HIGH, Brave's direct URL is the evidence, Gemini's rides in other_evidence_urls",
      r["case"] == "agree" and r["confidence"] == "high" and r["evidence"]["evidence_url"] == "https://bbb.org/f"
      and "https://example.com/g" in r["evidence"]["other_evidence_urls"])
r = R(N("Pablo", None, url="https://booksy.com/p"), G("Pablo", "Ponce"))
check("agree with a fuller name from Gemini: the fuller name is delivered, with Gemini's evidence and Brave's URL alongside",
      r["case"] == "agree" and (r["first"], r["last"]) == ("Pablo", "Ponce") and r["evidence"]["evidence_url"] == "https://example.com/g"
      and "https://booksy.com/p" in r["evidence"]["other_evidence_urls"] and r["confidence"] == "high")
r = R(N("Victoria", "Voncannon"), G("Victoria", "Vancannon"))
check("a spelling slip in the surname is still the same person", r["case"] == "agree")
r = R(N("Laura", "D."), G("Laura", "Dudek"))
check("an initial that starts the surname is the same person; the full surname is delivered", r["case"] == "agree" and r["last"] == "Dudek")
r = R(N("Pablo", None), G("Pablo", None))
check("first-name-only on BOTH paths, same first name: delivered, capped at medium",
      r["deliver"] and r["confidence"] == "medium" and r["last"] is None)
r = R(N("Pablo", None), None if False else G(None, None))
check("first-name-only from Brave alone is held back (blank), not delivered", r["case"] == "brave_only" and not r["deliver"])
r = R(None, G("Sid", None))
check("first-name-only from Gemini alone is held back", r["case"] == "gemini_only" and not r["deliver"])
r = R(N("Ron", "Mumpower"), G(None, None))
check("Brave alone with a surname: delivered at MEDIUM, Brave's evidence", r["deliver"] and r["confidence"] == "medium"
      and r["evidence"]["evidence_url"] == "https://bbb.org/x")
r = R(None, G("Clarence", "Moore", conf="high"))
check("Gemini alone with a surname and evidence: delivered at MEDIUM whatever it said about itself",
      r["deliver"] and r["confidence"] == "medium" and r["evidence"]["evidence_source"] == "gemini_grounding")
r = R(None, G("Clarence", "Moore", url=None))
check("Gemini alone without any evidence URL is not delivered (a named lead always has evidence)", not r["deliver"])
r = R(N("Niki", "Voncannon"), G("Victoria", "Voncannon"))
check("different people (co-owners): nobody delivered, both candidates kept with sources",
      r["case"] == "conflict" and not r["deliver"] and [c["source"] for c in r["candidates"]] == ["brave", "gemini"]
      and all(c["evidence_url"] for c in r["candidates"]))
a = R(N("Greg", "Furr", conf="high", src="about_page"), G("Hunter", "Griffin", conf="low"))
b = R(N("Greg", "Furr", conf="low"), G("Hunter", "Griffin", conf="high"))
check("confidence labels and source_tier never decide a conflict: both are needs_review either way",
      a["case"] == b["case"] == "conflict" and not a["deliver"] and not b["deliver"])
real_tier = ev.source_tier
ev.source_tier = lambda *x, **y: "weak"
r_forced = R(N("Greg", "Furr"), G("Greg", "Furr"))
ev.source_tier = real_tier
check("forcing a tier label changes no reconcile decision", r_forced["case"] == "agree" and r_forced["deliver"])

# --- Redirect resolution -------------------------------------------------------------------------
REDIR = "https://vertexaisearch.cloud.google.com/grounding-api-redirect/AUZ123"
calls = []


def fake_get(url, **kw):
    calls.append((url, kw))
    return SimpleNamespace(status_code=302, headers={"Location": "https://www.bbb.org/us/nc/charlotte/profile/x"})


requests.get = fake_get
u, kind = gp.resolve_redirect(REDIR)
check("a redirect link resolves to the real URL, kind direct", (u, kind) == ("https://www.bbb.org/us/nc/charlotte/profile/x", "direct"))
check("only the Location header is read: one request, redirects not followed, nothing else fetched",
      len(calls) == 1 and calls[0][0] == REDIR and calls[0][1].get("allow_redirects") is False)
calls.clear()
check("a normal URL is not touched (no request)", gp.resolve_redirect("https://bbb.org/x") == ("https://bbb.org/x", "direct") and not calls)
requests.get = lambda url, **kw: SimpleNamespace(status_code=200, headers={})
check("no Location header: the redirect link is kept, kind redirect", gp.resolve_redirect(REDIR) == (REDIR, "redirect"))
requests.get = lambda url, **kw: SimpleNamespace(status_code=302, headers={"Location": "javascript:alert(1)"})
check("a non-http Location is ignored", gp.resolve_redirect(REDIR) == (REDIR, "redirect"))


def raises(url, **kw):
    raise requests.exceptions.ConnectionError("down")


requests.get = raises
check("a failed resolution keeps the link and never raises", gp.resolve_redirect(REDIR) == (REDIR, "redirect"))
requests.get = boom

# --- Stage E0: the opt-in safeguard -------------------------------------------------------------------------------
check("allow switch is off by default", gp.is_allowed() is False)
posts = []
E0_LEAD = {k: "x" for k in ("business_name", "address", "city", "state", "phone", "website")}
_real_post = requests.post
requests.post = lambda *a, **k: posts.append(1) or boom()
try:
    resp, err, retryable = gp.call_once("p", "m", "k", 5)
    check("call_once without --gemini: error, not retryable, and requests.post was never reached",
          resp is None and "off" in err and retryable is False and posts == [])
    out = gp.ask_gemini(E0_LEAD, "k")
    check("ask_gemini without --gemini and no injected call: status off, zero attempts, no network",
          out["status"] == "off" and out["attempts"] == 0 and posts == [])
    out_inj = gp.ask_gemini(E0_LEAD, "k", call=lambda *a: (None, "HTTP 503: x", False))
    check("an injected call (tests) still runs when the switch is off", out_inj["status"] == "error" and "503" in out_inj["error"])
    gp.allow_gemini(True)
    try:
        gp.call_once("p", "m", "k", 5)
    except Exception:
        pass
    check("with the switch on, call_once does reach requests.post (the test double blocks it)", posts == [1])
finally:
    gp.allow_gemini(False)
    requests.post = _real_post
requests.post = boom
check("confirm_opt_in: 'n' keeps calls off, 'y' turns them on, the prompt carries the cost note",
      (not gp.confirm_opt_in(3, input_fn=lambda p: "n")) and not gp.is_allowed()
      and gp.confirm_opt_in(3, input_fn=lambda p: "y") and gp.is_allowed() and "cause unconfirmed" in gp.COST_NOTE)
gp.allow_gemini(False)
check("reconcile: status off -> case gemini_off, the Brave answer kept", rc.reconcile(
    {"first": "Ann", "last": "Lee", "title": "Owner", "confidence": "high", "evidence": {}}, {"status": "off"})["case"] == "gemini_off")
check("reconcile: status error/disabled keep the old case gemini_unavailable", all(rc.reconcile(None, {"status": s})["case"] == "gemini_unavailable"
                                                                                    for s in ("error", "disabled")))

# --- ask_gemini: time cap, one retry, error fallback ---------------------------------------------
LEAD = {"business_name": "Zephyr Cuts", "address": "1 Main St", "city": "Charlotte", "state": "NC", "phone": "", "website": "https://zephyr.example"}
ANSWER = {"candidates": [{"content": {"parts": [{"text": 'Jane Rivera owns it.\n{"owner_first_name": "Jane", "owner_last_name": "Rivera", '
                                                        '"goes_by_name": "JR", "title": "Owner", "confidence": "high", "source_urls": []}'}]},
                          "groundingMetadata": {"webSearchQueries": ["q1", "q2"],
                                                "groundingChunks": [{"web": {"uri": REDIR, "title": "bbb.org"}},
                                                                    {"web": {"uri": REDIR + "2", "title": "yelp.com"}}],
                                                "groundingSupports": [{"segment": {"text": "Jane Rivera owns it."}, "groundingChunkIndices": [0]}]}}],
          "usageMetadata": {"promptTokenCount": 1000, "thoughtsTokenCount": 2000, "candidatesTokenCount": 100}}
EMPTY = {"usageMetadata": {"promptTokenCount": 1000, "thoughtsTokenCount": 3000}}


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


class Script:
    """Fake call_once: pops (resp, err, retryable, seconds_it_takes); records the timeout it was given."""
    def __init__(self, clock, *steps):
        self.clock, self.steps, self.timeouts = clock, list(steps), []

    def __call__(self, prompt, model, key, timeout):
        self.timeouts.append(round(timeout, 1))
        resp, err, retryable, took = self.steps.pop(0)
        self.clock.t += took
        return resp, err, retryable


requests.get = lambda url, **kw: SimpleNamespace(status_code=302, headers={"Location": "https://www.bbb.org/us/nc/charlotte/profile/z"})
c = Clock()
s = Script(c, (ANSWER, "", False, 40))
out = gp.ask_gemini(LEAD, "k", call=s, clock=c)
check("success on the first attempt: one call, evidence resolved to the real URL, goes-by kept",
      out["status"] == "ok" and out["attempts"] == 1 and out["goes_by"] == "JR" and out["evidence"]["evidence_url"].startswith("https://www.bbb.org")
      and out["evidence"]["evidence_url_kind"] == "direct" and out["evidence"]["evidence_source"] == "gemini_grounding"
      and out["support"] == "segment" and out["searches"] == 2 and out["cost_usd"] > 0)
c = Clock()
check("caps are 180s per attempt and 240s in total", (gp.PER_ATTEMPT_S, gp.TOTAL_S) == (180, 240))
s = Script(c, (None, "timeout after 180s", True, 180), (ANSWER, "", False, 30))
out = gp.ask_gemini(LEAD, "k", call=s, clock=c)
check("timeout then answer: exactly 2 attempts, the first gets 180s, the second is capped by the time left (60s)",
      out["status"] == "ok" and out["attempts"] == 2 and s.timeouts == [180.0, 60.0])
c = Clock()
s = Script(c, (None, "timeout after 180s", True, 180), (None, "timeout after 60s", True, 60), (ANSWER, "", False, 1))
out = gp.ask_gemini(LEAD, "k", call=s, clock=c)
check("timeout twice: status error, only 2 attempts, total never above 240s",
      out["status"] == "error" and out["attempts"] == 2 and len(s.timeouts) == 2 and c.t <= 240 and "2 attempt" in out["error"])
c = Clock()
s = Script(c, (None, "timeout after 180s", True, 235), (ANSWER, "", False, 1))
out = gp.ask_gemini(LEAD, "k", call=s, clock=c)
check("a retry is not started when under 10s of the 240s budget is left", out["status"] == "error" and out["attempts"] == 1)
c = Clock()
s = Script(c, (ANSWER, "", False, 150))
out = gp.ask_gemini(LEAD, "k", call=s, clock=c)
check("a call that takes 150s (the old cap cut these) now completes", out["status"] == "ok" and s.timeouts == [180.0])
c = Clock()
s = Script(c, (EMPTY, "", False, 30), (ANSWER, "", False, 30))
out = gp.ask_gemini(LEAD, "k", call=s, clock=c)
check("empty response then answer: retried once, ok, and the empty reply's tokens still count toward cost",
      out["status"] == "ok" and out["attempts"] == 2 and out["cost_usd"] > gp.estimate_cost(gp.MODEL, ANSWER["usageMetadata"], 2))
c = Clock()
s = Script(c, (EMPTY, "", False, 30), (EMPTY, "", False, 30), (ANSWER, "", False, 1))
out = gp.ask_gemini(LEAD, "k", call=s, clock=c)
check("empty twice: status error (not a blank), never a third call", out["status"] == "error" and out["attempts"] == 2
      and "empty response" in out["error"] and out["first"] is None)
c = Clock()
s = Script(c, (None, "HTTP 429: quota", True, 1), (ANSWER, "", False, 5))
check("HTTP 429 is retried once", gp.ask_gemini(LEAD, "k", call=s, clock=c)["status"] == "ok")
c = Clock()
s = Script(c, (None, "HTTP 400: bad request", False, 1), (ANSWER, "", False, 5))
out = gp.ask_gemini(LEAD, "k", call=s, clock=c)
check("a non-retryable HTTP error (400/401/403) is not retried", out["status"] == "error" and out["attempts"] == 1 and len(s.timeouts) == 1)
check("no API key: error without any call", gp.ask_gemini(LEAD, "", call=boom)["status"] == "error")

# --- HTTP 402 (credits depleted): Gemini is disabled for the rest of the batch, loudly, once ----------------
import contextlib, io                                    # noqa: E402
gp.reset()
c = Clock()
s = Script(c, (None, "HTTP 402: Your prepayment credits are depleted.", False, 2), (ANSWER, "", False, 5))
err_buf = io.StringIO()
with contextlib.redirect_stderr(err_buf):
    out = gp.ask_gemini(LEAD, "k", call=s, clock=c)
check("402: the row is an error, not retried (1 attempt), and Gemini is now disabled", out["status"] == "error"
      and out["attempts"] == 1 and len(s.timeouts) == 1 and gp.disabled_reason() and "402" in gp.disabled_reason())
check("402: a loud warning is printed", "GEMINI DISABLED" in err_buf.getvalue() and "Brave answer only" in err_buf.getvalue())
out = gp.ask_gemini(LEAD, "k", call=boom, clock=c)
check("after a 402 the next rows make NO call and come back status 'disabled'", out["status"] == "disabled" and out["attempts"] == 0
      and "disabled" in out["error"])
err_buf = io.StringIO()
with contextlib.redirect_stderr(err_buf):
    gp._disable("HTTP 402: again")
check("a second 402 does not print the warning again", err_buf.getvalue() == "" and "again" not in gp.disabled_reason())
rec = rc.reconcile(N("Jane", "Rivera"), out)
check("a disabled Gemini leaves the Brave answer untouched (fallback)", rec["keep_brave"] and rec["case"] == "gemini_unavailable")
gp.reset()
c = Clock()
check("reset() at the start of the next batch turns Gemini back on", gp.ask_gemini(LEAD, "k", call=Script(c, (ANSWER, "", False, 5)), clock=c)["status"] == "ok")
for code in ("HTTP 429: quota", "HTTP 503: down"):
    c = Clock()
    gp.ask_gemini(LEAD, "k", call=Script(c, (None, code, True, 1), (None, code, True, 1)), clock=c)
check("only a 402 disables Gemini (429/5xx failures do not)", gp.disabled_reason() is None)
threads_err = io.StringIO()
with contextlib.redirect_stderr(threads_err):
    ts = [threading.Thread(target=gp._disable, args=("HTTP 402: x",)) for _ in range(8)]
    [x.start() for x in ts]
    [x.join() for x in ts]
check("eight workers hitting a 402 together print the warning exactly once", threads_err.getvalue().count("GEMINI DISABLED") == 1)
gp.reset()
c = Clock()
bad = {"candidates": [{"content": {"parts": [{"text": "I could not find an owner."}]}}]}
out = gp.ask_gemini(LEAD, "k", call=Script(c, (bad, "", False, 5)), clock=c)
check("an answer with no JSON is an error (falls back to Brave), not a blank", out["status"] == "error" and "JSON" in out["error"])
c = Clock()
nochunk = json.loads(json.dumps(ANSWER))
nochunk["candidates"][0]["groundingMetadata"] = {"webSearchQueries": ["q"]}
out = gp.ask_gemini(LEAD, "k", call=Script(c, (nochunk, "", False, 5)), clock=c)
check("a name with no grounding chunk at all has no evidence (support 'none')", out["status"] == "ok" and out["support"] == "none"
      and out["evidence"]["evidence_url"] is None)
c = Clock()
nosegment = json.loads(json.dumps(ANSWER))
nosegment["candidates"][0]["groundingMetadata"]["groundingSupports"] = []
out = gp.ask_gemini(LEAD, "k", call=Script(c, (nosegment, "", False, 5)), clock=c)
check("no grounded segment names her but chunks exist: evidence from the response's chunks, support 'response'",
      out["support"] == "response" and out["evidence"]["evidence_url"])
check("the key is sent in a header, never in a URL", "key" not in gp.ENDPOINT)

# --- The graph, offline: the join, the fallback, concurrency ---------------------------------------
sys.stdout.flush()
import first_graph as fg                                # noqa: E402
logging.disable(logging.CRITICAL)
# Gemini is OFF by default (stage E0). These graph tests exercise the two-path join with a FAKE ask_gemini, so they
# turn the node on; gemini_path's own network switch (allow_gemini) stays off and no real call can happen.
check("Gemini defaults: the graph node is off and the network switch is off", fg.GEMINI_ENABLED is False and not gp.is_allowed())
fg.GEMINI_ENABLED = True


def owner(first, last, title="Owner", conf="high", snippet="", url="", **kw):
    f = dict(linkedin="Unknown", instagram="Unknown", tiktok="Unknown", facebook="Unknown", why_this_person="because")
    f.update(kw)
    return fg.OwnerResult(decision_maker_first_name=first, decision_maker_last_name=last, title=title,
                          confidence=conf, evidence_snippet=snippet, evidence_url=url, **f)


class Haiku:
    def __init__(self, by_name):
        self.by_name = by_name

    def invoke(self, prompt):
        name = prompt.split("Business: ")[1].split("\n")[0]
        parsed = self.by_name.get(name) or owner("Unknown", "Unknown", "Unknown", "low")
        return {"raw": SimpleNamespace(usage_metadata={"input_tokens": 1, "output_tokens": 1}), "parsed": parsed}


class Sonnet:
    def invoke(self, prompt):
        if "Haiku's extraction: Pablo" in prompt:          # verifies a first-name-only answer, as the real Sonnet may
            url = [l[4:].strip() for l in prompt.splitlines() if l.startswith("URL:")][0]
            return SimpleNamespace(content=json.dumps({"verified_confidence": "medium", "verified_first_name": "Pablo",
                                   "verified_last_name": "Unknown", "verified_evidence_snippet": "owner Pablo",
                                   "verified_evidence_url": url, "alternative_first_name": "", "alternative_last_name": "",
                                   "verification_reasoning": "ok"}), usage_metadata={"input_tokens": 1, "output_tokens": 1})
        return SimpleNamespace(content='{"verified_confidence":"low","verified_first_name":"Unknown","verified_last_name":"Unknown",'
                                       '"verified_evidence_snippet":"","verified_evidence_url":"","alternative_first_name":"",'
                                       '"alternative_last_name":"","verification_reasoning":"none"}',
                               usage_metadata={"input_tokens": 1, "output_tokens": 1})


def hit(name, owner_name):
    url = f"https://www.bbb.org/us/nc/charlotte/profile/barber/{name.lower().replace(' ', '-')}-1"
    return [{"url": url, "title": f"{name} | BBB", "description": f"{name} owner {owner_name}, Charlotte NC."}], url


def make_brave(results_by_name):
    def fake_get(url, **kw):
        if "api.search.brave.com" in url:
            name = kw["params"]["q"].split(" Charlotte")[0]
            return SimpleNamespace(status_code=200, headers={}, raise_for_status=lambda: None,
                                   json=lambda: {"web": {"results": results_by_name.get(name, [])}, "locations": {"results": []}})
        return SimpleNamespace(status_code=404, text="", headers={})
    return fake_get


def lead_state(name):
    return {"business_name": name, "lead_id": 1, "website": "", "address": "1 Main St, Charlotte, NC 28202, USA",
            "phone": "", "city": "Charlotte", "state": "NC"}


def gem_result(first, last, url="https://www.bbb.org/real", **k):
    d = {"status": "ok", "error": None, "first": first, "last": last, "goes_by": k.get("goes_by"), "title": "Owner",
         "confidence": "high", "support": "segment", "searches": 1, "cost_usd": 0.01, "seconds": 5.0, "attempts": 1,
         "evidence": ev.evidence_fields(url, "gemini_grounding", domain="bbb.org") if url else ev.empty_evidence()}
    d.update({k2: v for k2, v in k.items() if k2 != "goes_by"})
    return d


def run(names, gem_by_name, haiku_by_name, delay=None):
    results = {n: hit(n, haiku_by_name[n][0]) if n in haiku_by_name else ([], None) for n in names}
    parsed = {}
    for n, (who, conf) in {k: v for k, v in haiku_by_name.items() if k in names}.items():
        f, l = who.split(" ") if " " in who else (who, "Unknown")
        parsed[n] = owner(f, l, conf=conf, snippet=f"owner {who}", url=results[n][1].lower())

    def fake_gemini(lead, key, **kw):
        if delay:
            time.sleep(delay.get(lead["business_name"], 0))
        return gem_by_name[lead["business_name"]]

    saved = (fg.structured_model, fg.reasoning_model, requests.get, fg.brave_key, fg.gemini_path.ask_gemini)
    fg.structured_model, fg.reasoning_model, fg.brave_key = Haiku(parsed), Sonnet(), "k"
    requests.get = make_brave({n: v[0] for n, v in results.items()})
    fg.gemini_path.ask_gemini = fake_gemini
    try:
        return {n: fg.app.invoke(lead_state(n)) for n in names}
    finally:
        fg.structured_model, fg.reasoning_model, requests.get, fg.brave_key, fg.gemini_path.ask_gemini = saved


names = ["Alpha Cuts", "Bravo Cuts", "Charlie Cuts", "Delta Cuts", "Echo Cuts", "Foxtrot Cuts"]
haiku = {"Alpha Cuts": ("Jane Rivera", "high"), "Bravo Cuts": ("Greg Furr", "high"), "Charlie Cuts": ("Ann Lee", "high"),
         "Echo Cuts": ("Pablo", "medium")}
gem = {"Alpha Cuts": gem_result("Jane", "Rivera"), "Bravo Cuts": gem_result("Hunter", "Griffin"),
       "Charlie Cuts": {"status": "error", "error": "timeout after 100s; 2 attempt(s), 150s", "cost_usd": 0.0, "seconds": 150.0},
       "Delta Cuts": gem_result("Clarence", "Moore"), "Echo Cuts": gem_result("Pablo", None), "Foxtrot Cuts": gem_result(None, None)}
res = run(names, gem, haiku)
check("graph: both agree -> delivered high with Brave's URL, reconcile_case agree",
      (res["Alpha Cuts"]["decision_maker_first_name"], res["Alpha Cuts"]["delivered_confidence"], res["Alpha Cuts"]["reconcile_case"])
      == ("Jane", "high", "agree") and res["Alpha Cuts"]["evidence_source"] == "brave_search")
check("graph: different people -> blank with review_candidates holding both, status ok (not an error)",
      res["Bravo Cuts"]["decision_maker_first_name"] is None and res["Bravo Cuts"]["reconcile_case"] == "conflict"
      and len(json.loads(res["Bravo Cuts"]["review_candidates"])) == 2 and res["Bravo Cuts"]["status"] == "ok"
      and res["Bravo Cuts"]["evidence_url"] is None)
check("graph: Gemini error -> the Brave answer exactly as the Brave path delivered it (high, its evidence), case gemini_unavailable",
      (res["Charlie Cuts"]["decision_maker_first_name"], res["Charlie Cuts"]["delivered_confidence"], res["Charlie Cuts"]["reconcile_case"])
      == ("Ann", "high", "gemini_unavailable") and res["Charlie Cuts"]["evidence_url"] and res["Charlie Cuts"]["status"] == "ok")
check("graph: Gemini alone -> delivered at medium with Gemini's evidence",
      (res["Delta Cuts"]["decision_maker_first_name"], res["Delta Cuts"]["delivered_confidence"]) == ("Clarence", "medium")
      and res["Delta Cuts"]["evidence_source"] == "gemini_grounding" and res["Delta Cuts"]["evidence_url"])
check("graph: first-name-only on both paths -> delivered at medium", res["Echo Cuts"]["decision_maker_first_name"] == "Pablo"
      and res["Echo Cuts"]["delivered_confidence"] == "medium")
check("graph: both blank -> blank, status ok", res["Foxtrot Cuts"]["decision_maker_first_name"] is None and res["Foxtrot Cuts"]["status"] == "ok")
check("graph: every path keeps each answer (brave_answer snapshot and the gemini result)",
      res["Bravo Cuts"]["brave_answer"]["first"] == "Greg" and res["Bravo Cuts"]["gemini"]["first"] == "Hunter")
named = [r for r in res.values() if r["decision_maker_first_name"]]
check("graph: every delivered name has an evidence URL", all(r["evidence_url"] for r in named))

# reconcile waits for the slow branch, in both orders, and runs exactly once per lead
counts = []
real_node = fg.reconcile_node
orig_reconcile = fg.reconcile.reconcile


def counting(brave, gemini):
    counts.append((brave or {}).get("first"))
    return orig_reconcile(brave, gemini)


fg.reconcile.reconcile = counting
try:
    slow_gemini = run(["Alpha Cuts"], gem, haiku, delay={"Alpha Cuts": 0.6})["Alpha Cuts"]
    n_slow = len(counts)
    counts.clear()
    fast_gemini = run(["Alpha Cuts"], gem, haiku, delay={"Alpha Cuts": 0.0})["Alpha Cuts"]
    n_fast = len(counts)
finally:
    fg.reconcile.reconcile = orig_reconcile
check("join: reconcile runs exactly once per lead whether Gemini finishes after or before Brave", (n_slow, n_fast) == (1, 1))
check("join: it saw BOTH paths in either order (agree, high)",
      slow_gemini["reconcile_case"] == fast_gemini["reconcile_case"] == "agree" and slow_gemini["delivered_confidence"] == "high")

# concurrency: leads in a pool never see each other's answers
saved_inv = (fg.structured_model, fg.reasoning_model, requests.get, fg.brave_key, fg.gemini_path.ask_gemini)
many = [f"Lead{i} Cuts" for i in range(8)]
many_hits = {n: hit(n, f"Owner{i} Person") for i, n in enumerate(many)}
parsed = {n: owner(f"Owner{i}", "Person", snippet=f"owner Owner{i} Person", url=many_hits[n][1].lower()) for i, n in enumerate(many)}
fg.structured_model, fg.reasoning_model, fg.brave_key = Haiku(parsed), Sonnet(), "k"
requests.get = make_brave({n: v[0] for n, v in many_hits.items()})
def gem_for(lead, key, **kw):
    time.sleep(0.05)
    return gem_result(f"Owner{lead['business_name'][4]}", "Person")      # 'Lead3 Cuts' -> Owner3 Person


fg.gemini_path.ask_gemini = gem_for
try:
    leads = [SimpleNamespace(id=i, business_name=n, website="", address="1 Main St, Charlotte, NC 28202, USA", phone="",
                             city="Charlotte", state="NC") for i, n in enumerate(many)]
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=4) as pool:
        outcomes = list(pool.map(fg._invoke_lead, leads))
finally:
    fg.structured_model, fg.reasoning_model, requests.get, fg.brave_key, fg.gemini_path.ask_gemini = saved_inv
check("concurrency: 8 leads on 4 workers, each result belongs to its own lead, no errors",
      all(err is None for _, err in outcomes)
      and all(r["decision_maker_first_name"] == f"Owner{i}" and r["reconcile_case"] == "agree" for i, (r, _) in enumerate(outcomes)))
ok, bad_, rate = fg.gemini_error_rate([r for r, _ in outcomes] + [{"gemini": {"status": "error"}}, {"gemini": {"status": "disabled"}}, None])
check("Gemini error rate counts ok and error only (disabled and missing results are left out)", (ok, bad_) == (8, 1) and abs(rate - 1 / 9) < 1e-9)

# Brave: one retry on a 429 (it used to become silently empty results)
sleeps = []
real_sleep = time.sleep
time.sleep = lambda s: sleeps.append(s)
seq = [SimpleNamespace(status_code=429, headers={"Retry-After": "2"}, raise_for_status=lambda: (_ for _ in ()).throw(requests.exceptions.HTTPError("429"))),
       SimpleNamespace(status_code=200, headers={}, raise_for_status=lambda: None, json=lambda: {"web": {"results": [{"url": "u"}]}, "locations": {"results": []}})]
requests.get = lambda url, **kw: seq.pop(0)
try:
    cost = fg.LeadCost(lead_id=1, business_name="x")
    out = fg._brave_search({"queries": ["q"], "cost": cost}, 0)
finally:
    requests.get, time.sleep = boom, real_sleep
check("Brave 429: waits the stated time once and retries; the results come back", out["search_results"][0]["results"] == [{"url": "u"}]
      and sleeps == [2.0] and not cost.brave_errors)
check("Brave counters (recording only): one 429 seen, one recovered, no search errored, two calls counted",
      (cost.brave_429, cost.brave_429_recovered, len(cost.brave_errors), cost.brave_calls) == (1, 1, 0, 1 + 1))

def _r429():
    return SimpleNamespace(status_code=429, headers={"Retry-After": "1"},
                           raise_for_status=lambda: (_ for _ in ()).throw(requests.exceptions.HTTPError("429")))

time.sleep = lambda s: None
seq = [_r429(), _r429()]
requests.get = lambda url, **kw: seq.pop(0)
try:
    cost2 = fg.LeadCost(lead_id=2, business_name="y")
    out2 = fg._brave_search({"queries": ["q"], "cost": cost2}, 0)
finally:
    requests.get, time.sleep = boom, real_sleep
check("Brave counters: a 429 that is still a 429 on the retry is counted as seen, not recovered, and the search errored",
      (cost2.brave_429, cost2.brave_429_recovered, len(cost2.brave_errors)) == (1, 0, 1) and out2["search_results"][0]["results"] == [])
cost3 = fg.LeadCost(lead_id=3, business_name="z")
requests.get = lambda url, **kw: SimpleNamespace(status_code=200, headers={}, raise_for_status=lambda: None,
                                                 json=lambda: {"web": {"results": []}, "locations": {"results": []}})
try:
    fg._brave_search({"queries": ["q"], "cost": cost3}, 0)
finally:
    requests.get = boom
check("Brave counters stay at zero when nothing goes wrong", (cost3.brave_429, cost3.brave_429_recovered, len(cost3.brave_errors)) == (0, 0, 0))

import c2_report                                          # noqa: E402
# --- The Gemini error-rate rule and the error classes in the report -----------------------------------------
E = lambda err, case="gemini_unavailable", grade="blank", secs=240: {"business_name": "x", "gemini_status": "error", "gemini_error": err,
                                                                     "reconcile_case": case, "grade": grade, "gemini_seconds": secs,
                                                                     "first": "", "last": ""}
cls = c2_report.error_classes([E("timeout after 180s; 2 attempt(s), 240s"), E("HTTP 402: depleted"), E("HTTP 503: down"),
                               E("empty response (finish=NO_CANDIDATES, block=-)"), E("answer had no parseable JSON")])
check("error classes: timeout, 402, 5xx, other 4xx, empty response, unparseable", sorted(cls) == sorted(
    ["timeout", "HTTP 402 credits depleted", "HTTP 5xx", "empty response", "unparseable answer"])
      and "other HTTP error" in c2_report.error_classes([E("HTTP 400: bad")]))
tos = [E("timeout after 180s; 2 attempt(s), 240s") for _ in range(3)]
check("rule: <=15% and every error vendor-side with the Brave fallback delivered -> accepted", c2_report.gemini_rule_ok(tos, [], 0.12))
check("rule: 5xx and empty responses are vendor-side -> accepted",
      c2_report.gemini_rule_ok([E("HTTP 503: down"), E("empty response (x)")], [], 0.05))
check("rule: other 4xx -> investigate", not c2_report.gemini_rule_ok([E("HTTP 400: bad")], [], 0.02))
check("rule: 5xx above 5% of a holdout run -> investigate; same rate on a dev run is accepted",
      not c2_report.gemini_rule_ok([E("HTTP 503: down")] * 3, [], 0.07, 43, holdout=True)
      and c2_report.gemini_rule_ok([E("HTTP 503: down")] * 3, [], 0.07, 43))
check("rule: over 15% -> investigate", not c2_report.gemini_rule_ok(tos, [], 0.21))
check("rule: an unparseable answer -> investigate", not c2_report.gemini_rule_ok(tos + [E("answer had no parseable JSON")], [], 0.05))
check("rule: a timeout whose fallback did not deliver (case not gemini_unavailable) -> investigate",
      not c2_report.gemini_rule_ok([E("timeout after 180s", case="conflict")], [], 0.05))
check("rule: any DISABLED row (a 402 happened) -> investigate", not c2_report.gemini_rule_ok([], [{"x": 1}], 0.0))
check("rule: zero errors -> accepted", c2_report.gemini_rule_ok([], [], 0.0))

# --- Export: the needs_review outcome ------------------------------------------------------------------
import sqlite3, tempfile                                  # noqa: E402
import export                                            # noqa: E402
import setup_database as sdb                             # noqa: E402

with tempfile.TemporaryDirectory() as d:
    db = os.path.join(d, "t.db")
    real_path, sdb.DB_PATH = sdb.DB_PATH, db
    try:
        sdb.init_database(reset=False)
    finally:
        sdb.DB_PATH = real_path
    conn = sqlite3.connect(db)
    for pid, nm in (("p1", "Conflict Shop"), ("p2", "Agree Shop"), ("p3", "Blank Shop")):
        conn.execute("INSERT INTO leads (place_id, business_name) VALUES (?, ?)", (pid, nm))
    cands = json.dumps([{"source": "brave", "first": "Niki", "last": "Voncannon", "evidence_url": "https://a.example"},
                        {"source": "gemini", "first": "Victoria", "last": "Voncannon", "evidence_url": "https://b.example"}])
    conn.execute("INSERT INTO results (lead_id, status, reconcile_case, review_candidates, confidence) VALUES (1,'ok','conflict',?, 'low')", (cands,))
    conn.execute("INSERT INTO results (lead_id, decision_maker_first_name, decision_maker_last_name, delivered_confidence, status, "
                 "evidence_url, reconcile_case, gemini_status) VALUES (2,'Jane','Rivera','high','ok','https://bbb.org/x','agree','ok')")
    conn.execute("INSERT INTO results (lead_id, status, reconcile_case) VALUES (3,'ok','both_blank')")
    conn.commit()
    conn.close()
    rows = {r["business_name"]: r for r in export.build_rows(db)}
    check("export: a conflict is outcome needs_review with both candidates and their sources, no name",
          rows["Conflict Shop"]["outcome"] == "needs_review" and rows["Conflict Shop"]["owner_first_name"] is None
          and len(json.loads(rows["Conflict Shop"]["review_candidates"])) == 2 and "b.example" in rows["Conflict Shop"]["review_candidates"])
    check("export: agree is found with evidence; a both-blank row stays no_owner_found, not needs_review",
          rows["Agree Shop"]["outcome"] == "found" and rows["Blank Shop"]["outcome"] == "no_owner_found"
          and rows["Agree Shop"]["reconcile_case"] == "agree")
    order = [r["outcome"] for r in export.build_rows(db)]
    check("export order: found, needs_review, no_owner_found", order == ["found", "needs_review", "no_owner_found"])
    check("export columns include review_candidates and reconcile_case", {"review_candidates", "reconcile_case"} <= set(export.COLUMNS))

# --- Database write of real graph results, then export (end to end, temp database) ------------------------------
with tempfile.TemporaryDirectory() as d:
    db = os.path.join(d, "e2e.db")
    real_path, sdb.DB_PATH = sdb.DB_PATH, db
    try:
        sdb.init_database(reset=False)
    finally:
        sdb.DB_PATH = real_path
    conn = sqlite3.connect(db)
    for i, nm in enumerate(["Alpha Cuts", "Bravo Cuts", "Charlie Cuts", "Foxtrot Cuts"], 1):
        conn.execute("INSERT INTO leads (place_id, business_name) VALUES (?, ?)", (f"p{i}", nm))
    conn.commit()
    conn.close()
    ldb = fg.LeadDatabase(db)
    for i, nm in enumerate(["Alpha Cuts", "Bravo Cuts", "Charlie Cuts", "Foxtrot Cuts"], 1):
        ldb.write_result(i, res[nm])
    out = {r["business_name"]: r for r in export.build_rows(db)}
    check("end to end: agree is found with evidence and the reconcile case stored",
          out["Alpha Cuts"]["outcome"] == "found" and out["Alpha Cuts"]["evidence_url"] and out["Alpha Cuts"]["reconcile_case"] == "agree")
    check("end to end: the conflict row is needs_review with both candidates and sources",
          out["Bravo Cuts"]["outcome"] == "needs_review" and len(json.loads(out["Bravo Cuts"]["review_candidates"])) == 2)
    check("end to end: a Gemini error with a Brave answer is found (the fallback), not an error",
          out["Charlie Cuts"]["outcome"] == "found" and out["Charlie Cuts"]["owner_first_name"] == "Ann")
    check("end to end: both blank is no_owner_found", out["Foxtrot Cuts"]["outcome"] == "no_owner_found")
    conn = sqlite3.connect(db)
    row = conn.execute("SELECT gemini_status, gemini_error, brave_first_name, gemini_first_name, gemini_seconds FROM results WHERE lead_id=3").fetchone()
    conn.close()
    check("end to end: the per-lead Gemini status, error and each path's own name are stored",
          row[0] == "error" and "timeout" in row[1] and row[2] == "Ann" and row[3] is None and row[4] == 150.0)

# --- Harness report: three views from one run, gate, differences ------------------------------------------
import io                                                 # noqa: E402
import contextlib                                         # noqa: E402
import c2_report                                          # noqa: E402

GT_BY_ID = {1: {"kind": "owner", "owners": [{"first": "Jane", "last": "Rivera", "aliases": []}]},
            2: {"kind": "owner", "owners": [{"first": "Greg", "last": "Furr", "aliases": []}]},
            3: {"kind": "owner", "owners": [{"first": "Ann", "last": "Lee", "aliases": []}, {"first": "Bo", "last": "Lee", "aliases": []}]},
            4: {"kind": "no_owner", "owners": []}}
base = {"subset": "audit", "status": "ok", "evidence_url": "https://x", "confidence": "high", "delivered_confidence": "high", "seconds": 1}
recs = [
    dict(base, id=1, business_name="A", truth="Jane Rivera", first="Jane", last="Rivera", grade="full", brave_grade="full", gemini_grade="full",
         gemini_status="ok", reconcile_case="agree", review_candidates=""),
    dict(base, id=2, business_name="B", truth="Greg Furr", first="", last="", grade="blank", brave_grade="wrong", gemini_grade="full", evidence_url="",
         gemini_status="ok", reconcile_case="conflict",
         review_candidates=json.dumps([{"first": "Hunter", "last": "Griffin"}, {"first": "Greg", "last": "Furr"}])),
    dict(base, id=3, business_name="C", truth="Ann Lee / Bo Lee", first="Ann", last="Lee", grade="full", brave_grade="full", gemini_grade="error",
         gemini_status="error", gemini_error="timeout", reconcile_case="gemini_unavailable", review_candidates=""),
    dict(base, id=4, business_name="D", truth="(no owner public)", first="", last="", grade="correct_blank", brave_grade="correct_blank",
         gemini_grade="correct_blank", gemini_status="ok", reconcile_case="both_blank", review_candidates="", evidence_url=""),
]
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    c2_report.report(recs, GT_BY_ID)
text = buf.getvalue()
check("report prints Brave alone, Gemini alone and Combined", all(x in text for x in ("Brave alone", "Gemini alone", "Combined (delivered)")))
check("reachable counts a correct name sitting among the needs_review candidates (2 delivered + 1 reachable = 3/3 owner rows)",
      c2_report.reachable(recs, GT_BY_ID) == (3, 3) and "lenient 3/3 strict 3/3" in text)
check("the Gemini error row is listed apart and the error rate is printed (counted over rows where Gemini ran)",
      "1 Gemini error row" in text and "Gemini error rate this run: 1/4 = 25%" in text)
check("the gate lines are printed with PASS/FAIL", "PASS  delivered wrong names <= 3" in text and "named rows without an evidence URL" in text)
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    c2_report.diff_vs(recs, [dict(r, grade="blank", first="", last="") for r in recs], "ref")
check("diff_vs lists only rows whose answer/grade/evidence differ from the reference", "Rows that differ from ref (3)" in buf.getvalue())
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    c2_report.report(recs[:3] + [dict(recs[3], gemini_status="disabled", gemini_grade="disabled")], GT_BY_ID)
check("the report calls out rows where Gemini was disabled by a 402", "Gemini was DISABLED mid-run" in buf.getvalue() and "1 row(s)" in buf.getvalue())

# --- Stage E0 / D3: a run without --gemini is recorded as off and reported as such ------------------------------
off_recs = [dict(r, gemini_status="off", gemini_grade="off", reconcile_case="gemini_off", gemini_attempts=0, review_candidates="")
            for r in recs]
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    c2_report.report(off_recs, GT_BY_ID)
t_off = buf.getvalue()
check("Gemini-off report: says 'Gemini off by choice, C2 gate not applicable', no 402 banner, no FAIL line, no Gemini error rate",
      "Gemini off by choice, C2 gate not applicable" in t_off and "DISABLED" not in t_off and "FAIL" not in t_off
      and "error rate this run" not in t_off)
check("Gemini-off report: the Brave-only checks still print, including 0 Gemini requests", "PASS  Gemini requests made: 0" in t_off)
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    c2_report.report([dict(off_recs[0], gemini_attempts=2)] + off_recs[1:], GT_BY_ID)
check("Gemini-off report fails the 0-request check if Gemini was actually called", "FAIL  Gemini requests made: 2" in buf.getvalue())
check("old labels still count: a 'disabled' row is excluded from Gemini alone, an 'off' row too",
      c2_report.alone_grades([dict(off_recs[0], gemini_grade="off"), dict(recs[0], gemini_grade="disabled")], "gemini") == [])

# --- Option 5 offline simulation = the production reconcile on the saved result CSVs ----------------------------
sim_files = [("eval/data/results_20261007_082954.csv", "eval/data/results_gemini_20261005_215033_raw.csv", (19, 19, 2, 3, 25, 24)),
             ("eval/data/results_20261007_085218.csv", "eval/data/results_gemini_20261006_122645_raw.csv", (24, 24, 0, 3, 30, 30))]
if all(os.path.exists(os.path.join(ROOT, b)) and os.path.exists(os.path.join(ROOT, g)) for b, g, _ in sim_files):
    from run_regression import rescore, excluded_rows       # noqa: E402
    from scoring import judge, summarize, is_blank           # noqa: E402
    gt = json.load(open(os.path.join(ROOT, "eval/data/ground_truth.json"), encoding="utf-8"))
    ex = excluded_rows(gt)
    gtid = {r["id"]: r for r in gt}
    for bpath, gpath, expected in sim_files:
        B = {int(r["id"]): r for r in rescore(os.path.join(ROOT, bpath), gt, ex) if r["grade"] != "excluded"}
        Gm = {int(r["id"]): r for r in rescore(os.path.join(ROOT, gpath), gt, ex) if r["grade"] != "excluded"}
        grades, reach_l, reach_s = [], 0, 0

        def side(r, evid):
            if r is None or r["grade"] == "error" or is_blank(r["first"], r["last"]):
                return None
            f = "" if is_blank(r["first"], "") else r["first"]
            l = "" if is_blank(r["last"], "") else r["last"]
            return {"first": f or None, "last": l or None, "title": None, "confidence": "high", "goes_by": None, "evidence": evid}

        for i in sorted(B):
            b = side(B[i], ev.evidence_fields("https://brave.example/x", "brave_search"))
            gem = Gm.get(i)
            g = side(gem, ev.evidence_fields("https://gem.example/x", "gemini_grounding", domain="gem.example"))
            gd = dict(g, status="ok") if g else ({"status": "error"} if gem and gem["grade"] == "error" else {"status": "ok", "first": None, "last": None})
            res = R(b, gd)
            gr = judge(res["first"] or "", res["last"] or "", gtid[i]) if res["deliver"] else judge("", "", gtid[i])
            if res["keep_brave"] and b:                      # Gemini error: the Brave answer stands as Brave delivered it
                gr = judge(b["first"] or "", b["last"] or "", gtid[i])
            grades.append(gr)
            hit_l, hit_s = gr in ("full", "partial"), gr == "full"
            if res["case"] == "conflict":
                cg = [judge(c["first"] or "", c["last"] or "", gtid[i]) for c in res["candidates"]]
                hit_l, hit_s = hit_l or any(x in ("full", "partial") for x in cg), hit_s or any(x == "full" for x in cg)
            reach_l += hit_l
            reach_s += hit_s
        s = summarize(grades)
        got = (s["correct_lenient"], s["correct_strict"], s["wrong"], s["correct_blanks"], reach_l, reach_s)
        check(f"production reconcile on {os.path.basename(bpath)[8:22]}: delivered lenient/strict/wrong/blanks/reachable "
              f"{got} == simulation {expected}", got == expected)
else:
    print("SKIP simulation parity: saved result CSVs not found")

print("\nStage C2 tests passed." if not failures else f"\n{len(failures)} FAILED: {failures}")
sys.exit(1 if failures else 0)
