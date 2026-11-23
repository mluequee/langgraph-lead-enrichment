"""Read-only ceiling check for Stage H (website reading). Builds nothing, scores nothing.

Question: of the Charlotte DEV leads with a known truth owner, how many have that owner's
name on a page we can actually fetch from the shop's own site? An upper limit on what
reading the website could add.

Free. No Brave, Anthropic or Gemini call, no secrets file, does not import src/graph.py.
Reads: eval/data/ground_truth.json (Charlotte dev set), leads.db (read-only), and the
latest full Charlotte results CSV (for the delivered grade only).
Network goes only to the lead websites: one request at a time, 1 s apart, 10 s timeout,
no retries. Chrome headers first; plain (python-requests) headers only as a fallback when
the Chrome request fails for a reason other than 404 / soft 404 / offsite redirect.
Redirects followed by hand and only while they stay on the lead's own domain.
Per site: homepage, the 4 guessed paths, and up to 5 same-domain links from the homepage
whose text or URL has about/team/staff/barber/owner/meet/story/contact.
Pages are held in memory only. Prints counts and lead ids only: no page text, no owner
names, no domains.

Copied from eval/diag_fetch_status.py (itself copied from src/graph.py), not imported:
the usable-site rule, the domain parse, the 4 guessed paths, the Chrome headers, the
on-site redirect rule.

Run from the repo root, venv active:
    python eval/diag_ceiling.py
"""
import csv
import json
import os
import re
import sqlite3
import sys
import time
import unicodedata
from collections import Counter
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse, urldefrag

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
import intake  # noqa: E402  (stdlib-only module; skip_reason is pure)

DB_PATH = os.path.join(ROOT, "leads.db")
GT_PATH = os.path.join(ROOT, "eval", "data", "ground_truth.json")
# Latest full Charlotte run (44 audit rows, Gemini off = the current default pipeline).
# leads.db `results` is empty, so the delivered grade comes from here.
RESULTS_CSV = os.path.join(ROOT, "eval", "data", "results_20261008_084655.csv")

# ---- copied from eval/diag_fetch_status.py (verbatim from src/graph.py) -------------
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

CHROME_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"),
    "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
               "image/webp,image/apng,*/*;q=0.8"),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
    "Upgrade-Insecure-Requests": "1",
}


def pipeline_domain(website):
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
# -------------------------------------------------------------------------------------

TIMEOUT = 10
PAUSE = 1.0
MAX_HOPS = 30
MAX_LINKS = 5
LINK_WORDS = ("about", "team", "staff", "barber", "owner", "meet", "story", "contact")
SKIP_EXT = (".pdf", ".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".mp4", ".zip", ".css", ".js", ".ico")
NEAR = 80
OWNER_WORD_RE = re.compile(r"\b(?:owner|owners|founder|founders|ceo)\b|\bowned by\b")

# ---- network guard: every request must go to the current lead's own domain ----------
REQUEST_LOG = []          # (host, allowed) for the final network report
_last_request = [0.0]


class Stop(Exception):
    pass


def polite_get(url, headers, site_base):
    if not same_site(url, site_base):
        raise RuntimeError("guard: refused a request off the lead's own domain")
    wait = PAUSE - (time.monotonic() - _last_request[0])
    if wait > 0:
        time.sleep(wait)
    REQUEST_LOG.append(base_host(urlparse(url).netloc))
    try:
        if headers is None:
            return requests.get(url, timeout=TIMEOUT, allow_redirects=False)
        return requests.get(url, timeout=TIMEOUT, allow_redirects=False, headers=headers)
    finally:
        _last_request[0] = time.monotonic()


def fetch(url, headers, site_base):
    """Follow redirects by hand, on-site only. Returns (outcome, final_url, html or None)."""
    seen = set()
    current = url
    hops = 0
    try:
        while True:
            seen.add(current)
            resp = polite_get(current, headers, site_base)
            if resp.status_code in (301, 302, 303, 307, 308):
                loc = resp.headers.get("Location")
                if not loc:
                    return "other status", current, None
                nxt = urljoin(current, loc)
                if not same_site(nxt, site_base):
                    return "offsite redirect", current, None
                hops += 1
                if nxt in seen or hops > MAX_HOPS:
                    return "3xx loop", current, None
                current = nxt
                continue
            code = resp.status_code
            if code == 200:
                if "html" not in resp.headers.get("Content-Type", "").lower():
                    return "non-html", current, None
                text = resp.text
                title = re.search(r"<title[^>]*>(.*?)</title>", text, re.IGNORECASE | re.DOTALL)
                if title and re.search(r"not found|404", title.group(1), re.IGNORECASE):
                    return "soft 404", current, None
                return "ok", current, text
            if code == 404:
                return "404", current, None
            if code == 429:
                return "429", current, None
            if code == 403:
                return "403", current, None
            if 500 <= code < 600:
                return "5xx", current, None
            if 400 <= code < 500:
                return "other 4xx", current, None
            return "other status", current, None
    except requests.exceptions.Timeout:
        return "timeout", current, None
    except requests.exceptions.SSLError:
        return "SSL error", current, None
    except requests.exceptions.ConnectionError as e:
        return ("SSL error" if "SSL" in repr(e) else "connection error"), current, None
    except RuntimeError:
        raise
    except Exception:  # noqa: BLE001
        return "other error", current, None


NO_FALLBACK = {"ok", "404", "soft 404", "offsite redirect"}


def fetch_with_fallback(url, site_base):
    out, final, html = fetch(url, CHROME_HEADERS, site_base)
    if out in NO_FALLBACK:
        return out, final, html, "chrome"
    out2, final2, html2 = fetch(url, None, site_base)
    return out2, final2, html2, "plain"


# ---- HTML -> visible text + ld+json ---------------------------------------------------
class TextAndLinks(HTMLParser):
    HIDDEN = {"script", "style", "noscript", "template", "svg", "head"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.text, self.ldjson, self.links = [], [], []
        self._hidden = 0
        self._in_ld = False
        self._a = None          # [href, [text parts]] while inside <a>

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "script" and (a.get("type") or "").lower().strip() == "application/ld+json":
            self._in_ld = True
            return
        if tag in self.HIDDEN:
            self._hidden += 1
        if tag == "a" and a.get("href"):
            self._a = [a["href"], []]
        if tag in ("br", "p", "div", "li", "h1", "h2", "h3", "h4", "h5", "h6", "td", "tr", "section"):
            self.text.append(" ")

    def handle_endtag(self, tag):
        if tag == "script" and self._in_ld:
            self._in_ld = False
            return
        if tag in self.HIDDEN and self._hidden:
            self._hidden -= 1
        if tag == "a" and self._a is not None:
            self.links.append((self._a[0], " ".join(self._a[1])))
            self._a = None

    def handle_data(self, data):
        if self._in_ld:
            self.ldjson.append(data)
            return
        if self._hidden:
            return
        self.text.append(data)
        if self._a is not None:
            self._a[1].append(data)


def parse(html):
    p = TextAndLinks()
    try:
        p.feed(html)
        p.close()
    except Exception:  # noqa: BLE001
        pass
    return " ".join(p.text), " ".join(p.ldjson), p.links


def norm(s):
    """Lowercase, strip accents, drop apostrophes, every other non-alphanumeric -> space."""
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(ch for ch in s if not unicodedata.combining(ch)).lower()
    s = re.sub(r"[’‘'`]", "", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return f" {s.strip()} "


def candidate_links(home_html, home_url, site_base, already):
    _, _, links = parse(home_html)
    picked = []
    for href, text in links:
        href = (href or "").strip()
        if not href or href.startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        url = urldefrag(urljoin(home_url, href))[0]
        pu = urlparse(url)
        if pu.scheme not in ("http", "https") or not same_site(url, site_base):
            continue
        if pu.path.lower().endswith(SKIP_EXT):
            continue
        hay = (text + " " + url).lower()
        if not any(w in hay for w in LINK_WORDS):
            continue
        key = (pu.path.rstrip("/") or "/").lower()
        if key in already:
            continue
        already.add(key)
        picked.append(url)
        if len(picked) >= MAX_LINKS:
            break
    return picked


# ---- name matching ------------------------------------------------------------------
def name_patterns(owner):
    """owner = {'first','last','aliases'}; returns compiled regexes on norm() text."""
    def tok(s):
        return norm(s).strip()
    firsts = [tok(owner.get("first", ""))]
    alias_firsts = []
    for al in owner.get("aliases") or []:
        t = tok(al).split()
        if t:
            alias_firsts.append(t[0])   # goes-by first name (e.g. a nickname)
    firsts = [f for f in firsts if f]
    alias_firsts = [a for a in alias_firsts if a and a not in firsts]
    last = tok(owner.get("last", ""))

    def full_re(fs):
        if not fs or not last:
            return None
        alt = "|".join(re.escape(f) for f in fs)
        return re.compile(rf" (?:{alt})(?: [a-z])? {re.escape(last)} ")
    return {
        "full": full_re(firsts),
        "full_alias": full_re(alias_firsts),
        "last": re.compile(rf" {re.escape(last)} ") if last else None,
        "first": re.compile(" (?:" + "|".join(re.escape(f) for f in firsts + alias_firsts) + ") ")
        if firsts + alias_firsts else None,
        "has_last": bool(last),
    }


def check_page(ntext, pats):
    """Returns dict of booleans for one page's normalized text and one owner."""
    r = {"full": False, "full_via_alias": False, "near": False, "last": False, "first": False}
    for key in ("full", "full_alias"):
        rx = pats[key]
        if rx is None:
            continue
        for m in rx.finditer(ntext):
            r["full"] = True
            if key == "full_alias":
                r["full_via_alias"] = True
            window = ntext[max(0, m.start() - NEAR): m.end() + NEAR]
            if OWNER_WORD_RE.search(window):
                r["near"] = True
        if r["full"] and key == "full":
            break
    if pats["last"] is not None and pats["last"].search(ntext):
        r["last"] = True
    if pats["first"] is not None and pats["first"].search(ntext):
        r["first"] = True
    return r


# ---- inputs -------------------------------------------------------------------------
def lname(s):
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def load():
    gt = json.load(open(GT_PATH, encoding="utf-8"))
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    leads = conn.execute("SELECT id, place_id, business_name, website FROM leads").fetchall()
    conn.close()
    by_name = {}
    for row in leads:
        by_name.setdefault(lname(row[2]), []).append(row)
    grades = {}
    with open(RESULTS_CSV, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            grades[int(r["id"])] = r["grade"]
    return gt, by_name, grades


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    gt, by_name, grades = load()
    funnel = Counter()
    targets = []   # (gt_id, lead_id, domain, owners)
    for row in gt:
        if row["kind"] != "owner" or not row["owners"]:
            continue
        funnel["truth owner"] += 1
        hits = by_name.get(lname(row["business_name"]), [])
        if len(hits) != 1:
            funnel["no unique lead match"] += 1
            continue
        lead_id, place_id, name, website = hits[0]
        if intake.skip_reason(name, place_id or ""):
            funnel["intake skip"] += 1
            continue
        funnel["after intake"] += 1
        website = (website or "").strip()
        if not website:
            funnel["no website"] += 1
            continue
        if _is_booking_platform(website):
            funnel["booking/social site"] += 1
            continue
        try:
            domain = pipeline_domain(website)
        except Exception:
            funnel["unparseable website"] += 1
            continue
        funnel["usable own site"] += 1
        targets.append((row["id"], lead_id, domain, row["owners"]))

    print("Funnel: " + ", ".join(f"{k}={v}" for k, v in funnel.items()), flush=True)
    print(f"timeout={TIMEOUT}s pause={PAUSE}s no retries; chrome first, plain fallback\n", flush=True)

    site_cache = {}    # domain -> list of (kind, ntext_visible, ntext_ld) and fetch stats
    streak = []
    per_lead = []
    stopped = None
    try:
        for gt_id, lead_id, domain, owners in targets:
            site_base = base_host(urlparse(domain).netloc)
            if domain not in site_cache:
                pages, stats = [], Counter()
                home_out, home_final, home_html, home_hdr = fetch_with_fallback(domain + "/", site_base)
                stats[f"home:{home_out}"] += 1
                stats[f"hdr:{home_hdr}"] += 1
                if home_html:
                    pages.append(("home", home_html))
                already = {"/"} | {p.lower() for p in ABOUT_PATHS}
                extra = candidate_links(home_html, home_final, site_base, already) if home_html else []
                todo = [("guess", domain + p) for p in ABOUT_PATHS] + [("link", u) for u in extra]
                for kind, url in todo:
                    out, final, html, hdr = fetch_with_fallback(url, site_base)
                    stats[f"{kind}:{out}"] += 1
                    stats[f"hdr:{hdr}"] += 1
                    if out == "429":
                        if site_base not in streak:
                            streak.append(site_base)
                        if len(streak) >= 3:
                            raise Stop("429 from 3 different sites in a row")
                    else:
                        streak = []
                    if html:
                        pages.append((kind, html))
                parsed = []
                for kind, html in pages:
                    vis, ld, _ = parse(html)
                    parsed.append((kind, norm(vis), norm(ld)))
                site_cache[domain] = {"pages": parsed, "stats": stats, "links_found": len(extra)}
            site = site_cache[domain]
            # best result across all owners (co-owners) and all pages
            best = {"full": False, "near": False, "via_alias": False, "ld_only": False,
                    "last": False, "first": False, "kinds": set(), "first_only_owner": False}
            for owner in owners:
                pats = name_patterns(owner)
                if not pats["has_last"]:
                    best["first_only_owner"] = True
                for kind, nvis, nld in site["pages"]:
                    rv = check_page(nvis, pats)
                    rl = check_page(nld, pats)
                    if rv["full"] or rl["full"]:
                        best["full"] = True
                        best["kinds"].add(kind)
                        if rv["near"] or rl["near"]:
                            best["near"] = True
                    best["last"] |= rv["last"] or rl["last"]
                    best["first"] |= rv["first"] or rl["first"]
                # alias-only and ld-only flags, computed per owner over all pages
                std_full = any(pats["full"] is not None and (pats["full"].search(v) or pats["full"].search(l))
                               for _, v, l in site["pages"])
                alias_full = any(pats["full_alias"] is not None
                                 and (pats["full_alias"].search(v) or pats["full_alias"].search(l))
                                 for _, v, l in site["pages"])
                vis_full = any(
                    (pats["full"] is not None and pats["full"].search(v))
                    or (pats["full_alias"] is not None and pats["full_alias"].search(v))
                    for _, v, _l in site["pages"])
                if alias_full and not std_full:
                    best["via_alias"] = True
                if (std_full or alias_full) and not vis_full:
                    best["ld_only"] = True
            reachable = bool(site["pages"])
            if not reachable:
                cat = "unreachable"
            elif best["full"]:
                cat = "full name"
            elif best["last"]:
                cat = "last name only"
            elif best["first"]:
                cat = "first name only"
            else:
                cat = "nothing"
            grade = grades.get(gt_id, "not in run")
            per_lead.append({"gt_id": gt_id, "lead_id": lead_id, "cat": cat, "near": best["near"],
                             "kinds": best["kinds"], "via_alias": best["via_alias"], "ld_only": best["ld_only"],
                             "first_only_owner": best["first_only_owner"], "grade": grade,
                             "n_pages": len(site["pages"]), "both_sep": best["last"] and best["first"]})
            st = site["stats"]
            print(f"lead={lead_id:<4} gt={gt_id:<3} pages_ok={len(site['pages']):<2} links_found={site['links_found']} "
                  f"home={next((k[5:] for k in st if k.startswith('home:')), '-'):<16} "
                  f"plain_fallbacks={st['hdr:plain']:<2} -> {cat:<16} "
                  f"near_owner_word={'y' if best['near'] else 'n'} hit_on={','.join(sorted(best['kinds'])) or '-'} "
                  f"pipeline_grade={grade}", flush=True)
    except Stop as e:
        stopped = str(e)
        print(f"\nSTOPPED: {stopped}")

    report(funnel, per_lead, site_cache, stopped)


def report(funnel, per_lead, site_cache, stopped):
    print("\n" + "=" * 90)
    print("SUMMARY" + (" (PARTIAL: run stopped)" if stopped else ""))
    print("=" * 90)
    reach = [r for r in per_lead if r["cat"] != "unreachable"]
    print(f"Dev leads with a truth owner: {funnel['truth owner']}  "
          f"(intake skip {funnel['intake skip']}, no unique lead match {funnel['no unique lead match']})")
    print(f"  of those after intake: {funnel['after intake']}; with a usable own site: {funnel['usable own site']} "
          f"(no website {funnel['no website']}, booking/social {funnel['booking/social site']})")
    print(f"  usable sites checked: {len(per_lead)}; any page reachable: {len(reach)}")
    cats = Counter(r["cat"] for r in reach)
    print("\nOf the reachable ones:")
    for c in ("full name", "last name only", "first name only", "nothing"):
        print(f"  {c:<16} {cats[c]}")
    full = [r for r in reach if r["cat"] == "full name"]
    print(f"\nFull-name hits: {len(full)}; near owner/founder/ceo/'owned by' (80 chars): "
          f"{sum(r['near'] for r in full)}; not near: {sum(not r['near'] for r in full)}")
    print(f"  full-name hits that needed a goes-by alias as the first name: {sum(r['via_alias'] for r in full)}")
    print(f"  full-name hits found only in ld+json (not in visible text): {sum(r['ld_only'] for r in full)}")
    kinds = Counter(k for r in full for k in r["kinds"])
    print(f"  page kinds carrying a full-name hit (a lead can count in several): "
          + ", ".join(f"{k}={v}" for k, v in sorted(kinds.items())))
    only_link = sum(1 for r in full if r["kinds"] == {"link"})
    only_home = sum(1 for r in full if r["kinds"] == {"home"})
    print(f"  hits only on a homepage-link page: {only_link}; only on the homepage: {only_home}")
    lo = [r for r in reach if r["cat"] == "last name only"]
    print(f"Last-name-only rows where the first name also appears elsewhere (not adjacent): "
          f"{sum(r['both_sep'] for r in lo)} of {len(lo)}")
    print(f"Truth owners with no last name on file (full-name check impossible): "
          f"{sum(r['first_only_owner'] for r in per_lead)}")

    print(f"\nCross-check vs delivered grade in {os.path.basename(RESULTS_CSV)} (Gemini off):")
    g = Counter(r["grade"] for r in full)
    for k in ("full", "partial", "wrong", "blank", "not in run"):
        print(f"  full-name hits graded {k:<11} {g[k]}")
    new_strict = [r for r in full if r["grade"] != "full"]
    new_len = [r for r in full if r["grade"] not in ("full", "partial")]
    print(f"  already right strict (full): {g['full']}; already right lenient (full+partial): "
          f"{g['full'] + g['partial']}")
    print(f"  NEW vs strict: {len(new_strict)} (lead ids {sorted(r['lead_id'] for r in new_strict)}); "
          f"of those near an owner word: {sum(r['near'] for r in new_strict)}")
    print(f"  NEW vs lenient: {len(new_len)} (lead ids {sorted(r['lead_id'] for r in new_len)}); "
          f"of those near an owner word: {sum(r['near'] for r in new_len)}")
    print(f"  wrong-graded leads whose site carries the truth full name: "
          f"{sorted(r['lead_id'] for r in full if r['grade'] == 'wrong')}")

    hosts = Counter(REQUEST_LOG)
    lead_hosts = {base_host(urlparse(d).netloc) for d in site_cache}
    off = [h for h in hosts if not any(h == b or h.endswith("." + b) for b in lead_hosts)]
    print(f"\nNetwork: {len(REQUEST_LOG)} requests to {len(hosts)} hosts; hosts outside the lead sites: {len(off)}")
    if stopped:
        print(f"Run stopped early: {stopped}")


if __name__ == "__main__":
    main()
