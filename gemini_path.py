"""Gemini with Grounding with Google Search: the second path (stage C2).

One grounded call per lead, answered as prose plus one JSON object. The prompt and the parsing moved
here UNCHANGED from eval/run_gemini.py (no prompt tuning); that script imports them from here.

Raw path: the answer is used as Gemini gave it. Its evidence is the grounding chunks behind it
(the chunks whose answer segment names the person, else the response's other chunks). Google
redirect links are resolved at run time by reading only the redirect's Location header.

Time is bounded per lead: at most PER_ATTEMPT_S for an attempt and TOTAL_S for the whole call,
with ONE retry (and only if time remains) for a timeout, an HTTP 429/5xx or an empty response.
A failure never raises: it returns status "error", and the graph falls back to the Brave answer.
An HTTP 402 (prepaid credits depleted) disables Gemini for the rest of the batch with one loud warning,
instead of failing row after row; the batch (or harness) calls reset() when it starts.
"""
import json
import re
import sys
import threading
import time
import unicodedata

import requests

import evidence

MODEL = "gemini-3.8-flash"
ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
PER_ATTEMPT_S = 180        # one attempt (answered calls took up to ~95s; the old 100s cap cut the tail)
TOTAL_S = 240              # the whole call, retry included
MIN_RETRY_S = 10           # no retry when less than this is left
MAX_RESOLVE = 3            # redirect links resolved per lead
RETRYABLE_HTTP = (429, 500, 502, 503, 504)

# Set by a 402 (credits depleted); cleared by reset() at the start of every batch.
_disabled = {"reason": None}
_disable_lock = threading.Lock()


def reset():
    """Re-enable Gemini. Call at the start of a batch or run."""
    with _disable_lock:
        _disabled["reason"] = None


def disabled_reason():
    return _disabled["reason"]


def _disable(reason):
    """Turn Gemini off for the rest of the batch; the loud warning is printed once, by the first caller."""
    with _disable_lock:
        first = _disabled["reason"] is None
        _disabled["reason"] = _disabled["reason"] or reason
    if first:
        bar = "!" * 78
        print(f"\n{bar}\n!!! GEMINI DISABLED for the rest of this batch: {reason[:200]}\n"
              f"!!! Every remaining lead gets the Brave answer only. Top up Gemini credits, then rerun.\n{bar}\n",
              file=sys.stderr, flush=True)

# USD per 1M tokens (input, output incl. thinking), and per search. LIST price, an upper bound:
# the 5,000 free searches/month are not subtracted. 3.8 Flash token prices double on 2027-01-01.
PRICES = {
    "gemini-3.8-flash": {"in": 0.75, "out": 3.75, "search": 14 / 1000},   # billed per search query run
    "gemini-2.5-flash": {"in": 0.30, "out": 2.50, "search": 35 / 1000},   # billed per grounded prompt
}
NULLISH = {"", "null", "none", "n/a", "na", "unknown", "<unknown>"}

PROMPT = """You are researching a local business to find its owner.

Business: {business_name}
Address: {address}
City and state: {city}, {state}
Phone: {phone}

Use Google Search. Rules:
- Find the owner (or founder or co-founder) of THIS business at THIS address. Businesses elsewhere with the same or a similar name are NOT this business: check the city, address or phone before you rely on a source.
- Only name a person that a source explicitly says owns, founded or runs this business. Never infer a name from the business name.
- Report one person, the primary owner. If you cannot find one with a source, say so. A blank is better than a guess.

Answer in two parts.
1. Two or three plain sentences saying who the owner is and which source says so (name the site).
2. On the final line, one JSON object and nothing after it:
{{"owner_first_name": string or null, "owner_last_name": string or null, "goes_by_name": string or null, "title": string or null, "confidence": "high" or "medium" or "low", "source_urls": [string]}}
Use null where unknown. Use confidence "high" only when a source explicitly names this person as the owner of this business."""


def build_prompt(lead):
    """Name, address, city, state, phone. The website is deliberately not sent."""
    v = lambda x: x or "(not provided)"
    return PROMPT.format(business_name=lead["business_name"], address=v(lead["address"]),
                         city=v(lead["city"]), state=v(lead["state"]), phone=v(lead["phone"]))


def _nullish(s):
    return s is None or str(s).strip().lower() in NULLISH


def parse_answer(text):
    """Pull the final JSON object out of Gemini's text. Returns (dict, prose) or (None, text).

    Scans from the last '{' backwards for an object that parses and carries the expected key,
    so braces in the prose cannot confuse it."""
    dec = json.JSONDecoder()
    for m in reversed([m.start() for m in re.finditer(r"\{", text or "")]):
        try:
            obj, _ = dec.raw_decode(text[m:])
        except ValueError:
            continue
        if isinstance(obj, dict) and "owner_first_name" in obj:
            clean = lambda k: None if _nullish(obj.get(k)) else str(obj[k]).strip()
            conf = str(obj.get("confidence", "low")).strip().lower()
            urls = obj.get("source_urls") if isinstance(obj.get("source_urls"), list) else []
            return {"first": clean("owner_first_name"), "last": clean("owner_last_name"),
                    "goes_by": clean("goes_by_name"), "title": clean("title"),
                    "confidence": conf if conf in ("high", "medium", "low") else "low",
                    "model_urls": [str(u) for u in urls]}, text[:m].strip()
    return None, (text or "").strip()


def extract_grounding(resp):
    """Read groundingMetadata off the first candidate. Tolerates it being absent.

    Fields (REST, camelCase): webSearchQueries[], groundingChunks[].web.{uri,title},
    groundingSupports[].{segment.text, groundingChunkIndices[]}. A support's segment is a span of
    the MODEL'S answer, and its indices point at the chunks (sources) backing that span."""
    cand = (resp.get("candidates") or [{}])[0]
    gm = cand.get("groundingMetadata")
    out = {"present": bool(gm), "queries": [], "chunks": [], "supports": []}
    if not gm:
        return out
    out["queries"] = list(gm.get("webSearchQueries") or [])
    for c in gm.get("groundingChunks") or []:
        web = c.get("web") or {}
        out["chunks"].append({"uri": web.get("uri", ""), "title": web.get("title", "")})
    for s in gm.get("groundingSupports") or []:
        out["supports"].append({"text": (s.get("segment") or {}).get("text", ""),
                                "indices": list(s.get("groundingChunkIndices") or [])})
    return out


def response_shape(resp):
    """Which grounding fields the response actually carried (counts and key names only, no content)."""
    cand = (resp.get("candidates") or [{}])[0]
    gm = cand.get("groundingMetadata")
    shape = {"candidate_keys": sorted(cand), "finish": cand.get("finishReason"),
             "groundingMetadata": bool(gm)}
    if gm:
        shape.update(gm_keys=sorted(gm), groundingChunks=len(gm.get("groundingChunks") or []),
                     groundingSupports=len(gm.get("groundingSupports") or []),
                     webSearchQueries=len(gm.get("webSearchQueries") or []))
    return shape


def diagnostics(resp):
    """Why a response looks the way it does: finish reason, any prompt block, and the token split
    (thinking tokens are billed and counted against the output budget). No content, only counts."""
    cand = (resp.get("candidates") or [{}])[0]
    u = resp.get("usageMetadata") or {}
    return {"finish_reason": cand.get("finishReason") or ("NO_CANDIDATES" if not resp.get("candidates") else ""),
            "block_reason": (resp.get("promptFeedback") or {}).get("blockReason", ""),
            "prompt_tokens": u.get("promptTokenCount", 0), "tooluse_tokens": u.get("toolUsePromptTokenCount", 0),
            "thought_tokens": u.get("thoughtsTokenCount", 0), "output_tokens": u.get("candidatesTokenCount", 0)}


def answer_text(resp):
    cand = (resp.get("candidates") or [{}])[0]
    parts = (cand.get("content") or {}).get("parts") or []
    return "".join(p.get("text", "") for p in parts if not p.get("thought"))


def is_empty(resp):
    """No answer text at all: no candidates, no parts, or only thought parts."""
    return not answer_text(resp).strip()


def estimate_cost(model, usage, n_queries):
    p = PRICES.get(model)
    if not p:
        return 0.0
    tin = (usage.get("promptTokenCount", 0) or 0) + (usage.get("toolUsePromptTokenCount", 0) or 0)
    tout = (usage.get("candidatesTokenCount", 0) or 0) + (usage.get("thoughtsTokenCount", 0) or 0)
    searches = n_queries if model.startswith("gemini-3") else (1 if n_queries else 0)
    return (tin * p["in"] + tout * p["out"]) / 1_000_000 + searches * p["search"]


# --- Evidence for a Gemini answer ----------------------------------------------------------------

def _words(text):
    t = unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z\s]", " ", t.replace("'", "")).split()


def supporting_sources(answer, grounding):
    """The chunks behind the answer. Returns (chunks, support) with support 'segment' when a supported
    segment of Gemini's answer names the person, 'response' when only the response's other chunks exist
    (the name is in the answer but no grounded segment holds it), 'none' when there is no chunk at all."""
    toks = _words(f"{answer.get('first') or ''} {answer.get('last') or ''}")
    chunks, seen = [], set()
    for s in grounding["supports"]:
        if not s["indices"] or not toks:
            continue
        if all(t in set(_words(s["text"])) for t in toks):
            for i in s["indices"]:
                if 0 <= i < len(grounding["chunks"]) and i not in seen:
                    seen.add(i)
                    chunks.append(grounding["chunks"][i])
    if chunks:
        return chunks, "segment"
    allc = [c for c in grounding["chunks"] if c.get("uri")]
    return (allc, "response") if allc else ([], "none")


def resolve_redirect(url, timeout=5):
    """Real URL behind a Google grounding redirect, by reading ONLY the Location header: the redirect is
    requested with allow_redirects=False and the target site is never fetched. Returns (url, kind):
    (real_url, 'direct') on success, else the original link with kind 'redirect' (never raises)."""
    if evidence.url_kind(url) != "redirect":
        return url, "direct"
    try:
        r = requests.get(url, allow_redirects=False, timeout=timeout)
        loc = r.headers.get("Location", "")
        if r.status_code in (301, 302, 303, 307, 308) and loc.lower().startswith(("http://", "https://")):
            return loc, "direct"
    except requests.RequestException:
        pass
    return url, "redirect"


# --- The call, bounded in time ----------------------------------------------------------------------

def call_once(prompt, model, api_key, timeout):
    """One grounded generateContent call. Returns (resp, error, retryable). Key in a header, never a URL."""
    body = {"contents": [{"parts": [{"text": prompt}]}], "tools": [{"google_search": {}}]}
    try:
        r = requests.post(ENDPOINT.format(model=model), json=body, timeout=(10, max(timeout, 1)),
                          headers={"x-goog-api-key": api_key, "Content-Type": "application/json"})
    except requests.exceptions.Timeout:
        return None, f"timeout after {timeout:.0f}s", True
    except requests.RequestException as e:
        return None, type(e).__name__, True
    if r.status_code == 200:
        try:
            return r.json(), "", False
        except ValueError:
            return None, "response was not JSON", True
    try:
        msg = r.json().get("error", {}).get("message", "")
    except ValueError:
        msg = ""
    return None, f"HTTP {r.status_code}: {msg[:160]}", r.status_code in RETRYABLE_HTTP


def _failed(error, attempts, seconds, cost, status="error"):
    return {"status": status, "error": error, "first": None, "last": None, "goes_by": None, "title": None,
            "confidence": None, "evidence": evidence.empty_evidence(), "support": "none", "searches": 0,
            "cost_usd": round(cost, 4), "seconds": round(seconds, 1), "attempts": attempts, "model_urls": []}


def ask_gemini(lead, api_key, model=MODEL, per_attempt=PER_ATTEMPT_S, total=TOTAL_S, call=None, clock=None):
    """The whole Gemini path for one lead. Never raises. Returns a dict:
    status 'ok' | 'error', error, first, last, goes_by, title, confidence, evidence (stage A fields),
    support ('segment' | 'response' | 'none'), searches, cost_usd, seconds, attempts.

    At most two attempts, and the second only if at least MIN_RETRY_S of the TOTAL_S budget is left.
    A timeout, HTTP 429/5xx or an empty response is retried once; other HTTP errors are not."""
    call, clock = call or call_once, clock or time.monotonic
    if not api_key:
        return _failed("GEMINI_API_KEY is not set", 0, 0.0, 0.0)
    if _disabled["reason"]:
        return _failed(f"Gemini disabled for this batch after: {_disabled['reason'][:80]}", 0, 0.0, 0.0, status="disabled")
    prompt, start, spent, attempts, err, resp = build_prompt(lead), clock(), 0.0, 0, "", None
    for attempt in (1, 2):
        remaining = total - (clock() - start)
        if attempt == 2 and remaining < MIN_RETRY_S:
            break
        attempts += 1
        resp, err, retryable = call(prompt, model, api_key, min(per_attempt, remaining))
        if not err and not is_empty(resp):
            break
        if err.startswith("HTTP 402"):                  # credits depleted: stop calling Gemini for this batch
            _disable(err)
            retryable = False
        if not err:                                    # empty response: billed, and retryable
            d = diagnostics(resp)
            err = f"empty response (finish={d['finish_reason']}, block={d['block_reason'] or '-'})"
            spent += estimate_cost(model, resp.get("usageMetadata") or {}, 0)
            retryable = True
        resp = None
        if not retryable:
            break
    seconds = clock() - start
    if resp is None:
        return _failed(f"{err}; {attempts} attempt(s), {seconds:.0f}s" if attempts > 1 else err, attempts, seconds, spent)

    answer, _ = parse_answer(answer_text(resp))
    grounding = extract_grounding(resp)
    cost = spent + estimate_cost(model, resp.get("usageMetadata") or {}, len(grounding["queries"]))
    if answer is None:
        return _failed("answer had no parseable JSON", attempts, seconds, cost)
    out = {"status": "ok", "error": None, "first": answer["first"], "last": answer["last"],
           "goes_by": answer["goes_by"], "title": answer["title"], "confidence": answer["confidence"],
           "evidence": evidence.empty_evidence(), "support": "none", "searches": len(grounding["queries"]),
           "cost_usd": round(cost, 4), "seconds": round(seconds, 1), "attempts": attempts,
           "model_urls": answer["model_urls"]}
    if answer["first"] or answer["last"]:
        chunks, support = supporting_sources(answer, grounding)
        out["support"] = support
        if chunks:
            resolved = [resolve_redirect(c["uri"]) if i < MAX_RESOLVE else (c["uri"], evidence.url_kind(c["uri"]))
                        for i, c in enumerate(chunks)]
            first_url, kind = resolved[0]
            domain = evidence.domain_of(first_url) if kind == "direct" else chunks[0]["title"]
            ev = evidence.evidence_fields(first_url, evidence.SRC_GEMINI, own_website=lead.get("website", "") or "",
                                          domain=domain, other_urls=[u for u, _ in resolved[1:]])
            ev["evidence_url_kind"] = kind
            out["evidence"] = ev
    return out
