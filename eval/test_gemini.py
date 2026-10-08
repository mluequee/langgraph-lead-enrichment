"""Free self-check for eval/run_gemini.py: no API calls, no key needed.
    python eval/test_gemini.py

Uses a hand-built response in the documented generateContent shape (groundingMetadata with
webSearchQueries, groundingChunks, groundingSupports). The real response shape is confirmed
on the first live smoke run; this checks the parsing and the gate against the shape we expect.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import run_gemini as rg                                    # noqa: E402

failures = []


def check(label, ok):
    print(("PASS " if ok else "FAIL ") + label)
    if not ok:
        failures.append(label)


LEAD = {"business_name": "Wilmar Barber Shop", "address": "123 Main St, Concord, NC 28025, USA",
        "city": "Concord", "state": "NC", "phone": "(704) 555-0100", "website": "https://secret-site.example"}
p = rg.build_prompt(LEAD)
check("prompt carries name, address, city, state, phone",
      all(x in p for x in ("Wilmar Barber Shop", "123 Main St", "Concord, NC", "(704) 555-0100")))
check("prompt does not send the website", "secret-site.example" not in p)

TEXT = ('Greg Furr is the owner of Wil-Mar Barber Shop, per LinkedIn.\n'
        'Note {braces} in prose are fine.\n'
        '{"owner_first_name": "Greg", "owner_last_name": "Furr", "goes_by_name": null, '
        '"title": "Owner", "confidence": "high", "source_urls": ["https://made-up.example/x"]}')
RESP = {"candidates": [{
    "content": {"parts": [{"text": TEXT}]},
    "groundingMetadata": {
        "webSearchQueries": ["Wilmar Barber Shop Concord NC owner"],
        "groundingChunks": [{"web": {"uri": "https://vertexaisearch.example/r/1", "title": "linkedin.com"}},
                            {"web": {"uri": "https://vertexaisearch.example/r/2", "title": "yelp.com"}}],
        "groundingSupports": [{"segment": {"startIndex": 0, "endIndex": 56, "text": "Greg Furr is the owner of Wil-Mar Barber Shop, per LinkedIn."},
                               "groundingChunkIndices": [0]}]},
}], "usageMetadata": {"promptTokenCount": 800, "toolUsePromptTokenCount": 4000,
                      "candidatesTokenCount": 200, "thoughtsTokenCount": 300, "totalTokenCount": 5300}}

ans, prose = rg.parse_answer(rg.answer_text(RESP))
check("JSON found after prose containing braces", ans is not None and ans["first"] == "Greg" and ans["last"] == "Furr")
check("null goes-by becomes None", ans["goes_by"] is None)
check("prose is separated from the JSON", prose.startswith("Greg Furr is the owner"))
g = rg.extract_grounding(RESP)
check("grounding parsed: 1 query, 2 chunks, 1 support", (len(g["queries"]), len(g["chunks"]), len(g["supports"])) == (1, 2, 1))
ok, sources, reason = rg.gate(ans, g)
check("gate passes when a grounded segment names the owner", ok and reason == "ok")
check("gate sources come from grounding chunks, not the model's own URLs",
      [s["uri"] for s in sources] == ["https://vertexaisearch.example/r/1"])

other = dict(ans, first="Gary", last="Fox")
check("gate rejects a name no grounded segment contains", not rg.gate(other, g)[0])
check("gate rejects when groundingMetadata is missing", rg.gate(ans, rg.extract_grounding({"candidates": [{}]}))[1:] == ([], "no groundingMetadata in the response"))
nosup = {"candidates": [{"groundingMetadata": {"groundingChunks": g["chunks"] and [{"web": {"uri": "u", "title": "t"}}]}}]}
check("gate rejects when there are no groundingSupports", rg.gate(ans, rg.extract_grounding(nosup))[2] == "no groundingSupports")
unsupported = {"candidates": [{"groundingMetadata": {"groundingChunks": [{"web": {"uri": "u", "title": "t"}}],
               "groundingSupports": [{"segment": {"text": "Greg Furr"}, "groundingChunkIndices": []}]}}]}
check("a support with no chunk indices does not count", not rg.gate(ans, rg.extract_grounding(unsupported))[0])
check("blank answer is never 'supported'", not rg.gate({"first": None, "last": None}, g)[0])

check("garbage text gives no answer", rg.parse_answer("I could not find anything.")[0] is None)
none_ans, _ = rg.parse_answer('{"owner_first_name": null, "owner_last_name": "unknown", "confidence": "weird", "source_urls": "x"}')
check("null/unknown names and bad confidence normalize", none_ans["first"] is None and none_ans["last"] is None
      and none_ans["confidence"] == "low" and none_ans["model_urls"] == [])

c = rg.estimate_cost("gemini-3.8-flash", RESP["usageMetadata"], 1)
check("cost = tokens at list price plus one search", abs(c - ((4800 * 0.75 + 500 * 3.75) / 1e6 + 0.014)) < 1e-9)
check("unknown model has no cost estimate", rg.estimate_cost("gemini-x", RESP["usageMetadata"], 3) == 0.0)

B = lambda f, l: {"first": f, "last": l}
check("same person: Greg Furr vs Greg Furr", rg.same_person(B("Greg", "Furr"), B("Greg", "Furr")))
check("same person: first-name-only vs full name", rg.same_person(B("Pablo", "<UNKNOWN>"), B("Pablo", "Reyes")))
check("different people disagree", not rg.same_person(B("Greg", "Furr"), B("Hunter", "Griffin")))
check("blank vs blank agree", rg.same_person(B("<UNKNOWN>", "<UNKNOWN>"), B("", "")))
check("blank vs named disagree", not rg.same_person(B("<UNKNOWN>", "<UNKNOWN>"), B("Greg", "Furr")))

# --- Empty-response retry (fake calls, no network) ---------------------------------------------
import csv                                                  # noqa: E402
import tempfile                                             # noqa: E402
from types import SimpleNamespace                           # noqa: E402

EMPTY = {"usageMetadata": {"promptTokenCount": 1000, "thoughtsTokenCount": 3000, "totalTokenCount": 4000}}   # no candidates
EMPTY_PARTS = {"candidates": [{"content": {"parts": []}, "finishReason": "STOP"}]}
THOUGHT_ONLY = {"candidates": [{"content": {"parts": [{"text": "thinking...", "thought": True}]}}]}
GOOD = RESP


class FakeCalls:
    """Stands in for call_gemini: returns the queued (resp, err) pairs in order and counts calls."""
    def __init__(self, *queue):
        self.queue, self.n = list(queue), 0

    def __call__(self, prompt, model, key):
        self.n += 1
        return self.queue.pop(0)


check("is_empty: no candidates", rg.is_empty(EMPTY))
check("is_empty: candidate with no parts", rg.is_empty(EMPTY_PARTS))
check("is_empty: thought-only parts", rg.is_empty(THOUGHT_ONLY))
check("is_empty: a real answer is not empty", not rg.is_empty(GOOD))

lines = []
f = FakeCalls((GOOD, ""))
resp, err, retries, spent = rg.ask_with_retry("p", "gemini-3.8-flash", "k", "Shop A", call=f, log=lines.append)
check("non-empty first response: one call, no retry, no log", (f.n, retries, err, lines) == (1, 0, "", []) and resp is GOOD)

lines = []
f = FakeCalls((EMPTY, ""), (GOOD, ""))
resp, err, retries, spent = rg.ask_with_retry("p", "gemini-3.8-flash", "k", "Shop B", call=f, log=lines.append)
check("empty then answer: exactly 2 calls, recovered, no error", f.n == 2 and retries == 1 and err == "" and resp is GOOD)
check("recovered retry is logged (row, attempt, outcome)",
      len(lines) == 2 and "Shop B" in lines[0] and "attempt=2" in lines[0] and "outcome=recovered" in lines[1])
check("the empty first reply's tokens are still counted as spend", spent > 0)

lines = []
f = FakeCalls((EMPTY, ""), (EMPTY, ""), (GOOD, ""))
resp, err, retries, spent = rg.ask_with_retry("p", "gemini-3.8-flash", "k", "Shop C", call=f, log=lines.append)
check("empty twice: retries ONCE only (2 calls, never a third)", f.n == 2 and retries == 1)
check("empty twice: returns an error, not a blank", err.startswith("empty response after retry"))
check("empty twice is logged as still empty -> error", len(lines) == 2 and "still empty" in lines[1] and "-> error" in lines[1])

lines = []
f = FakeCalls((EMPTY, ""), (None, "HTTP 503: unavailable"))
resp, err, retries, spent = rg.ask_with_retry("p", "gemini-3.8-flash", "k", "Shop D", call=f, log=lines.append)
check("empty then HTTP error: error row, 2 calls", f.n == 2 and retries == 1 and "retry failed" in err and resp is None)

lines = []
f = FakeCalls((None, "HTTP 429: quota"), (GOOD, ""))
resp, err, retries, spent = rg.ask_with_retry("p", "gemini-3.8-flash", "k", "Shop E", call=f, log=lines.append)
check("first-call HTTP error is NOT an empty-response retry", f.n == 1 and retries == 0 and err.startswith("HTTP 429") and lines == [])

# End to end through run(): the CSV keeps blank and error separate.
NO_OWNER_TEXT = ('No source names an owner.\n{"owner_first_name": null, "owner_last_name": null, '
                 '"goes_by_name": null, "title": null, "confidence": "low", "source_urls": []}')
BLANK_RESP = {"candidates": [{"content": {"parts": [{"text": NO_OWNER_TEXT}]}, "finishReason": "STOP"}]}
rows = [{"id": i, "business_name": n, "subset": "audit", "key": n.lower(), "kind": "no_owner", "owners": []}
        for i, n in ((1, "Blank Shop"), (2, "Empty Twice Shop"), (3, "Recovered Shop"))]
leads = {r["key"]: {"business_name": r["business_name"], "address": "", "city": "", "state": "", "phone": "", "website": ""}
         for r in rows}
queue = {"Blank Shop": [(BLANK_RESP, "")], "Empty Twice Shop": [(EMPTY, ""), (EMPTY, "")], "Recovered Shop": [(EMPTY, ""), (BLANK_RESP, "")]}


def fake_call(prompt, model, key):
    name = prompt.split("Business: ")[1].split("\n")[0]
    return queue[name].pop(0)


real_call, rg.call_gemini = rg.call_gemini, fake_call
try:
    log = []
    gated, raw = rg.run(rows, leads, SimpleNamespace(model="gemini-3.8-flash", max_errors=3, show_shape=False), "k", log)
finally:
    rg.call_gemini = real_call
by = {r["business_name"]: r for r in gated}
check("a real 'no owner' answer is a blank with status ok",
      by["Blank Shop"]["grade"] == "correct_blank" and by["Blank Shop"]["status"] == "ok" and by["Blank Shop"]["retries"] == 0)
check("empty twice is status error, grade error, not a blank",
      by["Empty Twice Shop"]["status"] == "error" and by["Empty Twice Shop"]["grade"] == "error" and by["Empty Twice Shop"]["error"])
check("recovered row is a normal answer that records retries=1",
      by["Recovered Shop"]["status"] == "ok" and by["Recovered Shop"]["retries"] == 1 and by["Recovered Shop"]["grade"] == "correct_blank")
check("run() logs every retry", sum("RETRY" in line for line in log) == 4)
with tempfile.TemporaryDirectory() as d:
    path = os.path.join(d, "r.csv")
    rg.write_csv(path, gated)
    back = {r["business_name"]: r for r in csv.DictReader(open(path, encoding="utf-8"))}
check("CSV keeps the two cases apart (status ok/blank vs error, with a retries column)",
      back["Blank Shop"]["status"] == "ok" and back["Empty Twice Shop"]["status"] == "error"
      and back["Empty Twice Shop"]["error"] and not back["Blank Shop"]["error"] and back["Recovered Shop"]["retries"] == "1")

# --- Stage E0: the harness's own network call is guarded by --gemini ------------------------------------------
import requests                                            # noqa: E402
_posts = []
_real_post = requests.post
requests.post = lambda *a, **k: _posts.append(1)
try:
    r_off = rg.call_gemini("p", "m", "k")
    check("call_gemini without --gemini: error, requests.post never reached", r_off[0] is None and "off" in r_off[1] and _posts == [])
finally:
    requests.post = _real_post
_src = open(rg.__file__, encoding="utf-8").read()
check("run_gemini main() refuses to run without --gemini", "Pass --gemini to allow it" in _src and "confirm_opt_in" in _src)

print("\nGemini harness tests passed." if not failures else f"\n{len(failures)} FAILED: {failures}")
sys.exit(1 if failures else 0)
