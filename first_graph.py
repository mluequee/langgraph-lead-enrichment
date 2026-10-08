from typing import TypedDict, Literal, Annotated, Optional
from langgraph.graph import END, StateGraph
from langchain_anthropic import ChatAnthropic
from dotenv import load_dotenv
from pydantic import BaseModel
from dataclasses import dataclass
import difflib
import os
import re
import requests
import operator
import json
import logging
import sqlite3
import logging as py_logging
import csv
import sys
from urllib.parse import urlparse
if sys.platform == 'win32':
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')
from datetime import datetime

import intake
import evidence
import gemini_path
import reconcile
import time
from concurrent.futures import ThreadPoolExecutor
import export
import setup_database

py_logging.getLogger("anthropic").setLevel(py_logging.WARNING)
py_logging.getLogger("langchain").setLevel(py_logging.WARNING)
py_logging.getLogger("langchain_anthropic").setLevel(py_logging.WARNING)
for _noisy in ("httpx", "httpcore", "urllib3"):
    py_logging.getLogger(_noisy).setLevel(py_logging.WARNING)
load_dotenv(r"C:\Users\marvi\.secrets\langgraph.env")

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "leads.db")

# Stage C2: Gemini is the second path. GEMINI_ENABLED False skips it (the Brave answer is then delivered unchanged).
GEMINI_ENABLED = True
GEMINI_MODEL = gemini_path.MODEL
# Leads processed at the same time. Each lead fires up to 5 Brave searches at once, so N workers put at most
# 5*N requests in flight; Brave allows 50 per second (x-ratelimit-policy 50;w=1).
DEFAULT_WORKERS = 4

@dataclass
class Lead:
    """Represents a lead from the database."""
    id: int
    business_name: str
    website: str
    address: str = ""
    phone: str = ""
    city: str = ""
    state: str = ""
    place_id: str = ""

@dataclass
class LeadCost:
    """Tracks cost metrics for a single lead."""
    lead_id: int
    business_name: str
    haiku_input_tokens: int = 0
    haiku_output_tokens: int = 0
    sonnet_input_tokens: int = 0
    sonnet_output_tokens: int = 0
    brave_calls: int = 0
    haiku_errors: list = None
    sonnet_errors: list = None
    brave_errors: list = None
    brave_429: int = 0                  # recording only: Brave 429 responses seen
    brave_429_recovered: int = 0        # of those, the retry came back without a 429
    
    def __post_init__(self):
        if self.brave_errors is None:
            self.brave_errors = []
        if self.haiku_errors is None:
            self.haiku_errors = []
        if self.sonnet_errors is None:
            self.sonnet_errors = []
    
    @property
    def total_tokens(self) -> int:
        return self.haiku_input_tokens + self.haiku_output_tokens + \
               self.sonnet_input_tokens + self.sonnet_output_tokens
    
    def estimated_cost_usd(self) -> float:
        """Estimate cost at current Claude pricing (as of 2026-08)."""
        # Haiku: $0.80 / 1M input, $4.00 / 1M output
        haiku_cost = (self.haiku_input_tokens * 0.80 + self.haiku_output_tokens * 4.00) / 1_000_000
        # Sonnet: $3.00 / 1M input, $15.00 / 1M output
        sonnet_cost = (self.sonnet_input_tokens * 3.00 + self.sonnet_output_tokens * 15.00) / 1_000_000
        # Brave: ~$0.10 per request (flat)
        brave_cost = self.brave_calls * 0.005
        return haiku_cost + sonnet_cost + brave_cost

class LeadDatabase:
    """Database abstraction layer — SQLite now, Postgres-ready by design."""

    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        if os.path.exists(db_path):
            added = setup_database.migrate_results(db_path)     # stage A columns; no-op once present
            if added:
                logging.getLogger(__name__).info(f"Migrated results table: added {', '.join(added)}")

    def get_all_leads(self) -> list[Lead]:
        """Fetch all unprocessed leads."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        cursor.execute("SELECT id, place_id, business_name, website, address, phone, city, state FROM leads")
        rows = cursor.fetchall()
        conn.close()

        return [
            Lead(
                id=row['id'],
                business_name=row['business_name'],
                website=row['website'] or "",
                address=row['address'] or "",
                phone=row['phone'] or "",
                city=row['city'] or "",
                state=row['state'] or "",
                place_id=row['place_id'] or "",
            )
            for row in rows
        ]

    def write_result(self, lead_id: int, result: dict) -> None:
        """Write extraction results for a lead. FIXED: corrected typos from Phase 6."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        cursor.execute("""
            INSERT INTO results (
                lead_id, decision_maker_first_name, decision_maker_last_name,
                title, instagram, tiktok, facebook, linkedin, confidence, 
                why_this_person, about_page_text,
                goes_by_name, evidence_url, evidence_domain, evidence_source, evidence_url_kind,
                other_evidence_urls, source_tier, delivered_confidence, status, error_detail,
                reconcile_case, review_candidates, brave_first_name, brave_last_name,
                gemini_first_name, gemini_last_name, gemini_goes_by_name, gemini_title, gemini_confidence,
                gemini_status, gemini_error, gemini_evidence_url, gemini_seconds, gemini_cost_usd
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            lead_id,
            result.get('decision_maker_first_name'),  # FIXED: was 'decision_make_first_name'
            result.get('decision_maker_last_name'),   # FIXED: was 'decison_maker_last_name'
            result.get('title'),
            result.get('instagram'),
            result.get('tiktok'),
            result.get('facebook'),
            result.get('linkedin'),
            result.get('confidence'),
            result.get('why_this_person'),
            result.get('about_page_text', ''),
            result.get('goes_by_name'),
            result.get('evidence_url'),
            result.get('evidence_domain'),
            result.get('evidence_source'),
            result.get('evidence_url_kind'),
            result.get('other_evidence_urls'),
            result.get('source_tier'),
            result.get('delivered_confidence'),
            result.get('status'),
            result.get('error_detail'),
            result.get('reconcile_case'),
            result.get('review_candidates'),
            (result.get('brave_answer') or {}).get('first'),
            (result.get('brave_answer') or {}).get('last'),
            (result.get('gemini') or {}).get('first'),
            (result.get('gemini') or {}).get('last'),
            (result.get('gemini') or {}).get('goes_by'),
            (result.get('gemini') or {}).get('title'),
            (result.get('gemini') or {}).get('confidence'),
            (result.get('gemini') or {}).get('status'),
            (result.get('gemini') or {}).get('error'),
            ((result.get('gemini') or {}).get('evidence') or {}).get('evidence_url'),
            (result.get('gemini') or {}).get('seconds'),
            (result.get('gemini') or {}).get('cost_usd'),
        ))

        conn.commit()
        conn.close()

    def get_results(self) -> list[dict]:
        """Fetch all processed results."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row 
        cursor = conn.cursor()

        cursor.execute("""
            SELECT
                l.business_name,
                r.decision_maker_first_name,
                r.decision_maker_last_name,
                r.confidence
            FROM results r
            JOIN leads l ON r.lead_id = l.id
            ORDER BY r.processed_at DESC
        """)

        rows = cursor.fetchall()
        conn.close()

        return [dict(row) for row in rows]


logging.basicConfig(
    level=logging.DEBUG,
    format='%(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('langgraph_debug.log', encoding='utf-8')
    ]
)
logger = logging.getLogger(__name__)

console_handler = logging.StreamHandler()
console_handler.setLevel(logging.INFO)
formatter = logging.Formatter('%(message)s')
console_handler.setFormatter(formatter)
logger.addHandler(console_handler)

extract_model = ChatAnthropic(model="claude-haiku-4-5-20251001")
reasoning_model = ChatAnthropic(model="claude-sonnet-5")
brave_key = os.getenv("BRAVE_API_KEY")

class OwnerResult(BaseModel):
    decision_maker_first_name: str
    decision_maker_last_name: str
    title: str
    linkedin: str
    instagram: str
    tiktok: str
    facebook: str
    confidence: Literal["high", "medium", "low"]
    why_this_person: str
    evidence_snippet: str
    evidence_url: str

class VerificationResult(BaseModel):
    """Sonnet's verification of Haiku's extraction."""
    verified_confidence: Literal["high", "medium", "low"]
    verified_first_name: str
    verified_last_name: str   
    alternative_first_name: str  
    alternative_last_name: str
    verification_reasoning: str

class LeadState(TypedDict):
    lead_id: int
    business_name: str
    website: str
    address: str
    phone: str
    city: str
    state: str
    fallback_website: str
    queries: list[str]
    search_results: Annotated[list[dict], operator.add]
    combined_source_text: str
    # Stage A: None is the one "nothing found" value for every name, title and social field.
    decision_maker_first_name: Optional[str]
    decision_maker_last_name: Optional[str]
    goes_by_name: Optional[str]
    title: Optional[str]
    linkedin: Optional[str]
    instagram: Optional[str]
    tiktok: Optional[str]
    facebook: Optional[str]
    confidence: Literal["high", "medium", "low"]
    why_this_person: str
    verified_confidence: Literal["high", "medium", "low"]
    alternative_first_name: Optional[str]
    alternative_last_name: Optional[str]
    verification_reasoning: str
    about_page_text: str
    about_page_url: Optional[str]
    # Stage A: record-keeping only. Nothing reads source_tier for confidence, routing or delivery.
    evidence_url: Optional[str]
    evidence_domain: Optional[str]
    evidence_source: Optional[str]
    evidence_url_kind: Optional[str]
    other_evidence_urls: Optional[str]
    source_tier: Optional[str]
    delivered_confidence: Optional[str]
    status: str
    error_detail: Optional[str]
    # Stage C2: the Gemini path's own answer, the Brave answer as it stood before reconcile, and the outcome
    gemini: Optional[dict]
    brave_answer: Optional[dict]
    reconcile_case: Optional[str]
    review_candidates: Optional[str]
    # Phase 7: Cost tracking
    cost: LeadCost

structured_model = extract_model.with_structured_output(OwnerResult, include_raw=True)

# Phase 10: one encoding for "no name" everywhere (was <UNKNOWN>, <E>, <ERROR>, UNKNOWN, empty).
NO_NAME = evidence.NO_NAME

def _is_no_name(s: str)-> bool:
    return evidence.is_none_like(s)

def _clean_name(s: str):
    """Stage A: the one 'nothing found' value is None (was <UNKNOWN>, <ERROR>, '' and the
    model's own 'Unknown'/'N/A'). Stops 'Greg UNKNOWN' leaking into results."""
    return evidence.none_if_empty(s)

def _prompt_name(state, name):
    """How a missing name is shown INSIDE a prompt. Byte-identical to before stage A:
    '<UNKNOWN>' normally, '<ERROR>' when the Haiku extraction itself raised."""
    if not _is_no_name(name):
        return name
    return "<ERROR>" if state["cost"].haiku_errors else "<UNKNOWN>"
# Phase 8: Identity anchor - location gate + about-page targeting

US_STATES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California",
    "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware", "FL": "Florida", "GA": "Georgia",
    "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa",
    "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland",
    "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi", "MO": "Missouri",
    "MT": "Montana", "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire", "NJ": "New Jersey",
    "NM": "New Mexico", "NY": "New York", "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio",
    "OK": "Oklahoma", "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina",
    "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont",
    "VA": "Virginia", "WA": "Washington", "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming",
    "DC": "District of Columbia",
}

# Booksy/thecut/Square/Vagaro/StyleSeat: booking platforms, never the shop's own domain.
BOOKING_PLATFORM_DOMAINS = ("booksy.com", "thecut.co", "squareup.com", "square.site", "vagaro.com", "styleseat.com")
# Phase 10: Pages that are never the shop's OWN site. About-page fetch on these would read
# the platform's corporate page (linktr.ee/about, feacebook.com/about), not the shop's.
NOT_OWN_SITE_DOMAINS = BOOKING_PLATFORM_DOMAINS + (
    "facebook.com", "instagram.com", "linktr.ee", "tiktok.com", "linkedin.com", "yelp.com", "google.com" 
)
# Phase 10: retrieval weight = who survives the top-8 cut. NOT trust (authority comes later).
# Starting weights, not tuned: BBB outranks a bare Instagram URL (60) but not a phone/address anchor (100).
RETRIEVAL_BOOST = {"bbb.org": 70, "booksy.com": 35, "yelp.com": 35}
# Phase 10: inline Goggle. Re-ranks inside Brave, so it changes WHICH results come back.
# Newline-delimited rules, strength 1-10 Starting weight, not tuned.
OWNER_SOURCES_GOGGLE = "\n".join(
    f"$boost=4,site={d}" for d in ("bbb.org", "linkedin.com", "booksy.com", "yelp.com")
)

def _phone_digits(phone: str) -> str:
    """Last 10 digits of a phone number, for comparing across formats."""
    digits = re.sub(r"\D", "", phone or "")
    return digits[-10:] if len(digits) >= 10 else digits


def _street_fragment(address: str) -> str:
    """Street number + first word of the street name, e.g. '608 Park' from
    '608 Park St, Belmont, NC 28012, USA'. Used as a deterministic anchor."""
    if not address:
        return ""
    street = address.split(",")[0].strip()
    tokens = street.split()
    if len(tokens) < 2:
        return ""
    return f"{tokens[0]} {tokens[1]}"


def _is_anchor_result(title: str, description: str, url: str, phone: str, address: str) -> bool:
    """A result is an anchor when it contains the lead's own phone number or
    street address - the strongest disambiguators available."""
    text = f"{title} {description} {url}"
    phone_digits = _phone_digits(phone)
    if phone_digits and phone_digits in re.sub(r"\D", "", text):
        return True
    fragment = _street_fragment(address)
    if fragment and fragment.lower() in text.lower():
        return True
    return False


def _names_other_state(title: str, description: str, lead_state: str) -> bool:
    """Deterministic location gate, run before any LLM sees a result. Discards
    a result that confidently names a DIFFERENT state than the lead's own -
    never on a bare 2-letter code (too many read as ordinary words), only on
    a full state name or an address-shaped ', XX 12345' citation."""
    if not lead_state:
        return False
    text = f"{title} {description}"
    lead_state = lead_state.upper()

    for abbr, full in US_STATES.items():
        if abbr == lead_state:
            continue
        if re.search(rf"\b{re.escape(full)}\b", text, re.IGNORECASE):
            return True

    for m in re.finditer(r",\s*([A-Z]{2})\s+\d{5}", text):
        if m.group(1) != lead_state:
            return True

    return False


def _names_other_city(title: str, description: str, lead_city: str, lead_state: str) -> bool:
    """Catches the case _names_other_state can't: a same-named shop in a
    DIFFERENT city but the SAME state. Address-shaped only (', City, ST'),
    never a bare city-name word match - city names are too ambiguous
    (repeat across states, double as first names) to discard on alone."""
    if not lead_city:
        return False
    text = f"{title} {description}"
    lead_city_norm = lead_city.strip().lower()

    for m in re.finditer(r",\s*([A-Za-z][A-Za-z .'-]*?),\s*([A-Z]{2})\b", text):
        city, st = m.group(1).strip().lower(), m.group(2)
        if city and city != lead_city_norm and (st == lead_state.upper() if lead_state else True):
            return True
    return False


def _is_booking_platform(url: str) -> bool:
    if not url:
        return False
    netloc = urlparse(url).netloc.lower()
    return any(d in netloc for d in NOT_OWN_SITE_DOMAINS)


def _resolve_fallback_website(search_results: list[dict], phone: str) -> str:
    """Brave's `locations` block is NOT a lookup of this shop - a 'Charlotte NC'
    query can return an unrelated shop first. Never take locations[0]; accept
    an entry only when its phone matches the lead's own phone."""
    phone_digits = _phone_digits(phone)
    if not phone_digits:
        return ""
    for search_set in search_results:
        for loc in search_set.get("locations", []):
            if _phone_digits(loc.get("phone", "")) == phone_digits:
                return loc.get("url", "")
    return ""

def _bbb_url_names_other_place(url: str, lead_city: str, lead_state: str) -> bool:
    """BBB profile URLs embed the place: /us/nc/black-mountain/profile/...
    Snippets often omit the address, so read the place from the URL."""
    m = re.search(r"bbb\.org/us/([a-z]{2})/([a-z0-9-]+)/", (url or "").lower())
    if not m:
        return False
    st, city_slug = m.group(1), m.group(2)
    if lead_state and st != lead_state.lower():
        return True
    lead_slug = re.sub(r"[^a-z0-9]+", "-", (lead_city or "").lower()).strip("-")
    return bool (lead_slug) and city_slug != lead_slug

def _booksy_url_names_other_place(url: str, lead_city: str) -> bool:
    """Booksy URLs end in the place: .../1254405_name_barber-shop_26689_gastonia.
    Same idea as the BBB gate:snippets rarely say where, the URL does."""
    u = (url or "").lower()
    if "booksy.com" not in u or not lead_city:
        return False
    m = re.search(r"_\d+_([a-z][a-z0-9-]*)/?(?:[?#].*)?$", u)
    if not m:
        return False
    lead_slug = re.sub(r"[^a-z0-9]+", "-", lead_city.lower()).strip("-")
    return m.group(1) != lead_slug

# Phase 10: relevance gate. A result must be about THIS shop to reach the model.
GENERIC_NAME_WORDS = {
    "the", "and", "of", "inc", "llc", "co", "company", "corp",
    "barber", "barbers", "barbering", "barbershop", "barbershops", "shop", "shoppe",
    "salon", "lounge", "studio", "room", "cuts", "cut", "haircuts", "grooming",
    "center", "men", "mens", "unisex", "historic",
    "charlotte", "nc", "blvd", "st", "university", "city",
}
_NAME_CUT = r"\s[-|@]\s|/|\("   # name junk: ' - city', ' @ x', URLs, '(notes)'

def _norm_words(s: str) -> list[str]:
    s = (s or "").lower().replace("'", "").replace("\u2019", "")
    return re.sub(r"[^a-z0-9]+", " ", s).split()

def _brand_tokens(business_name: str) -> list[str]:
    """The distinctive words of a shop name (max 2), generic words dropped."""
    cut = re.split(_NAME_CUT, business_name or "")[0]
    toks = [t for t in _norm_words(cut) if t not in GENERIC_NAME_WORDS and (len(t) > 1 or t.isdigit())]
    return toks[:2]

BBB_OK_CATEGORY_HINTS = ("barber", "hair", "beauty", "salon", "groom")

def _bbb_wrong_industry(url: str) -> bool:
    """BBB profile URLs carry the industry: .../profile/barber/..., .../profile/auto-repair/...
    A same-named business in another industry is not this shop."""
    m = re.search(r"bbb\.org/us/[a-z]{2}/[a-z0-9-]+/profile/([a-z0-9-]+)/", (url or "").lower())
    if not m:
        return False
    return not any(h in m.group(1) for h in BBB_OK_CATEGORY_HINTS)

def _has_token(tok: str, words: list[str], squash: str = "") -> bool:
    if tok in words:
        return True
    # run-together spellings: '@theelectbarbershop', 'Wil-Mar' vs 'Wilmar'
    if len(tok) >= 6 and tok in squash:
        return True
    return len(tok) >= 6 and bool(difflib.get_close_matches(tok, words, n=1, cutoff=0.85))

def _result_is_about_shop(title: str, description: str, url: str, business_name: str) -> bool:
    if _bbb_wrong_industry(url):
        return False
    words = _norm_words(f"{title} {description} {url}")
    squash = "".join(_norm_words(f"{title} {url}"))
    brand = _brand_tokens(business_name)
    if brand:
        return all(_has_token(t, words, squash) for t in brand)
    cut = re.split(_NAME_CUT, business_name or "")[0]
    phrase = " ".join(w for w in _norm_words(cut) if w != "the")
    return bool(phrase) and f" {phrase} " in f" {' '.join(words)} "


def prep_lead(state: LeadState) -> dict:
    business_name = state.get("business_name", "Unknown")
    logger.info(f"Processing lead: {business_name}")
    # Initialize cost tracking for this lead
    lead_id = state.get("lead_id", 0)
    cost = LeadCost(lead_id=lead_id, business_name=business_name)

    return {
        "business_name": business_name,
        "lead_id": lead_id,
        "website": state.get("website", ""),
        "address": state.get("address", ""),
        "phone": state.get("phone", ""),
        "city": state.get("city", ""),
        "state": state.get("state", ""),
        "fallback_website": "",
        "queries": [],
        "search_results": [],
        "combined_source_text": "",
        "decision_maker_first_name": None,
        "decision_maker_last_name": None,
        "goes_by_name": None,
        "title": None,
        "linkedin": None,
        "instagram": None,
        "tiktok": None,
        "facebook": None,
        "confidence": "low",
        "why_this_person": "",
        "verified_confidence": "low",
        "alternative_first_name": None,
        "alternative_last_name": None,
        "verification_reasoning": "",
        "about_page_text": "",
        "about_page_url": None,
        **evidence.empty_evidence(),
        "delivered_confidence": None,
        "status": "ok",
        "error_detail": None,
        "gemini": None,
        "brave_answer": None,
        "reconcile_case": None,
        "review_candidates": None,
        "cost": cost
    }

def build_queries(state: LeadState) -> dict:
    """Location-anchored query construction. City/state come from the lead's own
    address, never hardcoded (25 of 107 leads are outside Charlotte)."""
    business_name = state["business_name"]
    location = " ".join(p for p in (state.get("city", ""), state.get("state", "")) if p)
    # No exact-phrase quotes: real-world sources (news, directories) refer to a
    # shop by a shortened/differently-punctuated name, and quoting excludes them.
    anchor = f"{business_name} {location}".strip() if location else business_name

    queries = [
        f"{anchor} owner",
        f"{anchor} site:linkedin.com owner",
        f"{anchor} site:bbb.org",
        f"{anchor} site:booksy.com",
        f"{anchor} instagram"
    ]
    logger.debug(f"Built {len(queries)} for '{business_name}':\n" + "\n".join(f" Q{i+1}: {q}" for i, q in enumerate(queries)))
    return {"queries": queries}

def search_q1(state: LeadState) -> dict:
    return _brave_search(state, query_index=0)

def search_q2(state: LeadState) -> dict:
    return _brave_search(state, query_index=1)

def search_q3(state: LeadState) -> dict:
    return _brave_search(state, query_index=2)

def search_q4(state: LeadState) -> dict:
    return _brave_search(state, query_index=3)

def search_q5(state: LeadState) -> dict:
    return _brave_search(state, query_index=4)

def _brave_search(state: LeadState, query_index: int) -> dict:
    """Execute one Brave search and return results as a list (for reducer).
    Phase 7: Track Brave calls in cost object."""
    url = "https://api.search.brave.com/res/v1/web/search"
    headers = {
        "X-Subscription-Token": brave_key,
        "Accept": "application/json"
    }

    query = state["queries"][query_index]
    params = {"q": query, "goggles": OWNER_SOURCES_GOGGLE}

    logger.debug(f"Searching Q{query_index + 1}: '{query}'...")

    cost = state["cost"]
    cost.brave_calls += 1

    try:
        response = requests.get(url, headers=headers, params=params, timeout=10)
        if response.status_code == 429:           # rate limited: wait the stated time (max 5 s), try once more
            try:
                wait = float((response.headers.get('Retry-After') or response.headers.get('X-RateLimit-Reset') or '1')
                             .split(',')[0])
            except ValueError:
                wait = 1.0
            cost.brave_429 += 1
            logger.warning(f"Q{query_index + 1} rate limited (429), retrying once in {min(wait, 5):.0f}s")
            time.sleep(min(max(wait, 0.5), 5))
            cost.brave_calls += 1
            response = requests.get(url, headers=headers, params=params, timeout=10)
            if response.status_code != 429:
                cost.brave_429_recovered += 1
        response.raise_for_status()
        data = response.json()
        results = data.get("web", {}).get("results", [])
        # Phase 8: Brave's `locations` block, when present, is a candidate fallback
        # website for leads with no website on file - NOT a lookup of this shop.
        # combine_and_score only accepts an entry whose phone matches the lead's.
        locations = [
            {
                "url": loc.get("url", ""),
                "phone": (loc.get("contact") or {}).get("telephone", ""),
            }
            for loc in data.get("locations", {}).get("results", [])
        ]
        logger.debug(f"Q{query_index + 1} returned {len(results)} results, {len(locations)} location(s)")
    except requests.exceptions.Timeout:
        logger.warning(f"Q{query_index + 1} timeout: {query}")
        cost.brave_errors.append(f"Q{query_index + 1} timeout")
        cost.brave_calls += 1  # Count the retry
        results = []
        locations = []
    except requests.exceptions.HTTPError as e:
        logger.warning(f"Q{query_index + 1} HTTP error ({response.status_code}): {e}")
        cost.brave_errors.append(f"Q{query_index + 1} HTTP {response.status_code}")
        results = []
        locations = []
    except Exception as e:
        logger.warning(f"Q{query_index + 1} failed: {type(e).__name__}: {e}")
        cost.brave_errors.append(f"Q{query_index + 1} {type(e).__name__}")
        results = []
        locations = []

    return {
        "search_results": [
            {
                "query": query,
                "query_index": query_index,
                "results": results,
                "locations": locations,
            }
        ],
    }

def combine_and_score(state: LeadState) -> dict:
    """Score, dedupe, rank and format results from all 4 queries.
    Phase 8: deterministic location gate runs before any LLM sees a result -
    discards results naming another state, marks phone/address anchor results."""
    business_name = state["business_name"].lower()
    lead_city = state.get("city", "")
    lead_state = state.get("state", "")
    lead_phone = state.get("phone", "")
    lead_address = state.get("address", "")

    all_results_with_scores = []
    discarded_wrong_location = 0
    discarded_irrelevant = 0

    for search_set in state["search_results"]:
        results = search_set.get("results", [])

        for result in results:
            url = result.get("url", "").lower()
            title_raw = result.get("title", "")
            description_raw = result.get("description", "")
            title = title_raw.lower()
            description = description_raw.lower()

            # Skip filtered URLs
            if "authwall" in url or "linkedin-signup" in url:
                continue

            if _names_other_state(title_raw, description_raw, lead_state) or \
               _names_other_city(title_raw, description_raw, lead_city, lead_state) or \
               _bbb_url_names_other_place(url, lead_city, lead_state) or \
               _booksy_url_names_other_place(url, lead_city):
                discarded_wrong_location += 1
                continue

            is_anchor = _is_anchor_result(title_raw, description_raw, url, lead_phone, lead_address)
            # Phase 10: unless it carries the lead's own phone/address, a result
            # must be about THIS shop to reach the model.
            if not is_anchor and not _result_is_about_shop(title_raw, description_raw, url, state["business_name"]):
                discarded_irrelevant += 1
                continue

            score = _score_result(title, description, url, business_name, is_anchor)

            all_results_with_scores.append({
                "url": result.get("url", ""),
                "title": result.get("title", ""),
                "description": result.get("description", ""),
                "score": score
            })

    if discarded_wrong_location:
        logger.debug(f"Location gate discarded {discarded_wrong_location} result(s) naming another state")
    if discarded_irrelevant:
        logger.debug(f"Relevance gate discarded {discarded_irrelevant} result(s) not about this shop")

    # Dedupe by URL (keep first occurrence)
    seen_urls = set()
    deduped = []
    for item in all_results_with_scores:
        if item["url"] not in seen_urls:
            seen_urls.add(item["url"])
            deduped.append(item)

    # Sort by score (descending) and take top 8
    deduped.sort(key=lambda x: x["score"], reverse=True)
    top_results = deduped[:8]
    if not top_results:
        logger.info(f"-> No usable results survived the gates for {state['business_name']}")

    # Combine into one text string (40k char cap)
    combined_text = "\n---\n".join(
        f"[{i+1}] {r['title']}\n{r['description']}\nURL: {r['url']}"
        for i, r in enumerate(top_results)
    )

    if len(combined_text) > 40_000:
        combined_text = combined_text[:40_000]

    logger.debug(f"Combined & scored: {len(deduped)} deduped, top 8 ranked, {len(combined_text)} chars")

    fallback_website = _resolve_fallback_website(state["search_results"], lead_phone)

    return {
        "combined_source_text": combined_text,
        "fallback_website": fallback_website,
    }

def _score_result(title: str, description: str, url: str, business_name: str, is_anchor: bool = False) -> int:
    """Score a single search result."""
    score = 0

    # Phase 8: phone/address anchor is the strongest disambiguator - dominates ranking.
    if is_anchor:
        score += 100

    # Platform signals
    if "instagram" in url:
        score += 60
    if "tiktok" in url:
        score += 55
    if "facebook" in url:
        score += 40
    if "linkedin" in url:
        score += 45
    # Phase 10: boost the sources where owners were actually found (hand audit)
    for domain, boost in RETRIEVAL_BOOST.items():
        if domain in url:
            score += boost

    # Content signals
    if "owner" in title or "owner" in description:
        score += 50
    if "founder" in title or "founder" in description:
        score += 60
    if "barber" in title or "barber" in description:
        score += 20

    # Business name in title
    if business_name in title:
        score += 30

    return score

def _source_block_for_url(url: str, combined_text: str) -> str:
    """Return the [n] title/description/URL block in combined_text whose URL line matches `url` exactly, or '' if no such block exist."""
    if not url:
        return ""
    norm_url = url.strip().lower()
    for block in combined_text.split("\n---\n"):
        for line in block.splitlines():
            if line.lower().startswith("url:") and line [4:].strip().lower() == norm_url:
                return block
    return ""

def _canonical_url(url: str, combined_text: str):
    """The URL exactly as the matching source block prints it (the citation gate compares lowercased)."""
    block = _source_block_for_url(url, combined_text)
    for line in block.splitlines():
        if line.lower().startswith("url:"):
            return line[4:].strip()
    return None

def _same_person(state, first, last) -> bool:
    """Is (first, last) the person already in state? Decides whether title/socials still apply."""
    norm = lambda s: (s or "").strip().lower()
    return (norm(first), norm(last)) == (norm(state.get("decision_maker_first_name")),
                                         norm(state.get("decision_maker_last_name")))

def _evidence_supports_name(first: str, last: str, snippet: str, url: str, combined_text: str, business_name: str) -> tuple[bool, str]:
    """Phase 9: an extraction is only as good as its citation. The model's own
    transcription of the snippet is untrusted - it doesn't reliably quote
    verbatim even when it's right. Instead, pull the real source block for
    the cited URL and check the name against THAT.
    Returns (passed, reason) so failures are diagnosable"""
    if not snippet or not url:
        return False, "missing snippet or url"
    block = _source_block_for_url(url, combined_text)
    if not block:
        return False, "url not one of the cited sources"
    norm = lambda s: re.sub(r"\s+", " ", s or " ").strip().lower()
    name_toks = [t for t in (first, last) if not
                 _is_no_name(t)]
    missing = [t for t in name_toks if norm(t) not in norm(block)]
    if missing:
        return False, f"cited source doesn't contain: {', '.join(missing)}"
    if _is_name_echo_of_business(first, last, business_name):
        corroboration = min(_independent_corroboration_count(t, combined_text) for t in name_toks) if name_toks else 0
        if corroboration < 2:
            return False, f"name echoes business name, only {corroboration} source(s) mention it"
    return True, "ok"

def _about_evidence_supports_name(first: str, last: str, snippet: str, about_text: str) -> tuple[bool, str]:
    """Same discipline as _evidence_supports_name, but for the About-page pass:
    one page, one source - so there's no per-URL block to isolate. Check the name against the actual_text directly."""
    if not snippet:
        return False, "missing snippet"
    norm = lambda s: re.sub(r"\s+", " ", s or " ").strip().lower()
    name_toks = [t for t in (first, last) if not
                 _is_no_name(t)]
    missing = [t for t in name_toks if norm(t) not in norm(about_text)]
    if missing:
        return False, f"about_text doesn't contain: {', '.join(missing)}"
    return True, "ok"

SHOP_STOPWORDS = {"barber", "barbershop", "barbers", "shop", "shoppe", "salon", "cuts", "cut",
                  "hair", "the", "co", "company", "llc", "studio", "lounge", "parlor", "parlour",
                  "grooming", "kutz", "kuts", "cutz", "blendz", "blends", "room", "house", "club",
                  "kingdom", "academy", "group"}

def _biz_name_tokens(business_name: str) -> set:
    """Distinctive words in a business name - generic shop words filtered out
    so 'Barber Kons the Butcher' -> Kons doesn't trip on 'barber'."""
    toks = re.findall(r"[a-z']+", business_name.lower())
    return {t for t in toks if t not in SHOP_STOPWORDS and len(t) > 2}

def _is_name_echo_of_business(first: str, last: str, business_name: str) -> bool:
    """True when a name token matches a distinctive business-name word - the
    fabrication signature form the 09-21 audit: 'The Hair Cave' -> Rachel
    Cave, 'Stanley Barber Shop' -> Andre Stanley, 'Landrys' -> Cordrick Landry."""
    biz_toks = _biz_name_tokens(business_name)
    name_toks = {t.lower() for t in (first, last) if not 
                 _is_no_name(t)}
    return bool(biz_toks & name_toks)

def _independent_corroboration_count(name_part: str, combined_text: str) -> int:
    """How many distinct source blocks in combined_text mention this name part."""
    if not name_part:
        return 0
    norm = lambda s: re.sub(r"\s+", " ", s or "").strip().lower()
    target = norm(name_part)
    return sum(1 for block in combined_text.split("\n---\n") if target in norm(block))

def _calibration_floor(first: str, last: str, title: str, confidence: str) -> tuple[str, str]:
    """Phase 9: confidence is capped in code, never trusted as self-reported.
    Root cause (09-21 audit): 'Big Steve', 'Anthony', 'Niki' were marked high
    with no surname and were wrong. A name with no stand role is 'unclear
    role', which is no longer a deliverable medium - it's low.
    Returns (possibly-downgraded confidencem reason)."""
    last_missing = _is_no_name(last)
    has_nickname_chars = any(c in (first or "") + (last or "") for c in ('"', "'", "(", ")"))
    title_missing = not title or title.upper() in ("<UNKNOWN>", "")

    if title_missing:
        if confidence != "low":
            return "low", f"downgraded {confidence}->low: no role/title stated"
        return confidence, "ok"

    if (last_missing or has_nickname_chars) and confidence == "high":
        return "medium", f"downgraded high>medium: {'no last name' if last_missing else 'nickname pattern'}"

    return confidence, "ok"

def extract_owner(state: LeadState) -> dict:
    """Extract owner info from combined source text using Haiku.
    Phase 7: Capture tokens and handle errors."""
    combined_text = state["combined_source_text"]
    business_name = state["business_name"]
    cost = state["cost"]

    prompt = f"""You are an expert at identifying business decision-makers from web search snippets.

Business: {business_name}

Search results:
{combined_text}

Your task: Identify the primary decision-maker (owner, founder, or manager) from ONLY the text above.
- If you find a clear name with title (e.g., 'John Smith, owner'), extract it.
- If no name is found, return "Unknown" for first and last name, confidence "low".
- ONLY use information explicitly stated in the search results.

evidence_snippet: copy the exact sentence or phrase from the search results above that names this person. Verbatim, no paraphrasing. If you can't find such a sentence, return empty string here and confidence "low".
evidence_url: the URL from the "URL:" line of the source block that snippet came from.

Return JSON with confidence: high (name + clear title), medium (name but unclear role), or low (no clear match)."""

    try:
        output = structured_model.invoke(prompt)
        raw = output["raw"]
        response = output["parsed"]

        if raw.usage_metadata:
            cost.haiku_input_tokens += raw.usage_metadata.get("input_tokens", 0)
            cost.haiku_output_tokens += raw.usage_metadata.get("output_tokens", 0)

        if response is None:
            raise ValueError(f"Structured parse failed: {output.get('parsing_error')}")
        
        logger.debug(f"Haiku extracted: {response.decision_maker_first_name} {response.decision_maker_last_name} ({response.confidence})")

        evidence_ok, evidence_reason = _evidence_supports_name(
            response.decision_maker_first_name,
            response.decision_maker_last_name,
            response.evidence_snippet,
            response.evidence_url,
            combined_text,
            business_name,
        )
        if not evidence_ok:
            logger.debug(
                f"Evidence check failed for {response.decision_maker_first_name} {response.decision_maker_last_name} "
                f"- {evidence_reason} | snippet={response.evidence_snippet!r} | url={response.evidence_url!r}"
            )
            return {
                "decision_maker_first_name": None,
                "decision_maker_last_name": None,
                "title": None,
                "linkedin": None,
                "instagram": None,
                "tiktok": None,
                "facebook": None,
                "confidence": "low",
                "why_this_person": "No verbatim evidence for extracted name.",
                **evidence.empty_evidence(),
                "delivered_confidence": None,
                "cost": cost
            }
            
        logger.debug(f"Evidence check passed for {response.decision_maker_first_name} {response.decision_maker_last_name}")

        final_confidence, calib_reason = _calibration_floor(
            response.decision_maker_first_name,
            response.decision_maker_last_name,
            response.title,
            response.confidence,
        )
        if calib_reason != "ok":
            logger.debug(f"Calibration: {calib_reason}")

        first = _clean_name(response.decision_maker_first_name)
        last = _clean_name(response.decision_maker_last_name)
        ev_url = _canonical_url(response.evidence_url, combined_text) if (first or last) else None
        return {
            "decision_maker_first_name": first,
            "decision_maker_last_name": last,
            "title": evidence.none_if_empty(response.title),
            "linkedin": evidence.none_if_empty(response.linkedin),
            "instagram": evidence.none_if_empty(response.instagram),
            "tiktok": evidence.none_if_empty(response.tiktok),
            "facebook": evidence.none_if_empty(response.facebook),
            "confidence": final_confidence,
            "why_this_person": response.why_this_person,
            **evidence.evidence_fields(ev_url, evidence.SRC_BRAVE, own_website=state.get("website", "")),
            "delivered_confidence": final_confidence if (first or last) else None,
            "cost": cost
        }
    except Exception as e:
        logger.error(f"Haiku extraction failed: {type(e).__name__}: {e}")
        cost.haiku_errors.append(f"Extraction error: {str(e)}")
        
        # Fallback to low confidence. The failure lives in cost.haiku_errors (-> status 'error'
        # if nothing else delivers a name), no longer in a '<ERROR>' name.
        return {
            "decision_maker_first_name": None,
            "decision_maker_last_name": None,
            "title": None,
            "linkedin": None,
            "instagram": None,
            "tiktok": None,
            "facebook": None,
            "confidence": "low",
            "why_this_person": f"Extraction failed: {str(e)}",
            **evidence.empty_evidence(),
            "delivered_confidence": None,
            "cost": cost
        }

def verify_owner(state: LeadState) -> dict:
    """Verify Haiku's extraction using Sonnet (B→A logic).
    Phase 7: Capture tokens and handle errors."""
    haiku_first = state["decision_maker_first_name"]
    haiku_last = state["decision_maker_last_name"]
    combined_text = state["combined_source_text"]
    business_name = state["business_name"]
    cost = state["cost"]

    prompt = f"""You are an expert at verifying business decision-maker identification.

Haiku's extraction: {_prompt_name(state, haiku_first)} {_prompt_name(state, haiku_last)}

Search results:
{combined_text}

Your task:
1. Does the text strongly support {_prompt_name(state, haiku_first)} {_prompt_name(state, haiku_last)} as primary owner/founder?
2. If NOT, find an ALTERNATIVE owner in the results. 

Return JSON:
{{
    "verified_confidence": "high|medium|low",
    "verified_first_name": "...",
    "verified_last_name": "...",
    "verified_evidence_snippet": "exact sentence from the search results naming this person, or empty string",
    "verified_evidence_url": "the URL from the source block that snippet came from, or empty string",
    "alternative_first_name": "... or empty string",
    "alternative_last_name": "... or empty string",
    "verification_reasoning": "1-2 sentences"
}}"""

    try:
        response = reasoning_model.invoke(prompt)
        sonnet_text = response.content
        if isinstance(sonnet_text, list):
            sonnet_text = "".join(
                block.get("text", "") if isinstance(block, dict) else str(block)
                for block in sonnet_text
            )
        
        # Capture tokens
        if response.usage_metadata:
            cost.sonnet_input_tokens += response.usage_metadata.get("input_tokens", 0)
            cost.sonnet_output_tokens += response.usage_metadata.get("output_tokens", 0)

        try:
            json_start = sonnet_text.find('{')
            json_end = sonnet_text.rfind('}') + 1
            verification = json.loads(sonnet_text[json_start:json_end])
        except Exception as json_err:
            logger.warning(f"Sonnet JSON parse failed: {json_err}")
            verification = {
                "verified_confidence": "low",
                "verified_first_name": haiku_first,
                "verified_last_name": haiku_last,
                "verified_evidence_snippet": "",
                "verified_evidence_url": "",
                "alternative_first_name": "",
                "alternative_last_name": "",
                "verification_reasoning": "unable to verify"
            }

        logger.debug(f"Verification: {verification['verified_first_name']} {verification['verified_last_name']} ({verification['verified_confidence']})")

        evidence_ok, evidence_reason = _evidence_supports_name(
            verification["verified_first_name"],
            verification["verified_last_name"],
            verification.get("verified_evidence_snippet", ""),
            verification.get("verified_evidence_url", ""),
            combined_text,
            business_name,
        )
        if not evidence_ok:
            logger.debug(
                f"Verify evidence check failed for {verification['verified_first_name']} {verification['verified_last_name']} "
                f"- {evidence_reason}"
            )
            return {
                "verified_confidence": "low",
                "decision_maker_first_name": None,
                "decision_maker_last_name": None,
                "alternative_first_name": None,
                "alternative_last_name": None,
                "verification_reasoning": "No verbatim evidence for verified name.",
                "title": None,
                "linkedin": None,
                "instagram": None,
                "tiktok": None,
                "facebook": None,
                **evidence.empty_evidence(),
                "delivered_confidence": None,
                "cost": cost
            }

        v_first = _clean_name(verification["verified_first_name"])
        v_last = _clean_name(verification["verified_last_name"])
        has_name = bool(v_first or v_last)
        same = _same_person(state, v_first, v_last)       # Haiku's title/socials only apply to the same person
        ev_url = _canonical_url(verification.get("verified_evidence_url", ""), combined_text) if has_name else None
        return {
            "verified_confidence": verification["verified_confidence"],
            "decision_maker_first_name": v_first,
            "decision_maker_last_name": v_last,
            "alternative_first_name": evidence.none_if_empty(verification["alternative_first_name"]),
            "alternative_last_name": evidence.none_if_empty(verification["alternative_last_name"]),
            "verification_reasoning": verification["verification_reasoning"],
            **({} if same else {"title": None, "linkedin": None, "instagram": None, "tiktok": None, "facebook": None}),
            **evidence.evidence_fields(ev_url, evidence.SRC_BRAVE, own_website=state.get("website", "")),
            "delivered_confidence": verification["verified_confidence"] if has_name else None,
            "cost": cost
        }
    except Exception as e:
        logger.error(f"Sonnet verification failed: {type(e).__name__}: {e}")
        cost.sonnet_errors.append(f"Verification error: {str(e)}")
        
        # Fallback: downgrade to low
        return {
            "verified_confidence": "low",
            "decision_maker_first_name": haiku_first,
            "decision_maker_last_name": haiku_last,
            "alternative_first_name": "",
            "alternative_last_name": "",
            "verification_reasoning": f"Verification failed: {str(e)}",
            "cost": cost
        }

def route_after_search(state: LeadState) -> str:
    confidence = state["confidence"]
    if confidence == "high":
        logger.debug(f"→ High confidence, skipping verification")
        return "end"
    else:
        logger.debug(f"→ {confidence.capitalize()} confidence, routing to verification")
        return "verify_owner"

def route_after_verification(state: LeadState) -> str:
    verified_confidence = state["verified_confidence"]
    # Phase 10: no evidence -> blank. If Haiku could not back any name with evidence
    # (confidence low), a name Sonnet proposes on its own is not deliverable.
    # Saved runs: 2 such rescues, both wrong (Dave, Hendu).
    if state["confidence"] == "low":
        verified_confidence = "low"
    if verified_confidence in ["high", "medium"]:
        logger.info(f"Verified {verified_confidence} confidence, sending to END")
        return "end"
    else:
        logger.info(f"Verified low confidence, attempting About-page enrichment")
        return "fetch_about_page"

def fetch_about_page(state: LeadState) -> dict:
    """Fetch and filter About page for owner names.
    Phase 8: only from the lead's OWN domain, never the top search result's -
    a same-named out-of-state shop ranking first used to make a wrong answer
    more confident. Skips booking-platform URLs (Booksy, thecut, Square, etc.)."""
    cost = state["cost"]

    website = state.get("website", "")
    fallback = state.get("fallback_website", "")

    domain_source = ""
    if website and not _is_booking_platform(website):
        domain_source = website
    elif fallback and not _is_booking_platform(fallback):
        domain_source = fallback

    if not domain_source:
        logger.debug("No usable owned domain for About-page fetch (blank or booking platform)")
        return {"about_page_text": "", "about_page_url": None}

    # Parse domain
    try:
        parsed = urlparse(domain_source)
        if not parsed.scheme:
            parsed = urlparse(f"https://{domain_source}")
        domain = f"{parsed.scheme}://{parsed.netloc}"
    except Exception:
        logger.debug(f"Failed to parse domain from {domain_source}")
        return {"about_page_text": "", "about_page_url": None}

    # Try about-page variants
    about_paths = ["/about", "/about-us", "/team", "/about-page"]
    about_text = ""
    fetched_url = None

    for path in about_paths:
        try:
            about_url = domain + path
            response = requests.get(about_url, timeout=5)
            if response.status_code == 200:
                about_text = response.text
                fetched_url = about_url
                logger.debug(f"Fetched {about_url}")
                break
        except Exception:
            pass
    
    # Filter: only sentences with founder/owner/CEO + capitalized name
    if about_text:
        sentences = about_text.split('.')
        filtered = []
        for sent in sentences:
            if any(kw in sent.lower() for kw in ['founder', 'owner', 'ceo']) and \
               any(c.isupper() for c in sent):
                filtered.append(sent.strip())
        about_text = '. '.join(filtered[:3])[:8000]
    
    logger.debug(f"About-page text: {len(about_text)} chars")
    return {"about_page_text": about_text, "about_page_url": fetched_url, "cost": cost}

def extract_owner_from_about(state: LeadState) -> dict:
    """Second-pass extraction from About page using Haiku.
    Phase 7: Capture tokens and handle errors."""
    about_text = state["about_page_text"]
    business_name = state["business_name"]
    cost = state["cost"]

    if not about_text or about_text == "":
        logger.debug("No about_page_text to extract from")
        return {"confidence": "low"}

    prompt = f"""From this About page text, identify the primary decision-maker (owner, founder, CEO).

Business: {business_name}
About page text: {about_text}

evidence_snippet: copy the exact sentence or phrase from the About page text above that name this person. Verbatim, no paraphrasing. If you can't find such a sentence, return empty string here and confidence "low"
evidence_url: leave this as the literal string "about_page" 

Return JSON with confidence: high/medium/low."""

    try:
        output = structured_model.invoke(prompt)
        raw = output["raw"]
        response = output["parsed"]

        # Capture tokens from the raw AIMessage
        if raw.usage_metadata:
            cost.haiku_input_tokens += raw.usage_metadata.get("input_tokens", 0)
            cost.haiku_output_tokens += raw.usage_metadata.get("output_tokens", 0)

        if response is None:
            raise ValueError(f"Structured parse failed: {output.get('parsing_error')}")
        
        logger.debug(f"About-page extraction: {response.decision_maker_first_name} {response.decision_maker_last_name} ({response.confidence})")

        evidence_ok, evidence_reason = _about_evidence_supports_name(
            response.decision_maker_first_name,
            response.decision_maker_last_name,
            response.evidence_snippet,
            about_text,
        )
        if not evidence_ok:
            logger.debug(f"About-page evidence check failed for {response.decision_maker_first_name} {response.decision_maker_last_name} - {evidence_reason}")
            return {
                "decision_maker_first_name": None,
                "decision_maker_last_name": None,
                "confidence": "low",
                "title": None,
                "linkedin": None,
                "instagram": None,
                "tiktok": None,
                "facebook": None,
                **evidence.empty_evidence(),
                "delivered_confidence": None,
                "cost": cost
            }
        
        a_first = _clean_name(response.decision_maker_first_name)
        a_last = _clean_name(response.decision_maker_last_name)
        has_name = bool(a_first or a_last)
        about_url = state.get("about_page_url") if has_name else None
        return {
            "decision_maker_first_name": a_first,
            "decision_maker_last_name": a_last,
            "confidence": response.confidence,
            "title": evidence.none_if_empty(response.title),
            "linkedin": evidence.none_if_empty(response.linkedin),
            "instagram": evidence.none_if_empty(response.instagram),
            "tiktok": evidence.none_if_empty(response.tiktok),
            "facebook": evidence.none_if_empty(response.facebook),
            **evidence.evidence_fields(about_url, evidence.SRC_ABOUT, own_website=about_url or ""),
            "delivered_confidence": response.confidence if has_name else None,
            "cost": cost
        }
    except Exception as e:
        logger.error(f"About-page extraction failed: {type(e).__name__}: {e}")
        cost.haiku_errors.append(f"About-page error: {str(e)}")
        return {"confidence": "low", "cost": cost}

def flag_for_review(state: LeadState) -> dict:
    """Phase 9: no evidence -> blank. Only low-confidence leads reach this node,
    so any name still in state is unverified and must not be delivered."""
    logger.info("→ No owner found, flagged for manual review")
    return {"decision_maker_first_name": None,
            "decision_maker_last_name": None,
            "title": None,
            "linkedin": None,
            "instagram": None,
            "tiktok": None,
            "facebook": None,
            **evidence.empty_evidence(),
            "delivered_confidence": None}

def gemini_search(state: LeadState) -> dict:
    """Second path (stage C2). Runs next to the Brave branch and writes ONLY the 'gemini' key. It never raises: a
    timeout, an empty response or any failure is status 'error', and reconcile then keeps the Brave answer."""
    if not GEMINI_ENABLED:
        return {"gemini": {"status": "disabled", "error": None}}
    lead = {k: state.get(k) or "" for k in ("business_name", "address", "city", "state", "phone", "website")}
    try:
        res = gemini_path.ask_gemini(lead, os.getenv("GEMINI_API_KEY"), model=GEMINI_MODEL)
    except Exception as e:                      # belt and braces; ask_gemini is written not to raise
        res = {"status": "error", "error": f"{type(e).__name__}: {e}"[:200], "cost_usd": 0.0, "seconds": 0.0}
    logger.debug(f"Gemini for {lead['business_name']}: {res.get('status')} {res.get('error') or ''}")
    return {"gemini": res}

_EVIDENCE_KEYS = ("evidence_url", "evidence_domain", "evidence_source", "evidence_url_kind", "source_tier",
                  "other_evidence_urls")

def reconcile_node(state: LeadState) -> dict:
    """Join of the two paths (stage C2, option 5; rules in reconcile.py). Waits for the Brave branch AND Gemini."""
    first, last = state.get("decision_maker_first_name"), state.get("decision_maker_last_name")
    brave = None
    if first or last:
        brave = {"first": first, "last": last, "title": state.get("title"),
                 "confidence": state.get("delivered_confidence") or state.get("confidence"),
                 "evidence": {k: state.get(k) for k in _EVIDENCE_KEYS}}
    out = {"brave_answer": {"first": first, "last": last, "title": state.get("title"),
                            "confidence": state.get("delivered_confidence") or state.get("confidence"),
                            "evidence_url": state.get("evidence_url")}}
    res = reconcile.reconcile(brave, state.get("gemini"))
    out["reconcile_case"] = res["case"]
    out["review_candidates"] = json.dumps(res["candidates"]) if res["candidates"] else None
    if res["keep_brave"]:
        return out                                   # Gemini unavailable: the Brave answer stands, untouched
    if res["deliver"]:
        same_as_brave = brave is not None and (res["first"], res["last"]) == (first, last)
        out.update({"decision_maker_first_name": res["first"], "decision_maker_last_name": res["last"],
                    "title": res["title"], "delivered_confidence": res["confidence"], "goes_by_name": res["goes_by"],
                    **res["evidence"]})
        if not same_as_brave:                        # Brave's socials belong to Brave's candidate
            out.update({"linkedin": None, "instagram": None, "tiktok": None, "facebook": None})
    else:
        out.update({"decision_maker_first_name": None, "decision_maker_last_name": None, "title": None,
                    "linkedin": None, "instagram": None, "tiktok": None, "facebook": None,
                    "goes_by_name": None, "delivered_confidence": None, **evidence.empty_evidence()})
    return out

def finalize(state: LeadState) -> dict:
    """Last node on every path. Stage A, record-keeping only: sets status ('ok' or 'error') and its
    detail, and makes sure a blank carries no evidence or confidence. It never changes a name."""
    has_name = bool(state.get("decision_maker_first_name") or state.get("decision_maker_last_name"))
    cost = state["cost"]
    g_ok = (state.get("gemini") or {}).get("status") == "ok"      # a Gemini that completed makes a blank credible
    status, detail = evidence.compute_status(
        has_name, brave_errors=0 if g_ok else len(cost.brave_errors), n_queries=len(state.get("queries") or []),
        haiku_errors=0 if g_ok else len(cost.haiku_errors), sonnet_errors=0 if g_ok else len(cost.sonnet_errors))
    out = {"status": status, "error_detail": detail}
    if not has_name:
        out.update(evidence.empty_evidence())
        out["delivered_confidence"] = None
    return out

def route_after_about(state: LeadState) -> str:
    confidence = state["confidence"]
    if confidence in ["high", "medium"]:
        logger.info(f"About-page: {confidence} confidence, sending to END")
        return "end"
    else:
        logger.info(f"About-page: still low confidence, flagging for review")
        return "flag_for_review"

# Build graph
graph = StateGraph(LeadState)

graph.add_node("prep_lead", prep_lead)
graph.add_node("build_queries", build_queries)
graph.add_node("search_q1", search_q1)
graph.add_node("search_q2", search_q2)
graph.add_node("search_q3", search_q3)
graph.add_node("search_q4", search_q4)
graph.add_node("search_q5", search_q5)
graph.add_node("combine_and_score", combine_and_score)
graph.add_node("extract_owner", extract_owner)
graph.add_node("verify_owner", verify_owner)
graph.add_node("fetch_about_page", fetch_about_page)
graph.add_node("extract_owner_from_about", extract_owner_from_about)
graph.add_node("flag_for_review", flag_for_review)
graph.add_node("finalize", finalize)
graph.add_node("gemini_search", gemini_search)
# defer=True: reconcile waits until EVERY other pending task is done. The Brave branch has many steps, the Gemini
# branch one; without it reconcile would fire as soon as Gemini returns and then again when Brave ends.
graph.add_node("reconcile", reconcile_node, defer=True)

graph.set_entry_point("prep_lead")
graph.add_edge("prep_lead", "build_queries")
graph.add_edge("prep_lead", "gemini_search")
graph.add_edge("build_queries", "search_q1")
graph.add_edge("build_queries", "search_q2")
graph.add_edge("build_queries", "search_q3")
graph.add_edge("build_queries", "search_q4")
graph.add_edge("build_queries", "search_q5")
graph.add_edge("search_q1", "combine_and_score")
graph.add_edge("search_q2", "combine_and_score")
graph.add_edge("search_q3", "combine_and_score")
graph.add_edge("search_q4", "combine_and_score")
graph.add_edge("search_q5", "combine_and_score")
graph.add_edge("combine_and_score", "extract_owner")

graph.add_conditional_edges(
    "extract_owner",
    route_after_search,
    {
        "verify_owner": "verify_owner",
        "end": "reconcile"
    }
)

graph.add_conditional_edges(
    "verify_owner",
    route_after_verification,
    {
        "fetch_about_page": "fetch_about_page",
        "end": "reconcile"
    }
)
graph.add_edge("fetch_about_page", "extract_owner_from_about")

graph.add_conditional_edges(
    "extract_owner_from_about",
    route_after_about,
    {
        "flag_for_review": "flag_for_review",
        "end": "reconcile"
    }
)
graph.add_edge("flag_for_review", "reconcile")
graph.add_edge("gemini_search", "reconcile")
graph.add_edge("reconcile", "finalize")
graph.add_edge("finalize", END)

app = graph.compile()

# Phase 7: Accuracy comparison utilities
class AccuracyComparison:
    """Compare LangGraph results against n8n baseline."""
    
    @staticmethod
    def load_n8n_extractions(csv_path: str) -> dict:
        """Load n8n's owner extractions from CSV.
        Returns: {business_name: {'first': ..., 'last': ..., 'confidence': ...}}"""
        extractions = {}
        try:
            with open(csv_path, 'r', encoding='utf-8') as f:
                reader = csv.DictReader(f)
                for row in reader:
                    business_name = row.get('business_name', '').strip()
                    first = row.get('decision_maker_first_name', '').strip()
                    last = row.get('decision_maker_last_name', '').strip()
                    conf = row.get('identity_confidence', 'low').strip()
                    
                    if business_name:
                        extractions[business_name] = {
                            'first': first,
                            'last': last,
                            'confidence': conf
                        }
        except Exception as e:
            logger.error(f"Failed to load n8n extractions: {e}")
        
        return extractions
    
    @staticmethod
    def compare(langgraph_result: dict, n8n_baseline: dict) -> dict:
        """Compare one LangGraph result to n8n baseline.
        Returns match status: 'exact', 'partial', 'miss'."""
        def _norm(name: str) -> str:
            n = (name or '').strip()
            return '' if n.upper() in ('<UNKNOWN>', '<UNKNOWN>', '<ERROR>', 'NA', 'N/A', 'NONE') else n
        lg_first = _norm(langgraph_result.get('decision_maker_first_name', ''))
        lg_last = _norm((langgraph_result.get('decision_maker_last_name', '') or '').strip())
        lg_conf = langgraph_result.get('confidence', 'low')

        n8n_first = n8n_baseline.get('first', '')
        n8n_last = n8n_baseline.get('last', '')
        n8n_conf = n8n_baseline.get('confidence', '')
        
        # Both missing = no match
        if not lg_first and not n8n_first:
            return {'match': 'both_unknown', 'notes': 'Both systems found no owner'}
        
        # LG found nothing = miss
        if not lg_first:
            n8n_first = _norm(n8n_first)
            n8n_last = _norm(n8n_last)
            if lg_first and not n8n_first:
                return {'match': 'lg_only',
                         'notes': f'LG found {lg_first} {lg_last} ({lg_conf}); n8n found nothing - manual verify'}
        
        # Exact match (first + last)
        if lg_first.lower() == n8n_first.lower() and lg_last.lower() == n8n_last.lower():

            rank = {'low': 0, 'medium':1, 'high':2}
            lg_rank = rank.get(lg_conf, 0)
            n8n_rank = rank.get(n8n_conf, 0)

            if lg_conf == 'high' and n8n_conf == 'high':
                return {'match': 'exact',
                        'confidence_alignment': 'both_high_pass',
                        'notes': 'Full name match, both high confidence'}

            elif lg_rank >= n8n_rank:
                return {'match': 'exact',
                        'confidence_alignment': 'lg_better_or_equal',
                        'notes': f'Full name match, LG: {lg_conf} >= n8n: {n8n_conf}'}

            elif lg_rank < n8n_rank:
                return {'match': 'exact',
                        'confidence_alignment': 'flag_review',
                        'notes': f'REVIEW: Full name match but LG: {lg_conf} < n8n: {n8n_conf} (why is thorough system less confident?)'}

            return {'match': 'exact',
                    'notes': 'Full name match'}
        
        # First name match only
        if lg_first.lower() == n8n_first.lower() and n8n_first:
            return {'match': 'partial', 
                    'notes': f'First name match: {lg_first} (LG: {lg_conf}, n8n: {n8n_conf})'}
        
        # Last name match only
        if lg_last.lower() == n8n_last.lower() and n8n_last:
            return {'match': 'partial',
                    'notes': f'Last name match: {lg_last} (LG: {lg_conf}, n8n: {n8n_conf})'}
        
        # Different person
        return {'match': 'miss',
                'notes': f'LG: {lg_first} {lg_last} ({lg_conf}) vs n8n: {n8n_first} {n8n_last} ({n8n_conf})'}


def _invoke_lead(lead):
    """Run the graph for one lead. Returns (result, error); never raises, so one bad lead cannot stop a batch."""
    try:
        return app.invoke({
            "business_name": lead.business_name,
            "lead_id": lead.id,
            "website": lead.website,
            "address": lead.address,
            "phone": lead.phone,
            "city": lead.city,
            "state": lead.state,
        }), None
    except Exception as e:
        return None, e

def gemini_error_rate(results):
    """(ok, error, rate) over the results whose Gemini path ran (disabled or missing results do not count)."""
    st = [((r or {}).get("gemini") or {}).get("status") for r in results]
    ok, err = st.count("ok"), st.count("error")
    return ok, err, (err / (ok + err) if ok + err else 0.0)

def process_leads_batch(csv_path: str = "MBG_-_Charlotte_Barbershops_-_Charlotte_Barbershops__7_.csv",
                        workers: int = DEFAULT_WORKERS):
    """Process multiple leads from database and compare to n8n baseline."""
    db = LeadDatabase()
    gemini_path.reset()          # a 402 disables Gemini for ONE batch only
    leads, skipped = intake.filter_leads(db.get_all_leads(), log=logger)
    skipped_path = intake.write_skipped_file(skipped)
    logger.info(f"Skipped leads written to {skipped_path}")

    logger.info(f"\n{'='*80}")
    logger.info(f"PHASE 7: BATCH PROCESSING + COST TRACKING + ACCURACY COMPARISON")
    logger.info(f"{'='*80}\n")
    logger.info(f"Processing {len(leads)} leads from database")
    logger.info(f"Comparing against n8n baseline from CSV: {csv_path}\n")

    # Load n8n baseline
    n8n_extractions = AccuracyComparison.load_n8n_extractions(csv_path)
    logger.info(f"Loaded {len(n8n_extractions)} n8n extractions from CSV\n")

    results_summary = []
    cost_summary = LeadCost(lead_id=0, business_name="BATCH_TOTAL")
    accuracy_metrics = {
        'exact': 0,
        'partial': 0,
        'miss': 0,
        'both_unknown': 0,
        'langgraph_errors': 0,
        'lg_only': 0
    }

    # Leads run concurrently (the graph is thread-safe per call); database writes and the comparison stay on this thread.
    batch_start = time.time()
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        outcomes = list(pool.map(_invoke_lead, leads))
    batch_seconds = time.time() - batch_start

    for i, (lead, (result, run_error)) in enumerate(zip(leads, outcomes), 1):
        logger.info(f"\n[{i}/{len(leads)}] {lead.business_name}")

        try:
            if run_error is not None:
                raise run_error
            db.write_result(lead.id, result)

            confidence = result.get('confidence', 'unknown')
            first_name = result.get('decision_maker_first_name', 'unknown')
            last_name = result.get('decision_maker_last_name', 'unknown')
            cost = result.get('cost')

            # Accumulate costs
            if cost:
                cost_summary.haiku_input_tokens += cost.haiku_input_tokens
                cost_summary.haiku_output_tokens += cost.haiku_output_tokens
                cost_summary.sonnet_input_tokens += cost.sonnet_input_tokens
                cost_summary.sonnet_output_tokens += cost.sonnet_output_tokens
                cost_summary.brave_calls += cost.brave_calls

            logger.info(f"  Result: {first_name} {last_name} ({confidence})")
            if cost:
                logger.info(f"  Cost: {cost.total_tokens} tokens, {cost.brave_calls} Brave calls, est. ${cost.estimated_cost_usd():.4f}")

            # Accuracy comparison
            n8n_baseline = n8n_extractions.get(lead.business_name, {'first': '', 'last': '', 'confidence': ''})
            comparison = AccuracyComparison.compare(result, n8n_baseline)
            match = comparison['match']
            accuracy_metrics[match] += 1

            logger.info(f"  Comparison: {match.upper()} — {comparison['notes']}")

            results_summary.append({
                'business_name': lead.business_name,
                'langgraph_owner': f"{first_name or ''} {last_name or ''}".strip(),
                'langgraph_confidence': confidence,
                'n8n_owner': f"{n8n_baseline.get('first', '')} {n8n_baseline.get('last', '')}".strip(),
                'n8n_confidence': n8n_baseline.get('confidence', ''),
                'match': match,
                'tokens': cost.total_tokens if cost else 0,
                'brave_calls': cost.brave_calls if cost else 0,
                'estimated_cost_usd': f"${cost.estimated_cost_usd():.4f}" if cost else "$0.00"
            })

        except Exception as e:
            logger.error(f"✗ Error processing {lead.business_name}: {e}")
            accuracy_metrics['langgraph_errors'] += 1
            try:     # a crashed lead is an error row, not a missing one, so the export shows it
                db.write_result(lead.id, {"status": "error",
                                          "error_detail": f"graph crashed: {type(e).__name__}: {e}"[:300]})
            except Exception as db_err:
                logger.error(f"Could not record the crash for {lead.business_name}: {db_err}")
            results_summary.append({
                'business_name': lead.business_name,
                'langgraph_owner': 'ERROR',
                'langgraph_confidence': 'failed',
                'n8n_owner': n8n_extractions.get(lead.business_name, {}).get('first', ''),
                'n8n_confidence': n8n_extractions.get(lead.business_name, {}).get('confidence', ''),
                'match': 'error',
                'tokens': 0,
                'brave_calls': 0,
                'estimated_cost_usd': '$0.00'
            })

    g_ok, g_err, g_rate = gemini_error_rate([r for r, _ in outcomes])
    logger.info(f"Gemini path: {g_ok} ok, {g_err} error ({g_rate:.0%} error rate); {workers} workers, {batch_seconds:.0f}s")
    _costs = [r["cost"] for r, _ in outcomes if r and r.get("cost")]
    logger.info(f"Brave: {sum(c.brave_calls for c in _costs)} searches, {sum(c.brave_429 for c in _costs)} rate-limited (429), "
                f"{sum(c.brave_429_recovered for c in _costs)} recovered on retry, {sum(len(c.brave_errors) for c in _costs)} searches errored")
    try:     # stored per batch next to the per-lead gemini_status/gemini_error columns
        stats_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "batch_stats.csv")
        new = not os.path.exists(stats_path)
        with open(stats_path, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if new:
                w.writerow(["finished", "leads", "workers", "seconds", "gemini_ok", "gemini_error", "gemini_error_rate",
                            "gemini_requests", "gemini_search_queries", "brave_searches", "brave_429", "brave_429_recovered",
                            "brave_search_errors"])
            costs = [r["cost"] for r, _ in outcomes if r and r.get("cost")]
            gem = [(r or {}).get("gemini") or {} for r, _ in outcomes]
            w.writerow([datetime.now().isoformat(timespec="seconds"), len(leads), workers, round(batch_seconds),
                        g_ok, g_err, round(g_rate, 3), sum(g.get("attempts") or 0 for g in gem),
                        sum(g.get("searches") or 0 for g in gem), sum(c.brave_calls for c in costs),
                        sum(c.brave_429 for c in costs), sum(c.brave_429_recovered for c in costs),
                        sum(len(c.brave_errors) for c in costs)])
    except Exception as e:
        logger.error(f"Could not write batch_stats.csv: {e}")

    # SUMMARY
    logger.info(f"\n{'='*80}")
    logger.info("BATCH RESULTS SUMMARY")
    logger.info(f"{'='*80}\n")

    # Cost summary
    logger.info("COST TRACKING:")
    logger.info(f"  Haiku tokens: {cost_summary.haiku_input_tokens:,} in + {cost_summary.haiku_output_tokens:,} out")
    logger.info(f"  Sonnet tokens: {cost_summary.sonnet_input_tokens:,} in + {cost_summary.sonnet_output_tokens:,} out")
    logger.info(f"  Total tokens: {cost_summary.total_tokens:,}")
    logger.info(f"  Brave API calls: {cost_summary.brave_calls}")
    logger.info(f"  Estimated cost: ${cost_summary.estimated_cost_usd():.2f}")
    logger.info(f"  Avg cost per lead: ${cost_summary.estimated_cost_usd() / len(leads):.4f}\n")

    # Accuracy summary
    total_processed = len(results_summary) - accuracy_metrics['langgraph_errors']
    logger.info("ACCURACY COMPARISON vs n8n baseline:")
    logger.info(f"  Exact matches: {accuracy_metrics['exact']}/{total_processed} ({accuracy_metrics['exact']/total_processed*100:.1f}%)")
    logger.info(f"  Partial matches (first or last name): {accuracy_metrics['partial']}/{total_processed} ({accuracy_metrics['partial']/total_processed*100:.1f}%)")
    logger.info(f"  Misses: {accuracy_metrics['miss']}/{total_processed} ({accuracy_metrics['miss']/total_processed*100:.1f}%)")
    logger.info(f"  Both systems unknown: {accuracy_metrics['both_unknown']}/{total_processed} ({accuracy_metrics['both_unknown']/total_processed*100:.1f}%)")
    logger.info(f"  LangGraph processing errors: {accuracy_metrics['langgraph_errors']}\n")

    hit_rate = (accuracy_metrics['exact'] + accuracy_metrics['partial']) / total_processed * 100
    logger.info(f"  Combined hit rate (exact + partial): {hit_rate:.1f}%")
    logger.info(f"  System availability: {(total_processed / len(leads)) * 100:.1f}%\n")

    logger.info(f"{'='*80}\n")

    # Write detailed comparison to file
    comparison_file = f"phase7_accuracy_comparison_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    try:
        with open(comparison_file, 'w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=results_summary[0].keys())
            writer.writeheader()
            writer.writerows(results_summary)
        logger.info(f"Detailed comparison saved to: {comparison_file}\n")
    except Exception as e:
        logger.error(f"Failed to write comparison file: {e}\n")

    # Stage A: the enriched CSV (one row per lead, errors apart from blanks, skips listed)
    try:
        out_path, counts = export.write_export(db.db_path)
        logger.info(f"Enriched export written to {out_path}: {counts}\n")
    except Exception as e:
        logger.error(f"Export failed: {e}\n")

    # Sample output
    logger.info("Sample results (first 10):")
    for result in results_summary[:10]:
        logger.info(f"  {result['business_name']}: LG={result['langgraph_owner']} ({result['langgraph_confidence']}) vs n8n={result['n8n_owner']} ({result['n8n_confidence']}) → {result['match'].upper()}")


if __name__ == "__main__":
    process_leads_batch()
