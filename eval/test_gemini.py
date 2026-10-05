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

print("\nGemini harness tests passed." if not failures else f"\n{len(failures)} FAILED: {failures}")
sys.exit(1 if failures else 0)
