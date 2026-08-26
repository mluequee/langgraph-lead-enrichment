from typing import TypedDict, Literal, Annotated
from langgraph.graph import END, StateGraph
from langchain_anthropic import ChatAnthropic
from dotenv import load_dotenv
from pydantic import BaseModel
from dataclasses import dataclass
import os
import requests
import operator
import json
import logging
import sqlite3
import logging as py_logging
import csv
import sys
if sys.platform == 'win32':
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')
from datetime import datetime

py_logging.getLogger("anthropic").setLevel(py_logging.WARNING)
py_logging.getLogger("langchain").setLevel(py_logging.WARNING)
py_logging.getLogger("langchain_anthropic").setLevel(py_logging.WARNING)
load_dotenv()

DB_PATH = "leads.db"

@dataclass
class Lead:
    """Represents a lead from the database."""
    id: int
    business_name: str
    website: str

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
    
    def __post_init__(self):
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

    def get_all_leads(self) -> list[Lead]:
        """Fetch all unprocessed leads."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        cursor.execute("SELECT id, business_name, website FROM leads")
        rows = cursor.fetchall()
        conn.close()

        return [Lead(id=row['id'], business_name=row['business_name'], website=row['website']) for row in rows]

    def write_result(self, lead_id: int, result: dict) -> None:
        """Write extraction results for a lead. FIXED: corrected typos from Phase 6."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        cursor.execute("""
            INSERT INTO results (
                lead_id, decision_maker_first_name, decision_maker_last_name,
                title, instagram, tiktok, facebook, linkedin, confidence, 
                why_this_person, about_page_text
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            result.get('about_page_text', '')
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

class VerificationResult(BaseModel):
    """Sonnet's verification of Haiku's extraction."""
    verified_confidence: Literal["high", "medium", "low"]
    verified_first_name: str
    verified_last_name: str   
    alternative_first_name: str  
    alternative_last_name: str
    verification_reasoning: str

class LeadState(TypedDict):
    business_name: str
    queries: list[str]
    search_results: Annotated[list[dict], operator.add]
    combined_source_text: str
    decision_maker_first_name: str
    decision_maker_last_name: str
    title: str
    linkedin: str
    instagram: str
    tiktok: str
    facebook: str
    confidence: Literal["high", "medium", "low"]
    why_this_person: str
    verified_confidence: Literal["high", "medium", "low"]
    alternative_first_name: str
    alternative_last_name: str
    verification_reasoning: str
    about_page_text: str
    # Phase 7: Cost tracking
    cost: LeadCost

structured_model = extract_model.with_structured_output(OwnerResult, include_raw=True)

def prep_lead(state: LeadState) -> dict:
    business_name = state.get("business_name", "Unknown")
    logger.info(f"Processing lead: {business_name}")
    # Initialize cost tracking for this lead
    lead_id = state.get("lead_id", 0)
    cost = LeadCost(lead_id=lead_id, business_name=business_name)
    
    return {
        "business_name": business_name,
        "queries": [],
        "search_results": [],
        "combined_source_text": "",
        "decision_maker_first_name": "<UNKNOWN>",
        "decision_maker_last_name": "<UNKNOWN>",
        "title": "<UNKNOWN>",
        "linkedin": "<UNKNOWN>",
        "instagram": "<UNKNOWN>",
        "tiktok": "<UNKNOWN>",
        "facebook": "<UNKNOWN>",
        "confidence": "low",
        "why_this_person": "",
        "verified_confidence": "low",
        "alternative_first_name": "",
        "alternative_last_name": "",
        "verification_reasoning": "",
        "about_page_text": "",
        "cost": cost
    }

def build_queries(state: LeadState) -> dict:
    """Port n8n's 4-query construction."""
    business_name = state["business_name"]

    queries = [
        f"{business_name} owner",
        f"{business_name} founded barber",
        f"{business_name} instagram",
        f"{business_name} decision maker",
    ]
    logger.debug(f"Built 4 queries for '{business_name}':\n" + "\n".join(f" Q{i+1}: {q}" for i, q in enumerate(queries)))
    return {"queries": queries}

def search_q1(state: LeadState) -> dict:
    return _brave_search(state, query_index=0)

def search_q2(state: LeadState) -> dict:
    return _brave_search(state, query_index=1)

def search_q3(state: LeadState) -> dict:
    return _brave_search(state, query_index=2)

def search_q4(state: LeadState) -> dict:
    return _brave_search(state, query_index=3)

def _brave_search(state: LeadState, query_index: int) -> dict:
    """Execute one Brave search and return results as a list (for reducer).
    Phase 7: Track Brave calls in cost object."""
    url = "https://api.search.brave.com/res/v1/web/search"
    headers = {
        "X-Subscription-Token": brave_key,
        "Accept": "application/json"
    }

    query = state["queries"][query_index]
    params = {"q": query}

    logger.debug(f"Searching Q{query_index + 1}: '{query}'...")

    cost = state["cost"]
    cost.brave_calls += 1

    try:
        response = requests.get(url, headers=headers, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()
        results = data.get("web", {}).get("results", [])
        logger.debug(f"Q{query_index + 1} returned {len(results)} results")
    except requests.exceptions.Timeout:
        logger.warning(f"Q{query_index + 1} timeout: {query}")
        cost.brave_calls += 1  # Count the retry
        results = []
    except requests.exceptions.HTTPError as e:
        logger.warning(f"Q{query_index + 1} HTTP error ({response.status_code}): {e}")
        results = []
    except Exception as e:
        logger.warning(f"Q{query_index + 1} failed: {type(e).__name__}: {e}")
        results = []

    return {
        "search_results": [
            {
                "query": query,
                "query_index": query_index,
                "results": results,
            }
        ],
    }

def combine_and_score(state: LeadState) -> dict:
    """Score, dedupe, rank and format results from all 4 queries"""
    business_name = state["business_name"].lower()
    all_results_with_scores = []

    for search_set in state["search_results"]:
        results = search_set.get("results", [])

        for result in results:
            url = result.get("url", "").lower()
            title = result.get("title", "").lower()
            description = result.get("description", "").lower()

            # Skip filtered URLs
            if "authwall" in url or "linkedin-signup" in url:
                continue

            score = _score_result(title, description, url, business_name)

            all_results_with_scores.append({
                "url": result.get("url", ""),
                "title": result.get("title", ""),
                "description": result.get("description", ""),
                "score": score
            })

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

    # Combine into one text string (40k char cap)
    combined_text = "\n---\n".join(
        f"[{i+1}] {r['title']}\n{r['description']}\nURL: {r['url']}"
        for i, r in enumerate(top_results)
    )

    if len(combined_text) > 40_000:
        combined_text = combined_text[:40_000]

    logger.debug(f"Combined & scored: {len(deduped)} deduped, top 8 ranked, {len(combined_text)} chars")

    return {
        "combined_source_text": combined_text,
    }

def _score_result(title: str, description: str, url: str, business_name: str) -> int:
    """Score a single search result."""
    score = 0

    # Platform signals
    if "instagram" in url:
        score += 60
    if "tiktok" in url:
        score += 55
    if "facebook" in url:
        score += 40
    if "linkedin" in url:
        score += 45

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
        
        return {
            "decision_maker_first_name": response.decision_maker_first_name,
            "decision_maker_last_name": response.decision_maker_last_name,
            "title": response.title,
            "linkedin": response.linkedin,
            "instagram": response.instagram,
            "tiktok": response.tiktok,
            "facebook": response.facebook,
            "confidence": response.confidence,
            "why_this_person": response.why_this_person,
            "cost": cost
        }
    except Exception as e:
        logger.error(f"Haiku extraction failed: {type(e).__name__}: {e}")
        cost.haiku_errors.append(f"Extraction error: {str(e)}")
        
        # Fallback to low confidence
        return {
            "decision_maker_first_name": "<ERROR>",
            "decision_maker_last_name": "<ERROR>",
            "title": "",
            "linkedin": "",
            "instagram": "",
            "tiktok": "",
            "facebook": "",
            "confidence": "low",
            "why_this_person": f"Extraction failed: {str(e)}",
            "cost": cost
        }

def verify_owner(state: LeadState) -> dict:
    """Verify Haiku's extraction using Sonnet (B→A logic).
    Phase 7: Capture tokens and handle errors."""
    haiku_first = state["decision_maker_first_name"]
    haiku_last = state["decision_maker_last_name"]
    combined_text = state["combined_source_text"]
    cost = state["cost"]

    prompt = f"""You are an expert at verifying business decision-maker identification.

Haiku's extraction: {haiku_first} {haiku_last}

Search results:
{combined_text}

Your task:
1. Does the text strongly support {haiku_first} {haiku_last} as primary owner/founder?
2. If NOT, find an ALTERNATIVE owner in the results. 

Return JSON:
{{
    "verified_confidence": "high|medium|low",
    "verified_first_name": "...",
    "verified_last_name": "...",
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
                "alternative_first_name": "",
                "alternative_last_name": "",
                "verification_reasoning": "unable to verify"
            }

        logger.debug(f"Verification: {verification['verified_first_name']} {verification['verified_last_name']} ({verification['verified_confidence']})")
        
        return {
            "verified_confidence": verification["verified_confidence"],
            "decision_maker_first_name": verification["verified_first_name"],
            "decision_maker_last_name": verification["verified_last_name"],
            "alternative_first_name": verification["alternative_first_name"],
            "alternative_last_name": verification["alternative_last_name"],
            "verification_reasoning": verification["verification_reasoning"],
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
    if verified_confidence in ["high", "medium"]:
        logger.info(f"Verified {verified_confidence} confidence, sending to END")
        return "end"
    else:
        logger.info(f"Verified low confidence, attempting About-page enrichment")
        return "fetch_about_page"

def fetch_about_page(state: LeadState) -> dict:
    """Fetch and filter About page for owner names."""
    combined_text = state["combined_source_text"]
    cost = state["cost"]
    
    # Extract domain from top result (already ranked)
    import re
    urls = re.findall(r'URL: (https?://[^\s]+)', combined_text)
    
    if not urls:
        logger.debug("No URLs found for About-page fetch")
        return {"about_page_text": ""}
    
    top_url = urls[0]
    
    # Parse domain
    try:
        from urllib.parse import urlparse
        parsed = urlparse(top_url)
        domain = f"{parsed.scheme}://{parsed.netloc}"
    except:
        logger.debug(f"Failed to parse domain from {top_url}")
        return {"about_page_text": ""}
    
    # Try about-page variants
    about_paths = ["/about", "/about-us", "/team", "/about-page"]
    about_text = ""
    
    for path in about_paths:
        try:
            about_url = domain + path
            response = requests.get(about_url, timeout=5)
            if response.status_code == 200:
                about_text = response.text
                logger.debug(f"Fetched {about_url}")
                break
        except:
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
    return {"about_page_text": about_text, "cost": cost}

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
        
        return {
            "decision_maker_first_name": response.decision_maker_first_name,
            "decision_maker_last_name": response.decision_maker_last_name,
            "confidence": response.confidence,
            "cost": cost
        }
    except Exception as e:
        logger.error(f"About-page extraction failed: {type(e).__name__}: {e}")
        cost.haiku_errors.append(f"About-page error: {str(e)}")
        return {"confidence": "low", "cost": cost}

def flag_for_review(state: LeadState) -> dict:
    logger.info("→ No owner found, flagged for manual review")
    return {}

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
graph.add_node("combine_and_score", combine_and_score)
graph.add_node("extract_owner", extract_owner)
graph.add_node("verify_owner", verify_owner)
graph.add_node("fetch_about_page", fetch_about_page)
graph.add_node("extract_owner_from_about", extract_owner_from_about)
graph.add_node("flag_for_review", flag_for_review)

graph.set_entry_point("prep_lead")
graph.add_edge("prep_lead", "build_queries")
graph.add_edge("build_queries", "search_q1")
graph.add_edge("build_queries", "search_q2")
graph.add_edge("build_queries", "search_q3")
graph.add_edge("build_queries", "search_q4")
graph.add_edge("search_q1", "combine_and_score")
graph.add_edge("search_q2", "combine_and_score")
graph.add_edge("search_q3", "combine_and_score")
graph.add_edge("search_q4", "combine_and_score")
graph.add_edge("combine_and_score", "extract_owner")

graph.add_conditional_edges(
    "extract_owner",
    route_after_search,
    {
        "verify_owner": "verify_owner",
        "end": END
    }
)

graph.add_conditional_edges(
    "verify_owner",
    route_after_verification,
    {
        "fetch_about_page": "fetch_about_page",
        "end": END
    }
)
graph.add_edge("fetch_about_page", "extract_owner_from_about")

graph.add_conditional_edges(
    "extract_owner_from_about",
    route_after_about,
    {
        "flag_for_review": "flag_for_review",
        "end": END
    }
)

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


def process_leads_batch(csv_path: str = "MBG_-_Charlotte_Barbershops_-_Charlotte_Barbershops__7_.csv"):
    """Process multiple leads from database and compare to n8n baseline."""
    db = LeadDatabase()
    leads = db.get_all_leads()

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

    for i, lead in enumerate(leads, 1):
        logger.info(f"\n[{i}/{len(leads)}] {lead.business_name}")

        try:
            result = app.invoke({"business_name": lead.business_name, "lead_id": lead.id})

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
                'langgraph_owner': f"{first_name} {last_name}",
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

    # Sample output
    logger.info("Sample results (first 10):")
    for result in results_summary[:10]:
        logger.info(f"  {result['business_name']}: LG={result['langgraph_owner']} ({result['langgraph_confidence']}) vs n8n={result['n8n_owner']} ({result['n8n_confidence']}) → {result['match'].upper()}")


if __name__ == "__main__":
    process_leads_batch()
