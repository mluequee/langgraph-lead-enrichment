"""Read-only diagnostic: why does fetch_about_page come back empty on the Charlotte leads?

Free. No Brave, Anthropic or Gemini call, no secrets file, does not import src/graph.py.
Network goes only to the lead websites themselves: plain GETs, one at a time, 1 s apart,
10 s timeout, no retries. Redirects are followed by hand and ONLY while they stay on the
lead's own host (www. ignored, subdomains allowed). A redirect to any other host is
recorded as `offsite redirect` and NOT requested. Stops if 3 different sites in a row
answer 429. Prints only: no page is saved, no page text is printed (keyword counts only).

Copied from src/graph.py (as of this commit), not imported:
  - the usable-domain rule: fetch_about_page() itself decides, using
    `website and not _is_booking_platform(website)` with NOT_OWN_SITE_DOMAINS
    (the fallback_website branch needs Brave `locations`, so it is not reproduced here);
  - the domain parse (scheme://netloc, https:// added only when the website has no scheme);
  - the 4 guessed paths, in pipeline order;
  - the "old" request: requests.get(url, timeout=...) with requests' default headers
    (python-requests User-Agent). The pipeline uses timeout=5; this script uses 10 and
    marks any request slower than 5 s (`>5s`), which the pipeline would have lost.

Run from the repo root, venv active:
    python eval/diag_fetch_status.py
"""
import os
import re
import sqlite3
import sys
import time
from collections import Counter, defaultdict
from urllib.parse import urljoin, urlparse

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
import intake  # noqa: E402  (stdlib-only module: csv, logging; skip_reason is pure)

DB_PATH = os.path.join(ROOT, "leads.db")

# ---- copied verbatim from src/graph.py ---------------------------------------------
BOOKING_PLATFORM_DOMAINS = ("booksy.com", "thecut.co", "squareup.com", "square.site", "vagaro.com", "styleseat.com")
NOT_OWN_SITE_DOMAINS = BOOKING_PLATFORM_DOMAINS + (
    "facebook.com", "instagram.com", "linktr.ee", "tiktok.com", "linkedin.com", "yelp.com", "google.com"
)


def _is_booking_platform(url: str) -> bool:
    if not url:
        return False
    netloc = urlparse(url).netloc.lower()
    return any(d in netloc for d in NOT_OWN_SITE_DOMAINS)


ABOUT_PATHS = ["/about", "/about-us", "/team", "/about-page"]
PIPELINE_TIMEOUT = 5
# -------------------------------------------------------------------------------------

TIMEOUT = 10          # task rule
PAUSE = 1.0           # seconds between requests (every hop counts as a request)
MAX_HOPS = 30         # requests' own default redirect limit
PATHS = ["/"] + ABOUT_PATHS

CHROME_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"),
    "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
               "image/webp,image/apng,*/*;q=0.8"),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
    "Upgrade-Insecure-Requests": "1",
}
HEADER_SETS = [("old", None), ("chrome", CHROME_HEADERS)]

KEYWORDS = ["founder", "owner", "ceo", "team", "about"]
KW_RE = {k: re.compile(rf"\b{k}", re.IGNORECASE) for k in KEYWORDS}
OWNER_KW = ("founder", "owner", "ceo")   # the three the pipeline's sentence filter keeps

OUTCOMES = ["200 html", "3xx loop", "403", "404", "429", "5xx", "other 4xx", "other status",
            "timeout", "SSL error", "connection error", "non-html", "offsite redirect", "other error"]


def pipeline_domain(website):
    """fetch_about_page's domain parse, verbatim logic."""
    parsed = urlparse(website)
    if not parsed.scheme:
        parsed = urlparse(f"https://{website}")
    return f"{parsed.scheme}://{parsed.netloc}"


def base_host(host):
    host = (host or "").lower().split(":")[0]
    return host[4:] if host.startswith("www.") else host


def same_site(url, site_base):
    h = base_host(urlparse(url).netloc)
    return h == site_base or h.endswith("." + site_base)


def load_sites():
    """Unique usable domains -> lead ids, from leads.db opened read-only."""
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    rows = conn.execute("SELECT id, place_id, business_name, website FROM leads ORDER BY id").fetchall()
    conn.close()
    counts = Counter()
    sites = {}
    for lead_id, place_id, name, website in rows:
        if intake.skip_reason(name, place_id or ""):
            counts["intake skip"] += 1
            continue
        website = (website or "").strip()
        if not website:
            counts["no website"] += 1
            continue
        if _is_booking_platform(website):
            counts["not own site (booking/social)"] += 1
            continue
        try:
            domain = pipeline_domain(website)
        except Exception:
            counts["unparseable"] += 1
            continue
        counts["usable"] += 1
        sites.setdefault(domain, []).append(lead_id)
    return len(rows), counts, sites


class Stop(Exception):
    pass


_last_request = [0.0]


def polite_get(url, headers):
    wait = PAUSE - (time.monotonic() - _last_request[0])
    if wait > 0:
        time.sleep(wait)
    try:
        if headers is None:
            return requests.get(url, timeout=TIMEOUT, allow_redirects=False)
        return requests.get(url, timeout=TIMEOUT, allow_redirects=False, headers=headers)
    finally:
        _last_request[0] = time.monotonic()


def fetch(url, headers, site_base):
    """Follow redirects by hand, on-site only. Returns a result dict; never raises."""
    r = {"status": None, "exc": None, "final": url, "ctype": "", "bytes": 0, "hops": 0,
         "elapsed": 0.0, "kw": None, "offsite": "", "outcome": None}
    seen = set()
    current = url
    t0 = time.monotonic()
    try:
        while True:
            seen.add(current)
            resp = polite_get(current, headers)
            r["elapsed"] += resp.elapsed.total_seconds()
            r["status"] = resp.status_code
            r["final"] = current
            if resp.is_redirect or resp.status_code in (301, 302, 303, 307, 308):
                loc = resp.headers.get("Location")
                if not loc:
                    r["outcome"] = "other status"
                    break
                nxt = urljoin(current, loc)
                if not same_site(nxt, site_base):
                    r["outcome"] = "offsite redirect"
                    r["offsite"] = urlparse(nxt).netloc.lower()
                    break
                r["hops"] += 1
                if nxt in seen or r["hops"] > MAX_HOPS:
                    r["outcome"] = "3xx loop"
                    break
                current = nxt
                continue
            body = resp.content
            r["ctype"] = resp.headers.get("Content-Type", "")
            r["bytes"] = len(body)
            code = resp.status_code
            if code == 200:
                if "html" in r["ctype"].lower():
                    r["outcome"] = "200 html"
                    text = resp.text
                    r["kw"] = {k: len(rx.findall(text)) for k, rx in KW_RE.items()}
                    title = re.search(r"<title[^>]*>(.*?)</title>", text, re.IGNORECASE | re.DOTALL)
                    r["soft404"] = bool(title and re.search(r"not found|404", title.group(1), re.IGNORECASE))
                else:
                    r["outcome"] = "non-html"
            elif code == 403:
                r["outcome"] = "403"
            elif code == 404:
                r["outcome"] = "404"
            elif code == 429:
                r["outcome"] = "429"
            elif 500 <= code < 600:
                r["outcome"] = "5xx"
            elif 400 <= code < 500:
                r["outcome"] = "other 4xx"
            else:
                r["outcome"] = "other status"
            break
    except requests.exceptions.SSLError as e:
        r["exc"], r["outcome"] = type(e).__name__, "SSL error"
    except requests.exceptions.Timeout as e:
        r["exc"], r["outcome"] = type(e).__name__, "timeout"
    except requests.exceptions.ConnectionError as e:
        # requests wraps some SSL failures inside ConnectionError
        name = type(e).__name__
        if "SSL" in repr(e) or "CERTIFICATE" in repr(e).upper():
            r["exc"], r["outcome"] = name + "(ssl)", "SSL error"
        else:
            r["exc"], r["outcome"] = name, "connection error"
    except Exception as e:  # noqa: BLE001
        r["exc"], r["outcome"] = type(e).__name__, "other error"
    r["wall"] = time.monotonic() - t0
    return r


def line(lead_ids, domain, path, hdr, r):
    ids = ",".join(str(i) for i in lead_ids)
    status = r["exc"] or str(r["status"])
    kw = "-" if r["kw"] is None else " ".join(f"{k}={'y' if n else 'n'}({n})" for k, n in r["kw"].items())
    flags = []
    if r["hops"]:
        flags.append(f"hops={r['hops']}")
    if r["offsite"]:
        flags.append(f"offsite->{r['offsite']}")
    if r["elapsed"] > PIPELINE_TIMEOUT:
        flags.append(">5s")
    if r.get("soft404"):
        flags.append("title-says-404")
    if path != "/" and r["outcome"] == "200 html" and urlparse(r["final"]).path in ("", "/"):
        flags.append("landed-on-home")
    ctype = (r["ctype"] or "-").split(";")[0]
    print(f"lead={ids:<7} {domain:<46} {path:<11} {hdr:<6} {r['outcome']:<16} {status:<22} "
          f"final={r['final']} ctype={ctype} bytes={r['bytes']} t={r['elapsed']:.1f}s "
          f"kw[{kw}] {' '.join(flags)}", flush=True)


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    total, counts, sites = load_sites()
    print(f"leads.db: {total} leads; " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    print(f"unique usable domains: {len(sites)}; planned requests (before redirect hops): "
          f"{len(sites) * len(PATHS) * len(HEADER_SETS)}")
    print(f"timeout={TIMEOUT}s (pipeline uses {PIPELINE_TIMEOUT}s; '>5s' marks the difference), "
          f"pause={PAUSE}s, no retries, on-site redirects only\n")

    results = defaultdict(dict)   # domain -> (path, hdr) -> r
    streak_hosts = []             # consecutive 429s, distinct sites
    stopped = None
    try:
        for domain, lead_ids in sites.items():
            site_base = base_host(urlparse(domain).netloc)
            for path in PATHS:
                for hdr, headers in HEADER_SETS:
                    r = fetch(domain + path, headers, site_base)
                    results[domain][(path, hdr)] = r
                    line(lead_ids, domain, path, hdr, r)
                    if r["outcome"] == "429":
                        if site_base not in streak_hosts:
                            streak_hosts.append(site_base)
                        if len(streak_hosts) >= 3:
                            raise Stop(f"429 from 3 different sites in a row: {streak_hosts}")
                    else:
                        streak_hosts = []
    except Stop as e:
        stopped = str(e)
        print(f"\nSTOPPED: {stopped}")

    summarize(sites, results, stopped)


def usable(r):
    return r is not None and r["outcome"] == "200 html"


def summarize(sites, results, stopped):
    print("\n" + "=" * 100)
    print("SUMMARY" + (" (PARTIAL: run stopped)" if stopped else ""))
    print("=" * 100)
    n_req = sum(len(v) for v in results.values())
    print(f"logical requests: {n_req}; sites reached: {len(results)} of {len(sites)}\n")

    # 1. outcome counts, all 5 paths, and the 4 about-paths alone
    for label, paths in (("all 5 paths (homepage + 4 guessed)", PATHS), ("4 guessed about-paths only", ABOUT_PATHS)):
        c = {h: Counter() for h, _ in HEADER_SETS}
        for d, rs in results.items():
            for (p, h), r in rs.items():
                if p in paths:
                    c[h][r["outcome"]] += 1
        print(f"Outcome counts, {label}:")
        print(f"  {'outcome':<18}{'old':>6}{'chrome':>8}")
        for o in OUTCOMES:
            if c["old"][o] or c["chrome"][o]:
                print(f"  {o:<18}{c['old'][o]:>6}{c['chrome'][o]:>8}")
        print(f"  {'TOTAL':<18}{sum(c['old'].values()):>6}{sum(c['chrome'].values()):>8}\n")

    # 2. sites with a usable page
    def any_ok(d, hdr, paths):
        return any(usable(results[d].get((p, hdr))) for p in paths)

    for label, paths in (("any of the 5 paths", PATHS), ("any of the 4 about-paths", ABOUT_PATHS),
                         ("homepage", ["/"])):
        o = sum(any_ok(d, "old", paths) for d in results)
        ch = sum(any_ok(d, "chrome", paths) for d in results)
        either = sum(any_ok(d, "old", paths) or any_ok(d, "chrome", paths) for d in results)
        print(f"Sites with 200 html on {label:<26} old={o:<3} chrome={ch:<3} either={either}")

    # what the pipeline itself would get: first about-path, in order, that returns 200 (any type), old headers, 5s
    pipe_ok = pipe_kw = pipe_home = 0
    for d, rs in results.items():
        for p in ABOUT_PATHS:
            r = rs.get((p, "old"))
            if r and r["status"] == 200 and r["outcome"] in ("200 html", "non-html") and r["elapsed"] <= PIPELINE_TIMEOUT:
                pipe_ok += 1
                if r["kw"] and any(r["kw"][k] for k in OWNER_KW):
                    pipe_kw += 1
                if urlparse(r["final"]).path in ("", "/"):
                    pipe_home += 1
                break
    print(f"\nPipeline as written (old headers, 4 paths in order, first 200 within 5s): "
          f"{pipe_ok} sites get a page; {pipe_kw} of those pages contain founder/owner/ceo; "
          f"{pipe_home} of those pages are the homepage reached by redirect")

    # 3. per-site homepage + classification
    print("\nPer site (homepage 200 html? | about-path 200 html? | class):")
    cls_count = Counter()
    for d, lead_ids in sites.items():
        if d not in results:
            continue
        rs = results[d]
        h_old, h_ch = usable(rs.get(("/", "old"))), usable(rs.get(("/", "chrome")))
        a_old, a_ch = any_ok(d, "old", ABOUT_PATHS), any_ok(d, "chrome", ABOUT_PATHS)
        all_out = {r["outcome"] for r in rs.values()}
        old_out = Counter(r["outcome"] for (p, h), r in rs.items() if h == "old")
        if a_old:
            cls = "about-path works with old headers"
        elif a_ch:
            cls = "header block (chrome fixes an about-path)"
        elif h_ch and not h_old:
            cls = "header block (chrome fixes homepage only)"
        elif h_old:
            cls = "guessed paths missing, homepage works"
        elif all_out <= {"timeout", "SSL error"}:
            cls = "timeout/SSL under both"
        elif not (h_ch or a_ch):
            cls = "dead/unreachable under both"
        else:
            cls = "other"
        cls_count[cls] += 1
        old_s = ", ".join(f"{k}:{v}" for k, v in old_out.most_common())
        print(f"  lead={','.join(map(str, lead_ids)):<7} {d:<46} home old={'Y' if h_old else 'n'} "
              f"chrome={'Y' if h_ch else 'n'} | about old={'Y' if a_old else 'n'} chrome={'Y' if a_ch else 'n'} "
              f"| {cls}  [old outcomes: {old_s}]")

    print("\nSite classes:")
    for k, v in cls_count.most_common():
        print(f"  {v:>3}  {k}")
    if stopped:
        print(f"\nRun stopped early: {stopped}")


if __name__ == "__main__":
    main()
