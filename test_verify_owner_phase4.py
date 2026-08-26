from typing import Literal
from langchain_anthropic import ChatAnthropic
import json

extract_model = ChatAnthropic(model="claude-haiku-4-5-20251001")
reasoning_model = ChatAnthropic(model="claude-sonnet-5")

ambiguous_combined_text = """[https://www.instagram.com/barkbarbers/] Bark Brothers Co
See Instagram photos and videos from Marcus & David (@barkbarbers)

[https://barkbarbers.com/] Bark Barbers - Premium barbershop
Founded by Marcus Williams and David Chen, Bark Brothers blends traditional craft with modern style.

[https://www.facebook.com/barkbarbers/] Bark Barbers
David Chen and Marcus Williams welcome you to Bark Barbers, your neighborhood barbershop.

[https://yelp.com/biz/bark-barbers-portland] Bark Barbers - Yelp Reviews
Marcus is known for his fade work. Customers praise both Marucs and David for their precision. One review: "David gave me the best haircut I've had in years."

[https://www.google.com/maps/place/bark+barbers] Bark Barbers on Google Maps
Bark Barbers Co. Barber Shop. Business hours vary. Contact David Chen (listed), Marcus Williams (staff).

[https://linkedin.com/company/barkbarbers] Bark Barbers - Linkedin
Marcus Williams - Founder, David Chen - Co-Founder (listed as equal partners)

[https://barkbarbers.com/about] About us - Bark Barbers
We're Marcus and David. Started Bark in 2019 with a vision. Equal partners, equal vision."""

print("\n" + "=" * 70)
print("PHASE 4 TEST: Verify Owner (B -> A Logic)")
print("=" * 70)
print("\nBusiness: Bark Barbers (ambiguous co-ownership)")
print("Expected: Haiku -> medium/low confidence -> triggers verify_owner\n")

haiku_prompt= f"""Extract the owner/founder of this barbershop from the search results.

Business: Bark Barbers
Search results:
{ambiguous_combined_text}

Return JSON:
{{
    "decision_maker_first_name": "...",
    "decision_maker_last_name": "...",
    "title": "Owner|Founder|Co-Founder",
    "confidence": "high|medium|low",
    "why_this_person": "..."
}}

Be honest about confidence. If multiple peiple are equally mentioned, mark confidence as medium or low."""

response = extract_model.invoke(haiku_prompt)
haiku_text = response.content

print("Checked Haiku extraction:")
print(haiku_text)

try:
    json_start = haiku_text.find('{')
    json_end = haiku_text.rfind('}') + 1
    haiku_json = json.loads(haiku_text[json_start:json_end])
except:
    print("ERROR parsing Haiku response")
    exit(1)

print(f"\n  ->Extracted: {haiku_json['decision_maker_first_name']} {haiku_json['decision_maker_last_name']}")
print(f"    ->Confidence: {haiku_json['confidence']}")
print(f"    ->Title: {haiku_json['title']}")

haiku_confidence = haiku_json['confidence']
if haiku_confidence == 'high':
    print(f"\n->HIGH confidence, would skip verifiction (go to END)")
    exit(0)
else:
    print(f"\n-> {haiku_confidence.upper()} confidence, routing to verify_owner (B -> A logic)\n")

print("=* 70")
print("VERIFY_OWNER NODE (Sonnet re-examination)")
print("= * 70")

sonnet_prompt = f""" You are verifying a decision_maker extraction for a business.

Business: Bark Barbers

Haiku's extraction (confidence: {haiku_confidence}):
- Name: {haiku_json['decision_maker_first_name']} {haiku_json['decision_maker_last_name']}
- Title: {haiku_json['title']}

Search results (ranked by relevance):
{ambiguous_combined_text}

Your task:
1. Does the search text strongly support {haiku_json['decision_maker_first_name']} {haiku_json['decision_maker_last_name']} as the primary owner/founder?
    - Look for explicit mentions: "founder", "owner", "CEO", name + business name in same paragraph
    - Signal strength = explicit mention + title word + business name

2. If NOT strongly supported, find an ALTERNATIVE owner mentioned in the results.
    - Search for other people names + owner/founder keywords
    - Return the second-best candidate if found

3. Return JSON:
{{
    "verified_confidence": "high|medium|low",
    "verified_first_name": "...",
    "verified_last_name": "...",
    "alternative_first_name": "... or empty string",
    "alternative_last_name": "... or empty string",
    "verification_reasoning": "1-2 sentences"
}}

Example output if alternative found (Option B):
{{
    "verified_confidence": "medium",
    "verified_first_name": "Marcus",
    "verified_last_name": "Williams",
    "alternative_first_name": "David",
    "alternative_last_name": "Chen",
    "verification_reasoning": "Both Marcus and David are explicitly mentioned as co-founders and equal partners. Marcus appears slightly more in some results, but David is equally prominent as decision-maker."
}}

Example output if no Alternative (Option A -> downgrade):
{{
    "verified_confidence": "low",
    "verified_first_name": "Marcus",
    "verified_last_name": "Williams",
    "alternative_first_name": "",
    "alternative_last_name": "",
    "verification_reasoning": "Multiple people mentioned but none conclusively identified as primary decision-maker. Ambiguous co-ownership."
}}"""

sonnet_response = reasoning_model.invoke(sonnet_prompt)
sonnet_text = sonnet_response.content

print("Checked Sonnet verification:")
print(sonnet_text)

try:
    json_start =sonnet_text.find('{')
    json_end = sonnet_text.rfind('}') + 1
    sonnet_json = json.loads(sonnet_text[json_start:json_end])
except:
    print("\nERROR parsing Sonnet response")
    exit(1)

print(f"\n -> Verified: {sonnet_json['verified_first_name']} {sonnet_json['verified_last_name']} (OPTION B)")
print(f" -> Verified Confidence: {sonnet_json['verified_confidence']}")
if sonnet_json['alternative_first_name']:
    print(f" -> Alternative Found: {sonnet_json['alternative_first_name']} {sonnet_json['alternative_last_name']} (OPTION B)")
else:
    print(f" -> No Alternative Found (Option A -> downgrade to 'low')")

print(f" -> Reasoning: {sonnet_json['verification_reasoning']}")

print("\n" + "=" * 70)
print("FINAL ROUTING (after verification)")
print("=" * 70)

verified_confidence =sonnet_json['verified_confidence']
if verified_confidence in ['high', 'medium']:
    print(f"\ncheck Verified confidence = {verified_confidence.upper()}")
    print(" -> Route to END (decision-maker approved)")
else:
    print(f"\n^ Verified confidence = {verified_confidence.upper()}")
    print(" -> Route to flag_for_review (manual intervention needed)")

print("\n" + "=" * 70)
print("PHASE 4 TEST COMPLETE")
print("=" * 70)