"""
Step 5: Personalization
Nick Saraev cold-email framework:
1. personalization
2. who am I / why it matters
3. offer
4. CTA

This module turns enriched lead context into a constrained set of variables that
slot into one fixed campaign sequence.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import anthropic
import structlog
from dotenv import load_dotenv

from execution.campaign_strategy import summarize_doctrine_for_prompt
from execution.copy_constraints import CONSTRAINT_CONFIGS, DEFAULT_CONSTRAINT_ID
from execution.qa_check import (
    BANNED_AI_PHRASES,
    BANNED_AI_WORDS,
    BANNED_OUTREACH_PHRASES,
    BANNED_PRAISE_STARTS,
    BANNED_TRANSITIONS,
    COLLECTIVE_VOICE_WORDS,
    FORMAL_EXPANSIONS,
)

load_dotenv()
log = structlog.get_logger()

CLAUDE_MODEL = "claude-sonnet-4-6"
CLAUDE_COST_PER_LEAD = 0.004
MAX_PARALLEL_CLAUDE = 15
BATCH_SIZE = 4
MAX_REGEN_ATTEMPTS = 3
ANTHROPIC_TIMEOUT_SECONDS = float(os.getenv("ANTHROPIC_TIMEOUT_SECONDS", "90"))

VARIABLE_KEYS = [
    "signal_topic",
    "personalization_line",
    "identity_line",
    "who_i_help",
    "problem_i_fix",
    "bottleneck_trigger",
    "offer_observation",
    "offer_scope",
    "offer_payoff",
    "followup_ping",
    "micro_insight",
    "proof_problem",
    "proof_fix",
    "proof_payoff",
]

# Prefixes that make personalization_line read as a fragment or as AI
# scaffolding. Centralized so every check site stays in sync.
# - with / for / as: grammatical fragment openers (verb-less noun phrases)
# - including / such as / particularly: list-continuation words treated as
#   sentence subjects when Perplexity returned a list like "includes design,
#   animation, ..." — our extraction yanks "including design" and stuffs
#   it into the email (2026-04-20 post-production prospect incident)
# - based on / according to: Perplexity reasoning scaffolding that leaked
#   into one prospect's opener ("based on the search results...")
BANNED_PERSONALIZATION_PREFIXES = (
    "with ",
    "for ",
    "as ",
    "including ",
    "such as ",
    "particularly ",
    "based on ",
    "according to ",
)

LLM_GENERATED_KEYS = [
    "signal_topic",
    "personalization_line",
    "who_i_help",
    "problem_i_fix",
    "bottleneck_trigger",
    "offer_observation",
    "offer_payoff",
]

STOPWORDS = {
    "a", "about", "after", "again", "all", "also", "an", "and", "any", "are", "as", "at",
    "be", "because", "been", "before", "between", "both", "but", "by", "can", "company",
    "do", "does", "for", "from", "get", "gets", "got", "had", "has", "have", "how", "if",
    "in", "into", "is", "it", "its", "just", "like", "more", "most", "new", "not", "of",
    "on", "once", "one", "or", "our", "out", "over", "same", "so", "some", "still", "team",
    "that", "the", "their", "them", "then", "there", "they", "this", "to", "up", "us",
    "usually", "very", "was", "we", "what", "when", "where", "which", "who", "with", "your",
}

INSIDE_LANGUAGE_CANDIDATES = {
    "performance_marketing_agency": [
        "paid media",
        "paid social",
        "paid search",
        "client delivery",
        "new business",
        "pipeline",
        "replies",
        "handoffs",
        "ownership",
        "routing",
    ],
    "b2b_saas": [
        "demo flow",
        "speed to lead",
        "warm replies",
        "routing",
        "ownership",
        "handoffs",
        "pipeline",
        "inbound",
        "outbound",
        "queue",
    ],
    "generic": [
        "follow up",
        "replies",
        "ownership",
        "routing",
        "handoffs",
        "pipeline",
    ],
}

AGENCY_CONTEXT_ANCHORS = [
    (["experiential", "activation", "activations", "trade show", "tradeshow", "events"], "live activation work"),
    (["public relations", " pr ", "pr "], "monthly pr delivery"),
    (["seo", "paid media", "ppc", "paid social", "paid search", "digital advertising", "media buying", "google ads", "meta ads"], "campaign delivery"),
    (["web", "website", "design", "development", "dev", "creative", "branding", "brand"], "design and build delivery"),
    (["fulfillment", "merchandise", "promotional", "promo", "print", "production"], "production heavy delivery"),
]

SOURCE_OBSERVATION_PATTERNS = {
    "performance_marketing_agency": [
        (r"\bmonth[\s-]?to[\s-]?month contracts?\b", "month to month contracts"),
        (r"\bperformance[\s-]?based (?:publicity|pr|marketing)\b", "performance based pr"),
        (r"\badvertising\s*(?:and|&)\s*design\b", "advertising and design"),
        (r"\bcreatively[\s-]?led agency\b", "creatively led agency"),
        (r"\bhubspot certified diamond partner\b|\bhubspot diamond partner\b|\bdiamond partner\b", "hubspot diamond partner"),
        (r"\bpaid media\b|\bpaid social\b|\bpaid search\b|\bppc\b", "paid media delivery"),
        (r"\bguaranteed digital and published stories\b", "guaranteed stories"),
        (r"\bguarantee(?:d)? results?\b|\bguaranteed campaigns?\b", "guaranteed results"),
        (r"\bintegrated b2b agency\b|\bb2b agency\b", "integrated b2b agency"),
        (r"\bfull[\s-]?service (?:digital )?(?:marketing|advertising|creative|agency)?\b", "full service delivery"),
        (r"\bdigital marketing agency\b", "digital marketing agency"),
        (r"\bpublic relations\b|\bpublicity\b", "pr delivery"),
        (r"\bmonthly retainers?\b", "monthly retainers"),
        (r"\bmarketing execution and fulfillment\b", "execution heavy delivery"),
        (r"\bcorporate gifting\b|\bbranded merchandise\b|\bpromotional products\b", "production heavy delivery"),
        (r"\bbranded merchandise\b\s+(?:and|&)\s+\bcorporate gifting\b", "branded merchandise and corporate gifting"),
        (r"\bpromotional products\b\s+(?:and|&)\s+\bbranded merchandise\b", "promotional products and branded merchandise"),
        (r"\bpromotional marketing\b|\bdirect importing\b|\btrade shows?\b|\bspecial events?\b|\bspecial event promotion\b|\bemployee recognition\b|\bclient gifts?\b", "promotional marketing"),
        (r"\bthird generation\b|\bfamily[\s-]?owned\b|\boldest independent printer\b|\bprinter\b|\bprinting\b", "print production"),
        (r"\bexperiential marketing\b|\bevent consultation\b|\bactivations?\b", "experiential delivery"),
        (r"\bvideo production\b|\bweb design\b|\bgraphic\b|\bcreative marketing\b", "creative delivery"),
        (r"\bsocial media marketing\b|\bsocial media agency\b", "social media agency"),
        (r"\bvisual communications\b", "visual communications"),
    ],
    "b2b_saas": [
        (r"\bspeed to lead\b", "speed to lead"),
        (r"\bshared queue\b|\bshared inbox\b", "shared queue"),
        (r"\bproduct led\b", "product led growth"),
        (r"\bdemo\b", "demo flow"),
        (r"\btrial\b", "trial flow"),
        (r"\binbound\b", "inbound replies"),
        (r"\boutbound\b", "outbound replies"),
    ],
}

AGENCY_FOLLOWUP_BY_ANCHOR = {
    "monthly pr delivery": "new business follow up usually starts slipping once PR delivery gets heavy",
    "campaign delivery": "reply ownership usually starts drifting once campaign delivery gets heavy",
    "design and build delivery": "new business follow up usually gets pushed back once projects stack up",
    "production heavy delivery": "reply ownership usually gets fuzzy once production work piles up",
    "live activation work": "new business follow up usually starts slipping once event work piles up",
    "client delivery": "new business follow up usually starts slipping once delivery weeks stack up",
}

AGENCY_PROOF_KEYWORDS = {
    "monthly pr delivery": ["pr", "publicity", "month to month"],
    "campaign delivery": ["paid media", "campaign", "reporting"],
    "design and build delivery": ["website", "project", "full service", "design", "branding", "creative"],
    "production heavy delivery": ["production", "shared inbox", "merchandise", "fulfillment"],
    "live activation work": ["event", "activation", "onboarded", "onboarding"],
    "client delivery": ["client", "delivery", "new business", "social media", "content"],
}

SIGNAL_TOPIC_POOLS = {
    "pr": ["pr follow up", "reply ownership", "new business lag"],
    "paid_media": ["campaign reply lag", "reply routing", "pipeline ownership"],
    "social_media": ["reply handling", "follow up gaps", "pipeline ownership"],
    "print_promo": ["production follow up", "reply routing", "new business lag"],
    "full_service_creative": ["follow up ownership", "reply handling", "project pipeline"],
    "creative_studio": ["follow up ownership", "reply handling", "pipeline lag"],
    "activation": ["event follow up", "reply ownership", "pipeline lag"],
    "merch_gifting": ["fulfillment follow up", "reply ownership", "pipeline lag"],
    "execution_fulfillment": ["follow up ownership", "reply routing", "pipeline lag"],
    "digital_marketing": ["follow up ownership", "reply routing", "pipeline drift"],
    "b2b_agency": ["pipeline follow up", "reply ownership", "follow up lag"],
    "post_production": ["project follow up", "reply handling", "pipeline lag"],
    "generic_agency": ["follow up ownership", "reply handling", "pipeline lag"],
}

SPECIALIZATION_PROBLEM_I_FIX = {
    "pr": "reply handling and follow up leaks",
    "paid_media": "reply routing and handoff leaks",
    "activation": "follow up ownership and handoff leaks",
    "print_promo": "reply routing and ownership leaks",
    "merch_gifting": "follow up routing and handoff leaks",
    "social_media": "reply handling and ownership leaks",
    "digital_marketing": "reply routing and follow up leaks",
    "full_service_creative": "reply handling and handoff leaks",
    "creative_studio": "reply handling and handoff leaks",
    "b2b_agency": "reply handling and routing leaks",
    "execution_fulfillment": "follow up routing and handoff leaks",
    "post_production": "reply handling and project handoff leaks",
    "generic_agency": "reply handling and handoff leaks",
}

SPECIALIZATION_BOTTLENECK_TRIGGER = {
    "pr": "pr delivery gets heavy and follow up starts slipping",
    "paid_media": "campaign delivery starts pulling attention from new business replies",
    "activation": "event work piles up and follow up gets squeezed",
    "print_promo": "production work stacks up and reply handling slips",
    "merch_gifting": "fulfillment gets heavy and new business follow up stalls",
    "social_media": "client content delivery stacks up and replies slip",
    "digital_marketing": "client delivery stacks up and new business follow up drifts",
    "full_service_creative": "project delivery starts crowding out new business follow up",
    "creative_studio": "project work stacks up and new business follow up slips",
    "b2b_agency": "delivery work stacks up and pipeline follow up slips",
    "execution_fulfillment": "fulfillment work gets heavy and follow up starts depending on memory",
    "post_production": "edit and finishing cycles fill the week and new business follow up slips",
    "generic_agency": "delivery gets busy and follow up starts depending on memory",
}

SPECIALIZATION_OFFER_SCOPE = {
    "paid_media": "reply routing flow",
    "print_promo": "reply ownership flow",
    "social_media": "reply handling flow",
    "digital_marketing": "follow up ownership flow",
    "b2b_agency": "pipeline follow up flow",
    "post_production": "project handoff and follow up flow",
}

# Each specialization now maps to a LIST of micro-insight variants. We hash the
# lead's email into the list so two leads in the same specialization rarely get
# the same Email 2 insight. Previously each specialization had one string,
# which meant e.g. all 80 paid_media leads in a 500-lead send received
# identical copy on Email 2.
AGENCY_MICRO_INSIGHTS = {
    "pr": [
        "the gap shows up between a warm reply and the first human follow up",
        "pitching is sharp but reply handoff usually cools off fast",
        "earned media lands in the inbox but ownership on it usually drifts",
        "journalists get prompt follow up, warm prospects usually don't",
    ],
    "paid_media": [
        "most agencies have campaign delivery dialed but reply ownership runs on memory",
        "media plans hit deadline every week, new business replies usually don't",
        "performance dashboards get checked daily but reply queues don't have the same rhythm",
        "account managers are on top of campaigns, warm replies sit in a shared inbox",
    ],
    "activation": [
        "event timelines are tight but follow up after usually has no owner",
        "on-site logistics get attention, warm leads from site usually go quiet",
        "tear down is planned but the follow up workflow for inquiries isn't",
        "staffing ramps for the event and drops the pipeline it creates",
    ],
    "print_promo": [
        "the production floor runs on systems but new business follow up doesn't",
        "orders have order numbers but warm replies just have a name",
        "print runs hit deadlines but inbound quote requests wait days",
        "fulfillment has SOPs, new business inquiries usually don't",
    ],
    "full_service_creative": [
        "project delivery usually has a process but follow up between projects doesn't",
        "creative briefs get managed tightly, warm inbound doesn't enter the same system",
        "client revisions have a pipeline, new business threads live in email",
        "production runs on milestones, inbound replies wait for a free afternoon",
    ],
    "creative_studio": [
        "creative work usually has a process but follow up between projects doesn't",
        "rounds of revisions are tracked, new business threads usually aren't",
        "design sprints have clear handoffs, inbound inquiries don't have the same ownership",
    ],
    "social_media": [
        "content calendars are planned weeks ahead but reply handling usually isn't",
        "posting cadence is locked but dm follow up runs on whoever is online",
        "analytics reviews are scheduled weekly, new business replies usually aren't",
    ],
    "digital_marketing": [
        "campaigns get scheduled but new business follow up still depends on memory",
        "seo and content get a plan, warm inbound usually gets whoever has time",
        "paid and organic have owners, new business conversations usually don't",
    ],
    "merch_gifting": [
        "orders ship on time but warm replies sit until someone notices",
        "sample requests move fast, bigger inquiries wait a few days",
        "production tracks every sku but new business follow up doesn't",
    ],
    "execution_fulfillment": [
        "fulfillment runs on deadlines but new business follow up usually doesn't",
        "sla dashboards exist for client work, warm inbound goes through email",
        "operations have checklists but the business development pipeline usually doesn't",
    ],
    "b2b_agency": [
        "client delivery usually has a system but new business follow up doesn't",
        "retention gets a playbook, new pipeline usually runs on memory",
        "account reviews are structured, inbound sales conversations usually aren't",
    ],
    "post_production": [
        "edit bays run on tight schedules but new business replies usually don't",
        "finishing and color grading have deadlines, warm inbound inquiries don't",
        "project producers track every cut, new business threads live in email",
        "vfx and animation reviews get owners, inbound pitches usually don't",
    ],
    "generic_agency": [
        "client delivery usually has a process but follow up between projects doesn't",
        "most process energy goes into keeping clients happy, new business replies don't",
        "delivery teams have structure, sales-side replies live in a shared inbox",
    ],
}

GENERIC_OBSERVATIONS = {
    "client delivery",
    "delivery",
    "live activation work",
    "production heavy delivery",
    "campaign delivery",
    "design and build delivery",
    "creative delivery",
    "execution heavy delivery",
    "experiential delivery",
    "print production",
    "11-50",
    "51-200",
    "2-10",
    "1-10",
    "10-50",
}

PERSONALIZATION_VERBS = {
    "means",
    "mean",
    "puts",
    "put",
    "pulls",
    "pull",
    "leaves",
    "leave",
    "starts",
    "start",
    "gets",
    "get",
    "keeps",
    "keep",
    "slows",
    "slow",
    "pushes",
    "push",
}

POINT_WORDS = {"usually", "tends", "often", "means", "leaves", "puts", "pulls", "starts", "gets", "keeps", "slows", "pushes"}

SOURCE_PHRASE_WEIGHTS = [
    ("month to month contracts", 8),
    ("guaranteed digital and published stories", 8),
    ("performance based", 7),
    ("public relations", 7),
    ("publicity", 7),
    ("advertising and design", 7),
    ("creatively led agency", 7),
    ("hubspot diamond partner", 6),
    ("marketing execution and fulfillment", 6),
    ("promotional products", 6),
    ("branded merchandise", 6),
    ("corporate gifting", 6),
    ("promotional marketing", 6),
    ("social media", 6),
    ("digital marketing agency", 6),
    ("full service digital marketing", 7),
    ("paid media", 6),
    ("paid search", 6),
    ("paid social", 6),
    ("ppc", 6),
    ("trade show", 5),
    ("special events", 5),
    ("activation", 5),
    ("experiential", 5),
    ("visual communications", 5),
    ("printing", 5),
    ("special event promotion", 5),
    ("full funnel marketing", 5),
    ("print", 4),
    ("oldest independent printer", 7),
    ("video production", 4),
    ("web design", 4),
    ("creative marketing", 4),
    ("full service", 4),
    ("fractional cmo", 3),
]

BAD_SOURCE_PHRASES = {
    "it entertains",
    "want to talk",
    "what's your frequency",
    "innovative startups",
    "turn up the volume on your marketing",
    "exceptionally brave people",
    "who also happen to be kind",
    "results to prove it",
}

SERVICE_SIGNAL_WORDS = {
    "activation",
    "advertising",
    "agency",
    "branding",
    "campaign",
    "creative",
    "delivery",
    "design",
    "digital",
    "event",
    "experiential",
    "fulfillment",
    "gifting",
    "marketing",
    "media",
    "merchandise",
    "paid",
    "ppc",
    "print",
    "printer",
    "production",
    "promo",
    "promotional",
    "pr",
    "publicity",
    "search",
    "seo",
    "shop",
    "shops",
    "social",
    "video",
    "web",
    "website",
}


def check_lead_eligibility(lead: dict) -> bool:
    """Exclude enterprise exec-chair/board profiles."""
    company_size = str(lead.get("linkedin_company_size") or lead.get("company_size") or "")
    title = (lead.get("title") or "").lower()

    is_enterprise = any(size in company_size for size in ["1000", "5000", "10000", "10001"])
    exec_titles = ["chairman", "board", "executive chairman", "chief people", "chief human"]
    is_exec = any(token in title for token in exec_titles)
    return not (is_enterprise and is_exec)


def _classify_lane(lead: dict) -> str:
    context = " ".join(
        [
            str(lead.get("title") or ""),
            str(lead.get("linkedin_headline") or ""),
            str(lead.get("perplexity_company_context") or ""),
            str(lead.get("company_industry") or ""),
            str(lead.get("company_name") or ""),
        ]
    ).lower()

    agency_keywords = [
        "agency",
        "paid media",
        "ppc",
        "paid search",
        "paid social",
        "media buying",
        "performance marketing",
        "demand gen agency",
        "lead gen agency",
        "marketing & advertising",
        "public relations",
        "creative agency",
        "digital marketing",
        "social media",
        "branding",
        "promotional marketing",
        "promotional products",
        "branded merchandise",
        "corporate gifting",
        "marketing execution",
        "fulfillment",
        "trade show",
        "experiential",
        "visual communications",
    ]
    saas_keywords = [
        "saas",
        "software",
        "platform",
        "computer software",
        "software development",
        "product led",
        "b2b software",
    ]

    if any(keyword in context for keyword in agency_keywords):
        return "performance_marketing_agency"
    if any(keyword in context for keyword in saas_keywords):
        return "b2b_saas"
    return "generic"


def extract_primitives(lead: dict) -> dict:
    title = (lead.get("title") or "").lower()
    headline = lead.get("linkedin_headline") or ""

    if any(token in title for token in ["ops", "operations", "coo", "revops", "revenue operations"]):
        role_bucket = "ops"
    elif any(token in title for token in ["growth", "demand gen", "marketing director", "vp marketing"]):
        role_bucket = "growth"
    elif any(token in title for token in ["founder", "ceo", "owner", "partner", "president", "managing"]):
        role_bucket = "founder"
    else:
        role_bucket = "other"

    lane = _classify_lane(lead)
    headline_lower = headline.lower()
    confidence = "high" if len(headline) > 20 and headline_lower != title else "low"
    return {"role_bucket": role_bucket, "lane": lane, "confidence": confidence}


def _get_strategy(strategy: dict | None) -> dict:
    return strategy or {}


def _get_qa_thresholds(strategy: dict | None) -> dict:
    current = _get_strategy(strategy)
    return current.get(
        "qa_thresholds",
        {
            "email1_max_words": 110,
            "email2_max_words": 55,
            "email3_max_words": 70,
            "personalization_line_max_words": 18,
            "who_i_help_max_words": 6,
            "problem_i_fix_max_words": 7,
            "bottleneck_trigger_max_words": 13,
            "offer_observation_max_words": 16,
            "offer_scope_max_words": 8,
            "offer_payoff_max_words": 14,
            "followup_ping_max_words": 15,
            "proof_problem_max_words": 14,
            "proof_fix_max_words": 7,
            "proof_payoff_max_words": 12,
            "signal_topic_max_words": 4,
        },
    )


def _get_constraint_library(strategy: dict | None) -> dict:
    current = _get_strategy(strategy)
    constraint_ids = current.get("selectors", {}).get("constraint_ids") or list(CONSTRAINT_CONFIGS.keys())
    return {key: CONSTRAINT_CONFIGS[key] for key in constraint_ids if key in CONSTRAINT_CONFIGS} or dict(CONSTRAINT_CONFIGS)


def _get_default_constraint_id(strategy: dict | None, constraint_library: dict | None = None) -> str:
    current = _get_strategy(strategy)
    library = constraint_library or _get_constraint_library(current)
    default_constraint_id = current.get("selectors", {}).get("default_constraint_id")
    if default_constraint_id in library:
        return default_constraint_id
    return next(iter(library.keys()), DEFAULT_CONSTRAINT_ID)


def choose_constraint(primitives: dict, constraint_library: dict | None = None) -> str:
    library = constraint_library or dict(CONSTRAINT_CONFIGS)
    best_id = None
    best_score = -1
    for constraint_id, constraint in library.items():
        triggers = constraint.get("triggers", {})
        lane_match = primitives.get("lane") in triggers.get("lane", [])
        role_match = primitives.get("role_bucket") in triggers.get("role_bucket", [])
        if not lane_match and not role_match:
            continue

        match_count = int(lane_match) + int(role_match)
        trigger_breadth = len(triggers.get("lane", [])) + len(triggers.get("role_bucket", []))
        specificity = 100 - trigger_breadth
        score = match_count * 1000 + specificity
        if score > best_score:
            best_score = score
            best_id = constraint_id

    return best_id or DEFAULT_CONSTRAINT_ID


def _select_copy_selector(lead: dict, primitives: dict) -> str:
    if primitives.get("lane") == "performance_marketing_agency":
        return "agency_reference_group"
    if primitives.get("lane") == "b2b_saas":
        return "saas_reference_group"
    if primitives.get("role_bucket") == "ops":
        return "ops_authority"
    if lead.get("perplexity_company_context"):
        return "context_first"
    return "observation_first"


def _lead_context_text(lead: dict) -> str:
    return " ".join(
        [
            str(lead.get("title") or ""),
            str(lead.get("linkedin_headline") or ""),
            str(lead.get("linkedin_job_description") or ""),
            str(lead.get("perplexity_company_context") or ""),
            str(lead.get("company_description") or ""),
            str(lead.get("company_name") or ""),
        ]
    ).lower()


def _choose_agency_context_anchor(lead: dict) -> str:
    context = f" {_lead_context_text(lead)} "
    scores = {
        "monthly pr delivery": 0,
        "campaign delivery": 0,
        "design and build delivery": 0,
        "production heavy delivery": 0,
        "live activation work": 0,
    }

    def add(anchor: str, *keywords: str) -> None:
        scores[anchor] += sum(1 for keyword in keywords if keyword in context)

    add(
        "monthly pr delivery",
        "public relations",
        "performance based publicity",
        "performance based pr",
        "publicity",
        "month to month contracts",
        "guaranteed digital and published stories",
        "guaranteed stories",
    )
    add(
        "campaign delivery",
        "paid media",
        "paid search",
        "paid social",
        "ppc",
        "digital advertising",
        "media buying",
        "google ads",
        "meta ads",
    )
    add(
        "production heavy delivery",
        "marketing execution and fulfillment",
        "fulfillment",
        "promotional products",
        "branded merchandise",
        "corporate gifting",
        "printing",
        "printer",
        "print",
        "production",
        "visual communications",
        "company webstores",
        "recognition programs",
    )
    add(
        "live activation work",
        "experiential",
        "activation",
        "activations",
        "trade show",
        "tradeshow",
        "special events",
        "event consultation",
        "event services",
    )
    add(
        "design and build delivery",
        "social media",
        "digital marketing agency",
        "full service",
        "web design",
        "website",
        "development",
        "creative",
        "branding",
        "brand",
        "video production",
    )

    if scores["production heavy delivery"] and any(
        marker in context for marker in ["branded merchandise", "corporate gifting", "promotional products", "marketing execution and fulfillment"]
    ):
        scores["production heavy delivery"] += 2

    if scores["live activation work"] and any(
        marker in context for marker in ["trade show", "special events", "experiential", "activation"]
    ):
        scores["live activation work"] += 2

    best_anchor, best_score = max(scores.items(), key=lambda item: item[1])
    return best_anchor if best_score > 0 else "client delivery"


def _extract_source_observations(lead: dict, lane: str) -> list[str]:
    observations: list[str] = []
    combined = " ".join(
        [
            str(lead.get("title") or ""),
            str(lead.get("linkedin_headline") or ""),
            str(lead.get("linkedin_job_description") or ""),
            str(lead.get("perplexity_company_context") or lead.get("company_description") or ""),
        ]
    ).lower()

    for pattern, phrase in SOURCE_OBSERVATION_PATTERNS.get(lane, []):
        match = re.search(pattern, combined, re.IGNORECASE)
        if not match:
            continue
        raw_phrase = re.sub(r"\s+", " ", match.group(0).strip(" .,:;()")).lower()
        if raw_phrase and raw_phrase not in observations:
            observations.append(raw_phrase)
        if phrase not in observations:
            observations.append(phrase)

    if lane == "performance_marketing_agency":
        anchor = _choose_agency_context_anchor(lead)
        if anchor not in observations:
            observations.append(anchor)

    company_size = str(lead.get("linkedin_company_size") or lead.get("company_size") or "").strip()
    if company_size and company_size not in observations:
        observations.append(company_size)

    return observations[:6]


def _looks_like_title_snippet(text: str) -> bool:
    cleaned = re.sub(r"\s+", " ", (text or "").strip().lower())
    if not cleaned:
        return True
    if "@" in cleaned or cleaned.startswith(("founder ", "owner ", "ceo ", "president ")):
        return True
    if re.search(r"\b(founder|owner|president|partner|cofounder|ceo|coo|head)\b", cleaned):
        if cleaned.count(" at ") >= 1 and len(cleaned.split()) <= 12:
            return True
    if re.search(r"\b(founder|owner|president|partner|cofounder|ceo|coo|head)\b", cleaned) and len(cleaned.split()) <= 8:
        service_terms = {
            "pr", "publicity", "media", "marketing", "branding", "events", "event", "trade", "printer",
            "printing", "social", "seo", "ppc", "paid", "fulfillment", "merch", "gifting", "consulting",
            "agency", "creative",
        }
        if not any(term in cleaned.split() for term in service_terms):
            return True
    return False


def _clean_source_phrase(text: str) -> str:
    cleaned = re.sub(r"\*+", "", (text or "").lower())
    cleaned = re.sub(r"\[[0-9]+\]", "", cleaned)
    cleaned = cleaned.replace("&", " and ")
    cleaned = cleaned.replace("-", " ")
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .,:;()'\"")
    best_term = ""
    best_weight = 0
    for term, weight in SOURCE_PHRASE_WEIGHTS:
        if term in cleaned and weight > best_weight:
            best_term = term
            best_weight = weight
    if best_term and len(cleaned.split()) > len(best_term.split()) + 3:
        cleaned = best_term
    return cleaned


def _has_service_signal(text: str) -> bool:
    words = set(re.findall(r"[a-zA-Z][a-zA-Z0-9]+", (text or "").lower()))
    return any(word in words for word in SERVICE_SIGNAL_WORDS)


def _observation_matches_specialization(observation: str, specialization: str) -> bool:
    observation = (observation or "").lower()
    if not observation or observation in BAD_SOURCE_PHRASES:
        return False

    if specialization == "pr":
        return bool(re.search(r"\b(pr|publicity|public relations|stories|contracts|retainers)\b", observation))
    if specialization == "paid_media":
        return bool(re.search(r"\b(ppc|paid|search|social|media|funnel)\b", observation))
    if specialization == "activation":
        return bool(re.search(r"\b(event|events|activation|activations|experiential|trade show|promotional marketing)\b", observation))
    if specialization == "execution_fulfillment":
        return bool(re.search(r"\b(execution|fulfillment|direct mail|marketing execution)\b", observation))
    if specialization == "merch_gifting":
        return bool(re.search(r"\b(merchandise|gifting|promotional products|promo)\b", observation))
    if specialization == "print_promo":
        return bool(re.search(r"\b(print|printer|printing|production|visual communications|promo)\b", observation))
    if specialization == "post_production":
        return bool(re.search(r"\b(post production|post-production|editing|vfx|animation|color grading|finishing|visual effects|motion design)\b", observation))
    if specialization == "social_media":
        return bool(re.search(r"\b(social media|content|community)\b", observation))
    if specialization == "digital_marketing":
        return bool(re.search(r"\b(digital marketing|seo|website|web design|fractional cmo|marketing agency)\b", observation))
    if specialization == "b2b_agency":
        return bool(re.search(r"\b(b2b|hubspot|demand gen|revenue)\b", observation))
    if specialization == "full_service_creative":
        return bool(re.search(r"\b(full service|creative|advertising|design|branding|video|web design)\b", observation))
    return _has_service_signal(observation)


def _score_source_phrase(text: str) -> int:
    cleaned = _clean_source_phrase(text)
    if not cleaned:
        return -100

    score = 0
    for term, weight in SOURCE_PHRASE_WEIGHTS:
        if term in cleaned:
            score = max(score, weight)

    if _looks_like_title_snippet(cleaned):
        score -= 10
    if cleaned in BAD_SOURCE_PHRASES or cleaned.startswith(("it ", "they ", "we ", "our ")):
        score -= 10
    if cleaned in GENERIC_OBSERVATIONS:
        score -= 8
    if cleaned.startswith(("welcome to ", "as a leading ", "as the world's ", "the world's ", "called ")):
        score -= 3
    if _has_service_signal(cleaned):
        score += 3
    elif not any(term in cleaned for term, _weight in SOURCE_PHRASE_WEIGHTS):
        score -= 6
    if len(cleaned.split()) <= 2 and not _has_service_signal(cleaned):
        score -= 4
    if len(cleaned.split()) > 10:
        score -= 2
    return score


def _select_primary_observation(observations: list[str], source_snippets: list[str] | None = None) -> str:
    source_snippets = source_snippets or []
    candidates: list[tuple[int, int, str]] = []

    for observation in observations:
        cleaned = _clean_source_phrase(observation)
        if not cleaned or re.fullmatch(r"\d+\s*-\s*\d+|\d+", cleaned):
            continue
        candidates.append((_score_source_phrase(cleaned), 0, cleaned))

    for snippet in source_snippets:
        cleaned = _clean_source_phrase(snippet)
        if not cleaned or re.fullmatch(r"\d+\s*-\s*\d+|\d+", cleaned):
            continue
        candidates.append((_score_source_phrase(cleaned), 1, cleaned))

    if candidates:
        score, source_rank, cleaned = max(candidates, key=lambda item: (item[0], -item[1], -len(item[2])))
        if source_rank == 1:
            for observation in [_clean_source_phrase(item) for item in observations if item]:
                if observation and observation in cleaned and len(observation.split()) <= len(cleaned.split()):
                    if _score_source_phrase(observation) >= max(5, score - 5):
                        cleaned = observation
                        break
        if score > -5:
            return cleaned

    fallback = [_clean_source_phrase(item) for item in observations if item]
    return fallback[0] if fallback else ""


def _split_source_segments(text: str) -> list[str]:
    raw = (text or "").strip().lower()
    if not raw:
        return []

    segments: list[str] = []
    primary_parts = re.split(r"[\n|;/]+", raw)
    for part in primary_parts:
        for segment in re.split(r"\.\s+|,\s+", part):
            cleaned = re.sub(r"\s+", " ", segment).strip(" .,:;()")
            if not cleaned:
                continue
            word_count = len(cleaned.split())
            if 2 <= word_count <= 14 and cleaned not in segments:
                segments.append(cleaned)
    return segments[:18]


def _extract_source_snippets(lead: dict, lane: str, observations: list[str]) -> list[str]:
    matched_snippets: list[str] = []
    backup_snippets: list[str] = []
    sources = [
        lead.get("linkedin_headline") or "",
        lead.get("linkedin_job_description") or "",
        lead.get("perplexity_company_context") or lead.get("company_description") or "",
    ]
    inside_language = INSIDE_LANGUAGE_CANDIDATES.get(lane, INSIDE_LANGUAGE_CANDIDATES["generic"])
    preferred_observations = [item for item in observations if item and item not in GENERIC_OBSERVATIONS]

    for source in sources:
        for segment in _split_source_segments(source):
            if _looks_like_title_snippet(segment):
                continue
            if any(obs in segment for obs in preferred_observations) or any(term in segment for term in inside_language):
                if segment not in matched_snippets:
                    matched_snippets.append(segment)
            elif 2 <= len(segment.split()) <= 10 and segment not in backup_snippets:
                backup_snippets.append(segment)

    snippets = matched_snippets + [item for item in backup_snippets if item not in matched_snippets]
    return snippets[:8]


def _extract_meaningful_terms(text: str, *, min_len: int = 4) -> list[str]:
    words = re.findall(r"[a-zA-Z][a-zA-Z0-9]+", (text or "").lower())
    terms: list[str] = []
    for word in words:
        if len(word) < min_len or word in STOPWORDS:
            continue
        if word not in terms:
            terms.append(word)
    return terms


def _extract_source_anchor_phrases(lead: dict, lane: str) -> list[str]:
    phrases: list[str] = []
    observations = _extract_source_observations(lead, lane)

    headline = (lead.get("linkedin_headline") or "").strip().lower()
    title = (lead.get("title") or "").strip().lower()
    context = (lead.get("perplexity_company_context") or lead.get("company_description") or "").strip().lower()

    for observation in observations:
        lowered = observation.strip().lower()
        if lowered and lowered not in phrases:
            phrases.append(lowered)

    for source in [title, headline]:
        if source:
            for term in _extract_meaningful_terms(source, min_len=3)[:4]:
                if term not in phrases:
                    phrases.append(term)

    if context:
        for term in _extract_meaningful_terms(context)[:3]:
            if term not in phrases:
                phrases.append(term)

    return phrases[:8]


def _classify_agency_specialization(lead: dict, primary_observation: str, agency_anchor: str) -> str:
    context = _lead_context_text(lead)
    primary = (primary_observation or "").lower()
    anchor = (agency_anchor or "").lower()
    haystack = f"{context} {primary} {anchor}"

    if any(term in haystack for term in ["public relations", "publicity", "performance based pr", "performance based publicity", "month to month contracts"]):
        return "pr"
    # Post-production studios (editing, VFX, animation, color grading, finishing)
    # were previously falling through to generic_agency. Added 2026-04-20 after
    # a 150-person post house got a vanilla "full service creative
    # work" opener — the email worked but completely missed the prospect's actual
    # business.
    if any(term in haystack for term in [
        "post production", "post-production", "post house",
        "editing services", "video editing", "film editing",
        "color grading", "colour grading", "color correction",
        "vfx", "visual effects", "animation studio", "motion design",
        "finishing services", "broadcast design",
    ]):
        return "post_production"
    if any(term in haystack for term in ["marketing execution and fulfillment", "execution heavy delivery", "fulfillment company", "rebate processing"]):
        return "execution_fulfillment"
    if any(term in haystack for term in ["branded merchandise", "corporate gifting", "promotional products", "company webstores", "recognition programs"]):
        return "merch_gifting"
    if any(term in haystack for term in ["print", "printing", "printer", "visual communications", "production floor"]):
        return "print_promo"
    # "activation" alone is ambiguous: "brand activation" and "marketing
    # activation" are strategic/consulting services that never involve live
    # events, whereas "event activation" and "experiential activation" do.
    # A brand consultancy (2026-04-20 prospect-POV audit) hit this branch because
    # their Perplexity context mentioned brand activation, then got routed
    # to event-flavored micro_insights ("event timelines are tight..."),
    # which doesn't match a brand consultancy. Require an unambiguous live-
    # event marker before routing to the activation specialization.
    unambiguous_live_event = any(
        term in haystack
        for term in [
            "trade show",
            "special events",
            "experiential",
            "event services",
            "event consultation",
            "event production",
            "live event",
            "on-site activation",
            "on site activation",
            "street team",
            "pop-up",
            "pop up",
        ]
    )
    if unambiguous_live_event:
        return "activation"
    if "promotional marketing" in haystack and "brand" not in haystack:
        return "activation"
    # Fall through: "brand activation"/"marketing activation" leads continue
    # to later branches (most will land in full_service_creative or
    # creative_studio — much closer to the business).
    if any(term in haystack for term in ["paid media", "paid search", "paid social", "ppc", "digital advertising", "media buying"]):
        return "paid_media"
    if any(term in haystack for term in ["full service", "full-service", "web design", "video production", "creative agency"]):
        return "full_service_creative"
    if "social media" in haystack:
        return "social_media"
    if any(term in haystack for term in ["digital marketing agency", "digital marketing service provider", "fractional cmo"]):
        return "digital_marketing"
    if any(term in haystack for term in ["integrated b2b agency", "b2b agency", "hubspot diamond partner"]):
        return "b2b_agency"
    # Single "creative" or "branding" keyword → creative studio (not full-service)
    if any(term in haystack for term in ["creative", "branding"]):
        return "creative_studio"
    return "generic_agency"


def _choose_agency_followup_ping(specialization: str, agency_anchor: str) -> str:
    if specialization == "pr":
        return "new business follow up usually starts slipping once pr delivery gets heavy"
    if specialization == "activation":
        return "new business follow up usually starts slipping once event work piles up"
    if specialization == "paid_media":
        return "reply ownership usually starts drifting once campaign delivery gets heavy"
    if specialization in {"merch_gifting", "execution_fulfillment"}:
        return "new business follow up usually gets squeezed once fulfillment work piles up"
    if specialization == "print_promo":
        return "reply ownership usually gets fuzzy once production work piles up"
    if specialization == "post_production":
        return "new business follow up usually starts slipping once edit and finishing cycles fill the week"
    if specialization in {"social_media", "digital_marketing", "full_service_creative", "b2b_agency"}:
        return "new business follow up usually gets pushed back once client work stacks up"
    return AGENCY_FOLLOWUP_BY_ANCHOR.get(agency_anchor, AGENCY_FOLLOWUP_BY_ANCHOR["client delivery"])


def _derive_inside_language(lead: dict, lane: str) -> list[str]:
    context = _lead_context_text(lead)
    candidates = INSIDE_LANGUAGE_CANDIDATES.get(lane, INSIDE_LANGUAGE_CANDIDATES["generic"])
    matched = [candidate for candidate in candidates if candidate in context]
    return matched[:6] or candidates[:6]


def _build_lead_dossier(lead: dict, constraint_id: str, strategy: dict | None = None, primitives: dict | None = None) -> dict:
    current = _get_strategy(strategy)
    primitives = primitives or extract_primitives(lead)
    lane = primitives.get("lane", "generic")
    config = _get_constraint_library(current).get(constraint_id, CONSTRAINT_CONFIGS[DEFAULT_CONSTRAINT_ID])

    company_size = str(lead.get("linkedin_company_size") or lead.get("company_size") or "")
    inside_language = _derive_inside_language(lead, lane)
    source_observations = _extract_source_observations(lead, lane)
    source_anchors = _extract_source_anchor_phrases(lead, lane)
    source_snippets = _extract_source_snippets(lead, lane, source_observations)
    primary_observation = _select_primary_observation(source_observations, source_snippets)
    agency_anchor = _choose_agency_context_anchor(lead) if lane == "performance_marketing_agency" else ""
    if agency_anchor and agency_anchor not in source_anchors:
        source_anchors.append(agency_anchor)
    agency_specialization = _classify_agency_specialization(lead, primary_observation, agency_anchor) if lane == "performance_marketing_agency" else ""
    followup_ping = config["deterministic"]["followup_ping"]
    if constraint_id == "agency_delivery_drag":
        followup_ping = _choose_agency_followup_ping(agency_specialization, agency_anchor)

    dossier = {
        "email": lead.get("email", ""),
        "first_name": lead.get("first_name", ""),
        "company_name": lead.get("company_name", ""),
        "title": lead.get("title", ""),
        "company_size": company_size,
        "linkedin_headline": lead.get("linkedin_headline", ""),
        "linkedin_job_description": lead.get("linkedin_job_description", ""),
        "company_context": lead.get("perplexity_company_context") or lead.get("company_description") or "",
        "lane": lane,
        "role_bucket": primitives.get("role_bucket", "other"),
        "copy_selector": lead.get("copy_selector", "observation_first"),
        "constraint_id": constraint_id,
        "constraint_implication": config.get("implication", ""),
        "anchor_terms": list(config.get("anchor_terms", [])),
        "inside_language": inside_language,
        "raw_linkedin_headline": lead.get("linkedin_headline", ""),
        "raw_linkedin_job_description": lead.get("linkedin_job_description", ""),
        "raw_company_context": lead.get("perplexity_company_context") or lead.get("company_description") or "",
        "linkedin_skills": lead.get("linkedin_skills", ""),
        "linkedin_tenure_years": lead.get("linkedin_tenure_years", ""),
        "website_context": lead.get("website_context", ""),
        "linkedin_recent_posts": lead.get("linkedin_recent_posts", ""),
        "source_observations": source_observations,
        "source_snippets": source_snippets,
        "source_anchors": source_anchors,
        "primary_observation": primary_observation,
        "agency_anchor": agency_anchor,
        "agency_specialization": agency_specialization,
        "offer_scope": SPECIALIZATION_OFFER_SCOPE.get(agency_specialization, config["deterministic"]["offer_scope"]),
        "followup_ping": followup_ping,
    }
    return dossier


def _get_proof_variants(constraint_id: str, strategy: dict | None = None) -> list[dict]:
    current = _get_strategy(strategy)
    configured = (current.get("proof_library", {}) or {}).get(constraint_id) or []
    normalized = []
    for item in configured:
        if isinstance(item, dict) and {"problem", "fix", "payoff"} <= set(item.keys()):
            normalized.append(
                {
                    "problem": str(item.get("problem", "")).strip(),
                    "fix": str(item.get("fix", "")).strip(),
                    "payoff": str(item.get("payoff", "")).strip(),
                }
            )
    return normalized or list(CONSTRAINT_CONFIGS.get(constraint_id, {}).get("proof_variants", []))


def _select_proof_variant(constraint_id: str, lead: dict, strategy: dict | None = None) -> dict:
    variants = _get_proof_variants(constraint_id, strategy)
    if not variants:
        return {"problem": "", "fix": "", "payoff": ""}

    narrowed = False
    if constraint_id == "agency_delivery_drag":
        anchor = _choose_agency_context_anchor(lead)
        keywords = AGENCY_PROOF_KEYWORDS.get(anchor, [])
        if keywords:
            matched = []
            for variant in variants:
                haystack = " ".join(
                    [
                        str(variant.get("problem", "")),
                        str(variant.get("fix", "")),
                        str(variant.get("payoff", "")),
                    ]
                ).lower()
                if any(keyword in haystack for keyword in keywords):
                    matched.append(variant)
            if len(matched) >= 3:
                variants = matched
                narrowed = True

        if not narrowed:
            generic_variants = [
                variant
                for variant in variants
                if not re.search(r"\b(pr|publicity|paid media|paid search|website|project)\b", variant.get("problem", ""), re.IGNORECASE)
            ]
            if generic_variants:
                variants = generic_variants

    basis = "|".join(
        [
            str(lead.get("email") or ""),
            str(lead.get("company_name") or ""),
            str(lead.get("title") or ""),
            constraint_id,
        ]
    )
    index = int(hashlib.md5(basis.encode("utf-8")).hexdigest(), 16) % len(variants)
    return dict(variants[index])


_LEADING_CONJUNCTIONS = {"and", "but", "or", "so", "yet"}


def _normalize_line(value: str, lowercase: bool = False) -> str:
    text = (value or "").strip()
    for apo in ("'", chr(0x2018), chr(0x2019)):
        text = re.sub(r"\bit" + re.escape(apo) + r"d\b", "it would", text, flags=re.IGNORECASE)
        text = re.sub(r"\bthat" + re.escape(apo) + r"d\b", "that would", text, flags=re.IGNORECASE)
    text = text.replace("—", " ").replace("–", " ").replace("-", " ")
    text = re.sub(r"\s+", " ", text)
    text = text.rstrip(".!? ")
    # Strip leading conjunctions that create awkward sentence fragments
    words = text.split()
    while words and words[0].lower() in _LEADING_CONJUNCTIONS:
        words = words[1:]
    text = " ".join(words)
    if lowercase or text:
        text = text[:1].lower() + text[1:] if text else text
    return text


def _strip_code_fences(text: str) -> str:
    cleaned = (text or "").strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    return cleaned.strip()


def _parse_json_payload(text: str):
    cleaned = _strip_code_fences(text)
    decoder = json.JSONDecoder()

    for candidate in (
        cleaned,
        cleaned[cleaned.find("{"):] if "{" in cleaned else "",
        cleaned[cleaned.find("["):] if "[" in cleaned else "",
    ):
        candidate = candidate.strip()
        if not candidate:
            continue
        try:
            result, _idx = decoder.raw_decode(candidate)
            return result
        except json.JSONDecodeError:
            continue

    raise json.JSONDecodeError("Unable to parse JSON payload", cleaned, 0)


def _normalize_variables(variables: dict, strategy: dict | None = None) -> dict:
    """
    Normalize case and whitespace on every generated variable. Previously
    this function also POST-HOC TRIMMED each variable to a hardcoded word
    count, which silently chopped Claude's output mid-phrase and produced
    broken sentences ("saved about 4.", "compete with live.", "replies
    get."). We'd spent days patching a blocklist of "dangling" words to
    work around that design — the right fix was to stop chopping and
    treat word counts as prompt-level targets Claude satisfies directly.

    Egregious overages (>2x target) now trigger a WARNING log so we
    notice drift; anything worse is caught by QA and sent through regen
    — which produces coherent shorter copy instead of mechanical cuts.
    """
    out = dict(variables)
    thresholds = _get_qa_thresholds(strategy)

    for key in VARIABLE_KEYS:
        value = out.get(key, "")
        if not value:
            continue
        # Every content variable should be lowercase (the subject and
        # every other variable use this convention).
        out[key] = _normalize_line(value, lowercase=True)

        # Sanity warning only — no chopping. 2x target means something
        # went wrong upstream (prompt drift, regen bug). Visible in logs
        # so operators can act before scale.
        target = thresholds.get(f"{key}_max_words")
        if target and len(out[key].split()) > target * 2:
            log.warning(
                "variable.extreme_overage",
                key=key,
                target_words=target,
                actual_words=len(out[key].split()),
                value_preview=out[key][:120],
            )

    return out


def _convert_legacy_variables(generated: dict, lead: dict, constraint_id: str) -> dict:
    if any(key in generated for key in ["who_i_help", "problem_i_fix", "offer_observation", "offer_payoff"]):
        return dict(generated)

    if not any(key in generated for key in ["identity_line", "offer_line", "benefit_line", "followup_line", "proof_snippet"]):
        return dict(generated)

    config = CONSTRAINT_CONFIGS.get(constraint_id, CONSTRAINT_CONFIGS[DEFAULT_CONSTRAINT_ID])
    fallback = config["fallback_dynamic"]
    deterministic = config["deterministic"]
    proof = _parse_legacy_proof(generated.get("proof_snippet") or "", constraint_id)

    converted = dict(generated)
    converted.setdefault("who_i_help", fallback["who_i_help"])
    converted.setdefault("problem_i_fix", fallback["problem_i_fix"])
    # Legacy identity_line mixed "who i am" with the trigger moment, so it is not safe to
    # reuse for the new bottleneck_trigger field. Use the constraint fallback instead.
    converted.setdefault("bottleneck_trigger", fallback["bottleneck_trigger"])
    converted.setdefault("offer_observation", fallback["offer_observation"])
    # Legacy offer_line mixed the observation/pitch into one field, so it is not safe to
    # reuse as the new offer_scope noun phrase.
    converted.setdefault("offer_scope", deterministic["offer_scope"])
    converted.setdefault("offer_payoff", generated.get("benefit_line") or fallback["offer_payoff"])
    converted.setdefault("followup_ping", generated.get("followup_line") or deterministic["followup_ping"])
    converted.setdefault("proof_problem", proof["problem"])
    converted.setdefault("proof_fix", proof["fix"])
    converted.setdefault("proof_payoff", proof["payoff"])
    return converted


def _parse_legacy_proof(snippet: str, constraint_id: str) -> dict:
    config = CONSTRAINT_CONFIGS.get(constraint_id, CONSTRAINT_CONFIGS[DEFAULT_CONSTRAINT_ID])
    fallback = dict(config["proof_variants"][0])
    text = (snippet or "").strip().rstrip(".")
    if not text:
        return fallback

    lowered = text.lower()
    if " i fixed it with " in lowered and ", which " in lowered:
        parts = re.split(r"\bi fixed it with\b", text, flags=re.IGNORECASE, maxsplit=1)
        if len(parts) == 2:
            problem = parts[0].strip(" .")
            fix_and_payoff = parts[1].strip(" .")
            fix_parts = re.split(r",\s*which\s+", fix_and_payoff, flags=re.IGNORECASE, maxsplit=1)
            if len(fix_parts) == 2:
                return {
                    "problem": problem or fallback["problem"],
                    "fix": fix_parts[0].strip(" .") or fallback["fix"],
                    "payoff": fix_parts[1].strip(" .") or fallback["payoff"],
                }

    sentences = [segment.strip(" .") for segment in re.split(r"(?<=[.!?])\s+", text) if segment.strip()]
    if len(sentences) >= 2:
        problem = sentences[0]
        second = sentences[1]
        if ", which " in second.lower():
            fix, payoff = re.split(r",\s*which\s+", second, flags=re.IGNORECASE, maxsplit=1)
            return {
                "problem": problem or fallback["problem"],
                "fix": fix.strip(" .") or fallback["fix"],
                "payoff": payoff.strip(" .") or fallback["payoff"],
            }
        return {
            "problem": problem or fallback["problem"],
            "fix": second.strip(" .") or fallback["fix"],
            "payoff": fallback["payoff"],
        }

    return fallback


def _contains_anchor_term(text: str, anchor_terms: list[str]) -> bool:
    lower = (text or "").lower()
    return any(term in lower for term in anchor_terms)


def validate_bundle(bundle: dict, lead: dict, constraint_id: str, strategy: dict | None = None) -> tuple[bool, str]:
    config = _get_constraint_library(strategy).get(constraint_id, CONSTRAINT_CONFIGS[DEFAULT_CONSTRAINT_ID])
    anchor_terms = config.get("anchor_terms", [])
    dossier = lead.get("lead_dossier") or _build_lead_dossier(lead, constraint_id, strategy)
    anchors = dossier.get("source_anchors", [])
    source_observations = [item.lower() for item in dossier.get("source_observations", [])]
    source_snippets = [item.lower() for item in dossier.get("source_snippets", [])]
    primary_observation = (dossier.get("primary_observation") or "").lower()

    personalization = (bundle.get("personalization_line") or "").strip().lower()
    if len(personalization.split()) < 5:
        return False, "Personalization too thin"
    if personalization.startswith(BANNED_PERSONALIZATION_PREFIXES):
        return False, "Personalization reads like a fragment"
    has_observation = any(observation in personalization for observation in source_observations[:4]) if source_observations else False
    if primary_observation and primary_observation in personalization:
        has_observation = True
    if not has_observation and source_snippets:
        has_observation = any(snippet in personalization for snippet in source_snippets[:4])
    if not has_observation and not any(anchor in personalization for anchor in anchors[:5]):
        return False, "Personalization missing source anchor"
    if not re.search(r"\b(usually|tends|often|means|leaves|puts|pulls|starts|gets|keeps|slows)\b", personalization):
        return False, "Personalization missing implied point"

    who_i_help = (bundle.get("who_i_help") or "").strip().lower()
    if len(who_i_help.split()) < 2 or re.search(r"\b(i|we|our|us|my)\b", who_i_help):
        return False, "who_i_help malformed"

    problem_i_fix = (bundle.get("problem_i_fix") or "").strip().lower()
    if len(problem_i_fix.split()) < 3 or not _contains_anchor_term(problem_i_fix, anchor_terms):
        return False, "problem_i_fix too generic"

    trigger = (bundle.get("bottleneck_trigger") or "").strip().lower()
    if (
        len(trigger.split()) < 4
        or not re.search(r"\b(once|when|as|starts|gets|goes)\b", trigger)
        or re.search(r"\b(i|we|our|us|my)\b", trigger)
        or re.search(r"\b(fix|wire|build|help|work with|pulled in)\b", trigger)
    ):
        return False, "bottleneck_trigger too weak"

    observation = (bundle.get("offer_observation") or "").strip().lower()
    if len(observation.split()) < 5 or re.search(r"\b(i|we|our|us|my|can|will|map|book|call)\b", observation):
        return False, "offer_observation malformed"

    offer_scope = (bundle.get("offer_scope") or "").strip().lower()
    if (
        len(offer_scope.split()) < 2
        or re.search(r"\b(i|we|our|us|my)\b", offer_scope)
        or re.search(r"\b(can|will|map|book|call|show|fix|help)\b", offer_scope)
    ):
        return False, "offer_scope malformed"

    payoff = (bundle.get("offer_payoff") or "").strip().lower()
    if len(payoff.split()) < 3:
        return False, "offer_payoff too thin"

    if not re.search(r"\b(time|pipeline|replies|momentum|qualified|follow up|demos|speed|owners|lag|leaks)\b", payoff):
        return False, "offer_payoff too generic"

    signal_topic = (bundle.get("signal_topic") or "").strip().lower()
    if not signal_topic:
        return False, "Subject missing"

    return True, "Valid"


def _get_default_variables(constraint_id: str, strategy: dict | None = None, lead: dict | None = None) -> dict:
    return _finalize_generated_variables({}, lead or {}, constraint_id, strategy)


def _repair_bundle(bundle: dict, lead: dict, constraint_id: str, strategy: dict | None = None) -> dict:
    dossier = lead.get("lead_dossier") or _build_lead_dossier(lead, constraint_id, strategy)
    repaired = dict(bundle)
    defaults = _build_dynamic_defaults(lead, constraint_id, dossier)
    proof_variant = _select_proof_variant(constraint_id, lead, strategy)

    personalization = (repaired.get("personalization_line") or "").strip().lower()
    if (
        len(personalization.split()) < 5
        or personalization.startswith(BANNED_PERSONALIZATION_PREFIXES)
        or not any(word in personalization.split() for word in POINT_WORDS)
        or not any(
            source in personalization
            for source in [
                *[item.lower() for item in dossier.get("source_observations", [])[:4]],
                *[item.lower() for item in dossier.get("source_snippets", [])[:3]],
            ]
        )
    ):
        repaired["personalization_line"] = defaults["personalization_line"]

    who_i_help = (repaired.get("who_i_help") or "").strip().lower()
    if len(who_i_help.split()) < 2:
        repaired["who_i_help"] = defaults["who_i_help"]

    problem_i_fix = (repaired.get("problem_i_fix") or "").strip().lower()
    if len(problem_i_fix.split()) < 3:
        repaired["problem_i_fix"] = defaults["problem_i_fix"]

    trigger = (repaired.get("bottleneck_trigger") or "").strip().lower()
    if (
        len(trigger.split()) < 4
        or not re.search(r"\b(once|when|as|starts|gets|goes)\b", trigger)
        or re.search(r"\b(i|we|our|us|my)\b", trigger)
        or re.search(r"\b(fix|wire|build|help|work with|pulled in)\b", trigger)
    ):
        repaired["bottleneck_trigger"] = defaults["bottleneck_trigger"]

    observation = (repaired.get("offer_observation") or "").strip().lower()
    if len(observation.split()) < 5 or re.search(r"\b(i|we|our|us|my|can|will|map|book|call)\b", observation):
        repaired["offer_observation"] = defaults["offer_observation"]

    offer_scope = (repaired.get("offer_scope") or "").strip().lower()
    if (
        len(offer_scope.split()) < 2
        or re.search(r"\b(i|we|our|us|my)\b", offer_scope)
        or re.search(r"\b(can|will|map|book|call|show|fix|help)\b", offer_scope)
    ):
        repaired["offer_scope"] = defaults["offer_scope"]

    payoff = (repaired.get("offer_payoff") or "").strip().lower()
    if len(payoff.split()) < 3 or not re.search(r"\b(time|pipeline|replies|reply|qualified|follow up|momentum|speed|owners|lag|leaks|stalling|moving)\b", payoff):
        repaired["offer_payoff"] = defaults["offer_payoff"]

    proof_problem = (repaired.get("proof_problem") or "").strip().lower()
    if len(proof_problem.split()) < 5:
        repaired["proof_problem"] = proof_variant["problem"]

    proof_fix = (repaired.get("proof_fix") or "").strip().lower()
    if (
        len(proof_fix.split()) < 2
        or re.search(r"\b(i|we|our|us|my)\b", proof_fix)
        or not re.search(r"\b(flow|system|routing|logic|split|handoff|rule|owner|reminder|escalation)\b", proof_fix)
    ):
        repaired["proof_fix"] = proof_variant["fix"]

    proof_payoff = (repaired.get("proof_payoff") or "").strip().lower()
    if (
        len(proof_payoff.split()) < 4
        or not re.search(r"\b(time|pipeline|replies|reply|qualified|follow up|momentum|speed|owners|lag|leaks|stalling|moving)\b", proof_payoff)
    ):
        repaired["proof_payoff"] = proof_variant["payoff"]

    return _normalize_variables(repaired, strategy)


def _finalize_generated_variables(
    generated: dict,
    lead: dict,
    constraint_id: str,
    strategy: dict | None = None,
) -> dict:
    config = CONSTRAINT_CONFIGS.get(constraint_id, CONSTRAINT_CONFIGS[DEFAULT_CONSTRAINT_ID])
    normalized_generated = _convert_legacy_variables(generated or {}, lead, constraint_id)
    deterministic = config["deterministic"]
    dossier = lead.get("lead_dossier") or _build_lead_dossier(lead, constraint_id, strategy)
    proof = _select_proof_variant(constraint_id, lead, strategy)
    dynamic_defaults = _build_dynamic_defaults(lead, constraint_id, dossier)

    # Select identity_line deterministically from trust_assets
    current_strategy = strategy or {}
    identity_signals = current_strategy.get("trust_assets", {}).get("identity_signals", [])
    if identity_signals:
        id_basis = f"{lead.get('email', '')}|identity_line"
        id_idx = int(hashlib.md5(id_basis.encode("utf-8")).hexdigest(), 16) % len(identity_signals)
        identity_line = identity_signals[id_idx]
    else:
        identity_line = "i fix the follow up and handoff leaks that usually cost agencies warm replies once delivery gets busy"

    # Select micro_insight deterministically: first by specialization, then
    # hash within the list so two leads in the same lane don't get identical
    # Email 2 copy. Falls back to generic_agency list if the spec is unknown.
    spec = dossier.get("agency_specialization", "generic_agency")
    insight_options = AGENCY_MICRO_INSIGHTS.get(spec) or AGENCY_MICRO_INSIGHTS.get("generic_agency") or []
    if insight_options:
        mi_basis = f"{lead.get('email', '')}|micro_insight"
        mi_idx = int(hashlib.md5(mi_basis.encode("utf-8")).hexdigest(), 16) % len(insight_options)
        micro_insight = insight_options[mi_idx]
    else:
        micro_insight = ""

    finalized = {
        "signal_topic": normalized_generated.get("signal_topic") or dynamic_defaults["signal_topic"],
        "personalization_line": normalized_generated.get("personalization_line") or dynamic_defaults["personalization_line"],
        "identity_line": identity_line,
        "who_i_help": normalized_generated.get("who_i_help") or dynamic_defaults["who_i_help"],
        "problem_i_fix": normalized_generated.get("problem_i_fix") or dynamic_defaults["problem_i_fix"],
        "bottleneck_trigger": normalized_generated.get("bottleneck_trigger") or dynamic_defaults["bottleneck_trigger"],
        "offer_observation": normalized_generated.get("offer_observation") or dynamic_defaults["offer_observation"],
        "offer_scope": normalized_generated.get("offer_scope") or dossier.get("offer_scope") or deterministic["offer_scope"],
        "offer_payoff": normalized_generated.get("offer_payoff") or dynamic_defaults["offer_payoff"],
        "followup_ping": normalized_generated.get("followup_ping") or dossier.get("followup_ping") or deterministic["followup_ping"],
        "micro_insight": micro_insight,
        "proof_problem": normalized_generated.get("proof_problem") or proof["problem"],
        "proof_fix": normalized_generated.get("proof_fix") or proof["fix"],
        "proof_payoff": normalized_generated.get("proof_payoff") or proof["payoff"],
    }

    proof_fix = (finalized.get("proof_fix") or "").strip().lower()
    if (
        len(proof_fix.split()) < 2
        or re.search(r"\b(i|we|our|us|my)\b", proof_fix)
        or not re.search(r"\b(flow|system|routing|logic|split|handoff|rule|owner|reminder|escalation)\b", proof_fix)
    ):
        finalized["proof_fix"] = proof["fix"]

    proof_payoff = (finalized.get("proof_payoff") or "").strip().lower()
    if (
        len(proof_payoff.split()) < 4
        or not re.search(r"\b(time|pipeline|replies|reply|qualified|follow up|momentum|speed|owners|lag|leaks|stalling|moving)\b", proof_payoff)
    ):
        finalized["proof_payoff"] = proof["payoff"]

    prepared = {}
    for key in VARIABLE_KEYS:
        prepared[key] = _normalize_line(finalized.get(key, ""), lowercase=True)
    return _normalize_variables(prepared, strategy)


def _build_dynamic_defaults(lead: dict, constraint_id: str, dossier: dict) -> dict:
    config = CONSTRAINT_CONFIGS.get(constraint_id, CONSTRAINT_CONFIGS[DEFAULT_CONSTRAINT_ID])
    fallback = dict(config["fallback_dynamic"])
    lane = dossier.get("lane", "generic")
    observation = (dossier.get("primary_observation") or "").strip().lower()
    anchor = dossier.get("agency_anchor", "")
    specialization = dossier.get("agency_specialization") or _classify_agency_specialization(lead, observation, anchor)
    context = _lead_context_text(lead)

    def _strip_leading_conjunctions(phrase: str, fallback: str) -> str:
        words = phrase.split()
        while words and words[0].lower() in _LEADING_CONJUNCTIONS:
            words = words[1:]
        return " ".join(words) if words else fallback

    def source_phrase(default_label: str) -> str:
        result = _source_phrase_inner(default_label)
        return _strip_leading_conjunctions(result, default_label)

    def _source_phrase_inner(default_label: str) -> str:
        if "fractional cmo" in context and "digital marketing agency" in context:
            return "fractional cmo work inside a digital marketing agency"
        if specialization == "pr":
            if "guaranteed digital and published stories" in context:
                return "guaranteed digital and published stories"
            if "month to month contracts" in context or "month-to-month contracts" in context:
                return "month to month contracts"
            if "performance based publicity" in context or "performance-based publicity" in context:
                return "performance based publicity"
        if specialization == "social_media" and _observation_matches_specialization(observation, specialization):
            return observation
        if specialization == "digital_marketing" and _observation_matches_specialization(observation, specialization):
            return observation
        if specialization == "full_service_creative" and _observation_matches_specialization(observation, specialization):
            return observation
        if specialization == "execution_fulfillment":
            return "marketing execution and fulfillment work"
        if specialization == "merch_gifting":
            if "branded merchandise" in context and "corporate gifting" in context:
                return "branded merchandise and corporate gifting"
            if "promotional products" in context and "branded merchandise" in context:
                return "promotional products and branded merchandise"
            return "branded merchandise work"
        if specialization == "print_promo":
            if _observation_matches_specialization(observation, specialization):
                return observation
            if "visual communications" in context:
                return "visual communications work"
            return "print and production work"
        if specialization == "post_production":
            if _observation_matches_specialization(observation, specialization):
                return observation
            if "vfx" in context and "animation" in context:
                return "vfx and animation delivery"
            if "editing" in context and "finishing" in context:
                return "editing and finishing delivery"
            if "color grading" in context or "colour grading" in context:
                return "color grading and finishing work"
            return "post production delivery"
        if specialization == "activation":
            if _observation_matches_specialization(observation, specialization):
                return observation
            if "promotional marketing" in context:
                return "promotional marketing and event work"
            return "event and activation work"
        if specialization == "paid_media":
            if "paid search" in context and "paid social" in context:
                return "paid search and paid social delivery"
            if "ppc" in context:
                return "ppc delivery"
            return "paid media delivery"
        if specialization == "social_media":
            return "social media agency work"
        if specialization == "digital_marketing":
            return "digital marketing agency work"
        if specialization == "b2b_agency":
            return "b2b agency delivery"
        if specialization == "full_service_creative":
            if "advertising and design" in context or ("advertising" in context and "design" in context):
                return "advertising and design work"
            if "creative agency" in context or "creatively led agency" in context:
                return "creative agency work"
            if "web design" in context or "video production" in context:
                return "design and production work"
            return "full service creative work"
        if _observation_matches_specialization(observation, specialization) and len(observation.split()) <= 8:
            return observation
        return default_label

    def observation_verb(phrase: str) -> str:
        last = (phrase.split()[-1] if phrase.split() else "").lower()
        if " and " in phrase or last in {"contracts", "stories", "products", "services", "activations", "events"}:
            return "mean"
        return "means"

    if lane == "performance_marketing_agency":
        if specialization == "pr":
            phrase = source_phrase("month to month contracts")
            fallback["personalization_line"] = f"{phrase} usually {observation_verb(phrase)} new business follow up gets whatever time is left"
            fallback["who_i_help"] = "performance based pr firms"
            fallback["offer_observation"] = "a few follow up gaps are probably already showing up once pr delivery gets heavy"
            fallback["offer_payoff"] = "new business pipeline stops slipping"
        elif specialization == "activation":
            phrase = source_phrase("event and activation work")
            fallback["personalization_line"] = f"{phrase} usually {observation_verb(phrase)} new business follow up gets squeezed fast"
            fallback["who_i_help"] = "activation agencies"
            fallback["offer_observation"] = "reply ownership is probably already slipping once event delivery gets busy"
            fallback["offer_payoff"] = "warm replies keep moving during busy delivery weeks"
        elif specialization == "execution_fulfillment":
            phrase = source_phrase("marketing execution and fulfillment work")
            fallback["personalization_line"] = f"{phrase} usually {observation_verb(phrase)} new business follow up gets whatever time is left"
            fallback["who_i_help"] = "marketing execution and fulfillment shops"
            fallback["offer_observation"] = "reply ownership is probably already slipping once fulfillment work gets busy"
            fallback["offer_payoff"] = "warm replies stop stalling"
        elif specialization == "merch_gifting":
            phrase = source_phrase("promotional products and branded merchandise")
            fallback["personalization_line"] = f"{phrase} usually {observation_verb(phrase)} production work can push new business follow up to the side"
            fallback["who_i_help"] = "branded merchandise and gifting shops"
            fallback["offer_observation"] = "a few new business replies are probably already stalling once fulfillment work gets busy"
            fallback["offer_payoff"] = "new business replies stop stalling while fulfillment stays full"
        elif specialization == "print_promo":
            phrase = source_phrase("print and production work")
            fallback["personalization_line"] = f"{phrase} usually {observation_verb(phrase)} production work can push new business follow up to the side"
            fallback["who_i_help"] = "print and promo shops"
            fallback["offer_observation"] = "a few new business replies are probably already stalling once the production floor gets busy"
            fallback["offer_payoff"] = "new business replies stop stalling while production stays full"
        elif specialization == "post_production":
            phrase = source_phrase("post production delivery")
            fallback["personalization_line"] = f"{phrase} usually {observation_verb(phrase)} project schedules fill the week and new business follow up slips"
            fallback["who_i_help"] = "post production studios"
            fallback["offer_observation"] = "a few new business replies are probably already sitting unanswered during active finishing weeks"
            fallback["offer_payoff"] = "new business replies stop stalling between projects"
        elif specialization == "b2b_agency":
            phrase = source_phrase("b2b agency delivery")
            fallback["personalization_line"] = f"{phrase} usually {observation_verb(phrase)} delivery stays busy enough that new business follow up gets squeezed"
            fallback["who_i_help"] = "b2b agencies"
            fallback["offer_observation"] = "a few reply leaks are probably already showing up once delivery weeks stack up"
            fallback["offer_payoff"] = "pipeline stays steadier without extra admin time"
        elif specialization == "paid_media":
            phrase = source_phrase("paid media delivery")
            fallback["personalization_line"] = f"{phrase} usually {observation_verb(phrase)} warm replies compete with live client work"
            fallback["who_i_help"] = "paid media agencies"
            fallback["offer_observation"] = "reply ownership is probably already slipping once campaign delivery gets heavy"
            fallback["offer_payoff"] = "warm replies keep moving without extra chasing"
        elif specialization == "social_media":
            phrase = source_phrase("social media agency work")
            fallback["personalization_line"] = f"{phrase} usually {observation_verb(phrase)} new business follow up gets squeezed fast"
            fallback["who_i_help"] = "social media agencies"
            fallback["offer_observation"] = "reply ownership is probably already slipping once client work stacks up"
            fallback["offer_payoff"] = "warm replies stop stalling"
        elif specialization == "digital_marketing":
            phrase = source_phrase("digital marketing agency work")
            fallback["personalization_line"] = f"{phrase} usually {observation_verb(phrase)} client delivery and new business are always competing"
            fallback["who_i_help"] = "digital marketing agencies"
            fallback["offer_observation"] = "reply ownership is probably already slipping once client work stacks up"
            fallback["offer_payoff"] = "warm replies stop stalling"
        elif specialization == "full_service_creative":
            phrase = source_phrase("full service creative work")
            fallback["personalization_line"] = f"{phrase} usually {observation_verb(phrase)} project delivery can crowd out new business follow up"
            fallback["who_i_help"] = "full service creative agencies"
            fallback["offer_observation"] = "reply ownership is probably already slipping once project work stacks up"
            fallback["offer_payoff"] = "more pipeline stays live without extra admin time"
        elif specialization == "creative_studio":
            phrase = source_phrase("creative work")
            fallback["personalization_line"] = f"{phrase} usually {observation_verb(phrase)} project delivery can crowd out new business follow up"
            fallback["who_i_help"] = "creative studios"
            fallback["offer_observation"] = "reply ownership is probably already slipping once project work stacks up"
            fallback["offer_payoff"] = "more pipeline stays live without extra admin time"
        elif specialization == "generic_agency":
            phrase = source_phrase("client delivery")
            fallback["personalization_line"] = f"{phrase} usually {observation_verb(phrase)} new business follow up gets squeezed fast"
            fallback["who_i_help"] = "marketing agencies"
            fallback["offer_observation"] = "a few reply leaks are probably already showing up once delivery gets heavy"
            fallback["offer_payoff"] = "warm replies stop stalling"
        elif "ag marketing" in context or "communications business owner" in context:
            phrase = source_phrase("ag marketing work")
            fallback["personalization_line"] = f"{phrase} usually {observation_verb(phrase)} new business follow up gets squeezed fast"
            fallback["who_i_help"] = "ag marketing and communications owners"
            fallback["offer_observation"] = "a few reply leaks are probably already showing up once client work stacks up"
            fallback["offer_payoff"] = "warm replies stop stalling"
        else:
            if anchor == "design and build delivery":
                fallback["personalization_line"] = "full service creative work usually means project delivery can crowd out new business follow up"
                fallback["who_i_help"] = "full service creative agencies"
                fallback["offer_observation"] = "reply ownership is probably already slipping once project work stacks up"
                fallback["offer_payoff"] = "more pipeline stays live without extra admin time"
            elif anchor == "campaign delivery":
                fallback["personalization_line"] = "campaign delivery usually means warm replies compete with live client work"
                fallback["who_i_help"] = "paid media agencies"
                fallback["offer_observation"] = "reply ownership is probably already slipping once campaign delivery gets heavy"
                fallback["offer_payoff"] = "warm replies keep moving without extra chasing"
            elif anchor == "production heavy delivery":
                fallback["personalization_line"] = "production work usually means new business follow up gets whatever time is left"
                fallback["who_i_help"] = "print and promo shops"
                fallback["offer_observation"] = "a few new business replies are probably already stalling once the production floor gets busy"
                fallback["offer_payoff"] = "new business replies stop stalling while production stays full"
            elif anchor == "live activation work":
                fallback["personalization_line"] = "event and activation work usually mean new business follow up gets squeezed fast"
                fallback["who_i_help"] = "activation agencies"
                fallback["offer_observation"] = "reply ownership is probably already slipping once event delivery gets busy"
                fallback["offer_payoff"] = "warm replies keep moving during busy delivery weeks"
            else:
                chosen = observation or "client delivery"
                fallback["personalization_line"] = f"{chosen} usually means new business follow up gets squeezed fast"

    if lane == "b2b_saas":
        observation_norm = observation.replace("-", " ")
        if any(token in observation_norm for token in ["shared queue", "shared inbox"]):
            fallback["personalization_line"] = f"{observation} usually means hot replies wait longer than they should"
        elif any(token in observation_norm for token in ["speed to lead", "demo", "trial"]):
            fallback["personalization_line"] = f"{observation} usually starts leaking once reply volume goes up"

    personalization = (fallback.get("personalization_line") or "").strip().lower()
    if lane == "performance_marketing_agency":
        if specialization == "pr" or anchor == "monthly pr delivery":
            safe_personalization = "pr delivery usually means new business follow up gets whatever time is left"
        elif specialization == "paid_media" or anchor == "campaign delivery":
            safe_personalization = "campaign delivery usually means warm replies compete with live client work"
        elif specialization in {"execution_fulfillment", "merch_gifting", "print_promo"} or anchor == "production heavy delivery":
            safe_personalization = "production work usually means new business follow up gets whatever time is left"
        elif specialization == "activation" or anchor == "live activation work":
            safe_personalization = "event and activation work usually mean new business follow up gets squeezed fast"
        elif specialization == "post_production":
            safe_personalization = "post production delivery usually means project schedules fill the week and new business follow up slips"
        else:
            safe_personalization = "full service creative work usually means project delivery can crowd out new business follow up"

        if (
            not personalization
            or personalization.startswith(BANNED_PERSONALIZATION_PREFIXES)
            or ":" in personalization
            or re.search(r"\b(we|our|us)\b", personalization)
            or not re.search(r"\b(means|mean|puts|put|pulls|pull|leaves|leave|starts|start|gets|get|keeps|keep|slows|slow|pushes|push)\b", personalization)
            or not _has_service_signal(personalization)
        ):
            fallback["personalization_line"] = safe_personalization

    # Apply per-specialization overrides for diversity (Issues 3-6)
    if specialization and specialization in SPECIALIZATION_PROBLEM_I_FIX:
        fallback["problem_i_fix"] = SPECIALIZATION_PROBLEM_I_FIX[specialization]
    if specialization and specialization in SPECIALIZATION_BOTTLENECK_TRIGGER:
        fallback["bottleneck_trigger"] = SPECIALIZATION_BOTTLENECK_TRIGGER[specialization]
    if specialization and specialization in SPECIALIZATION_OFFER_SCOPE:
        fallback["offer_scope"] = SPECIALIZATION_OFFER_SCOPE[specialization]
    # Hash-deterministic signal_topic selection from specialization pool
    if specialization and specialization in SIGNAL_TOPIC_POOLS:
        pool = SIGNAL_TOPIC_POOLS[specialization]
        basis = f"{lead.get('email', '')}|{constraint_id}|signal_topic"
        idx = int(hashlib.md5(basis.encode("utf-8")).hexdigest(), 16) % len(pool)
        fallback["signal_topic"] = pool[idx]

    # Belt-and-suspenders: some lane branches (notably b2b_saas) don't enforce
    # a safe_personalization guard, so a "with/for/as" prefix can survive from
    # the base fallback. Catch and rewrite here before diversification runs.
    pl = (fallback.get("personalization_line") or "").strip().lower()
    if pl.startswith(BANNED_PERSONALIZATION_PREFIXES):
        fallback["personalization_line"] = "client delivery usually means new business follow up gets whatever time is left"

    # Final step: structurally diversify the personalization_line so that
    # fallback output (which nearly every specialization branch above
    # emits as "X usually means Y") doesn't produce the banned template
    # pattern at scale. See _diversify_personalization_structure for the
    # list of variants.
    fallback["personalization_line"] = _diversify_personalization_structure(
        fallback.get("personalization_line", ""),
        lead.get("email", ""),
    )

    return fallback


# Structural variants for the personalization_line fallback. All take a
# line originally written as "{phrase} usually means {consequence}" and
# re-render it into one of these forms. Selection is deterministic on
# lead email so the same lead always sees the same line, but two leads
# with the same (phrase, consequence) pair (e.g. same specialization,
# thin enrichment) will see different structures.
#
# Each structure MUST contain one of QA's POINT_WORDS
# ({usually, tends, often, means, leaves, puts, pulls, starts, pushes})
# — otherwise _check_personalization_has_point will reject it at QA time.
# "when X gets heavy, Y" was dropped because "gets" is not a POINT_WORD.
_PERSONALIZATION_STRUCTURES = [
    "{phrase} usually means {consequence}",
    "{phrase} tends to mean {consequence}",
    # Previously "{phrase} often pulls focus from {consequence}" — "from"
    # required a noun-phrase consequence, but our consequences are full
    # clauses (e.g. "new business follow up gets squeezed fast"), producing
    # broken grammar when fused: "...pulls focus from new business follow
    # up gets squeezed fast." Replaced with a clause-safe variant that
    # doubles up on POINT_WORDS (often + starts + mean).
    "{phrase} often starts to mean {consequence}",
    "once {phrase} starts picking up, {consequence}",
]


def _diversify_personalization_structure(line: str, email: str) -> str:
    """
    Recognize the "{phrase} usually means {consequence}" fallback template
    and hash-pick a structural variant. Leaves other lines untouched.
    """
    if not line:
        return line
    # Accept "usually means", "usually mean", "usually puts", etc. — the
    # verb_observation helper picks between "means"/"mean" based on the
    # phrase, so we normalize here.
    m = re.match(r"^(?P<phrase>.+?)\s+usually\s+(?:means|mean)\s+(?P<consequence>.+)$", line.strip(), flags=re.IGNORECASE)
    if not m:
        return line
    phrase = m.group("phrase").strip()
    consequence = m.group("consequence").strip()
    if not phrase or not consequence:
        return line

    basis = f"{email}|personalization_structure"
    idx = int(hashlib.md5(basis.encode("utf-8")).hexdigest(), 16) % len(_PERSONALIZATION_STRUCTURES)
    return _PERSONALIZATION_STRUCTURES[idx].format(phrase=phrase, consequence=consequence)


def build_deterministic_rescue_bundle(
    lead: dict,
    constraint_id: str,
    strategy: dict | None = None,
    current_vars: dict | None = None,
    failures: list[str] | None = None,
) -> dict:
    failures = failures or []
    current_vars = current_vars or {}
    defaults = _get_default_variables(constraint_id, strategy, lead=lead)
    current_bundle = _repair_bundle(
        _finalize_generated_variables(current_vars, lead, constraint_id, strategy),
        lead,
        constraint_id,
        strategy,
    )
    rescued = dict(current_bundle)
    failure_text = " | ".join(failures).lower()

    def has_failure(*needles: str) -> bool:
        return any(needle in failure_text for needle in needles)

    if has_failure("personalization_line", "collective voice", "banned ai word"):
        rescued["personalization_line"] = defaults["personalization_line"]
    if has_failure("who_i_help", "problem_i_fix", "bottleneck_trigger", "collective voice"):
        rescued["who_i_help"] = defaults["who_i_help"]
        rescued["problem_i_fix"] = defaults["problem_i_fix"]
        rescued["bottleneck_trigger"] = defaults["bottleneck_trigger"]
    if has_failure("offer_observation", "offer_scope", "offer_payoff", "collective voice", "banned ai word"):
        rescued["offer_observation"] = defaults["offer_observation"]
        rescued["offer_scope"] = defaults["offer_scope"]
        rescued["offer_payoff"] = defaults["offer_payoff"]
    if has_failure("followup_ping", "collective voice"):
        rescued["followup_ping"] = defaults["followup_ping"]
    if has_failure("proof_problem", "proof_fix", "proof_payoff", "collective voice", "banned ai word"):
        rescued["proof_problem"] = defaults["proof_problem"]
        rescued["proof_fix"] = defaults["proof_fix"]
        rescued["proof_payoff"] = defaults["proof_payoff"]

    rescued = _repair_bundle(rescued, lead, constraint_id, strategy)
    return _normalize_variables(rescued, strategy)


def _build_banned_reminders() -> str:
    """
    Compact text block enumerating every QA-banned word / phrase / transition /
    formal expansion / praise-start. Injected into both the synthesis and regen
    system prompts so Claude generates passing copy on the first try instead of
    getting rejected by QA and burning regen cycles.

    Kept terse: one line per category, alphabetized for prompt-cache stability.
    """
    ai_words = ", ".join(sorted(BANNED_AI_WORDS))
    ai_phrases = "; ".join(sorted(BANNED_AI_PHRASES))
    transitions = ", ".join(sorted(BANNED_TRANSITIONS))
    outreach = "; ".join(sorted(BANNED_OUTREACH_PHRASES))
    praise = ", ".join(sorted(f'"{p.strip()}"' for p in BANNED_PRAISE_STARTS))
    collective = ", ".join(f'"{w}"' for w in COLLECTIVE_VOICE_WORDS)
    expansions = "; ".join(f'"{k}" → "{v}"' for k, v in sorted(FORMAL_EXPANSIONS.items()))

    return f"""ABSOLUTELY BANNED — QA will reject and regenerate if ANY of these appear:
- ai/hype words: {ai_words}
- ai phrases: {ai_phrases}
- formal transitions: {transitions}
- outreach clichés: {outreach}
- praise starts: never begin personalization_line with {praise}
- collective voice: never use {collective} anywhere in output
- formal expansions (always contract): {expansions}
- em dashes or hyphens of any kind (use "and", commas, or new sentences)
- semicolons or colons inside variable values
- exclamation marks

Also banned:
- any claim with a number you cannot verify from the dossier
- "I noticed..." openers — they scream AI template
- praise or flattery toward the prospect or their company
- meta-language ("just checking in", "bumping this", "circling back")
"""


def _build_synthesis_system_prompt(strategy: dict | None) -> str:
    current = _get_strategy(strategy)
    doctrine_summary = summarize_doctrine_for_prompt(current.get("doctrine", {})) if current.get("doctrine") else ""
    thresholds = _get_qa_thresholds(current)
    banned_block = _build_banned_reminders()

    return f"""You generate constrained cold-email variables for Futureaide using Nick Saraev's framework.

{doctrine_summary}

{banned_block}

NON-NEGOTIABLE RULES:
- write like one operator sending a 1:1 email
- use first person singular only where needed in the final email voice
- never use "we", "our", or "us"
- do not write the full email
- generate only the requested variables
- the personalization must be source grounded, not generic praise
- each paragraph must set up the next one
- the cold email is reply first, not meeting first
- do not mention linkedin
- do not invent facts, names, tools, numbers, or claims
- avoid hype, jargon, and corporate language
- no hyphens or dashes of any kind
- use contractions, but avoid unclear contractions like "it'd"
- keep all output lowercase

YOU ARE GENERATING ONLY THESE VARIABLES:
1. signal_topic
2. personalization_line
3. who_i_help
4. problem_i_fix
5. bottleneck_trigger
6. offer_observation
7. offer_payoff

VARIABLE SPECS:

signal_topic
- job: a short plausible-deniability subject line that hints at a live issue
- length: 1 to {thresholds['signal_topic_max_words']} words
- good: "follow up ownership"
- bad: "quick automation idea"

personalization_line
- job: a one-sentence opener that CITES something real from the dossier and states what that likely means operationally
- length: aim for around {thresholds['personalization_line_max_words']} words — this is a natural-prose target, not a hard cap. a sentence that reads cleanly at 18 or 25 words is better than one artificially cut to fit.
- must cite a concrete source observation — prefer: (1) recent linkedin post topic, (2) linkedin skill or specialty, (3) service mentioned on the website, (4) distinctive phrase from linkedin headline or company description, in that priority order
- when a primary observation is provided, use that exact phrase or a very close paraphrase inside the line
- prefer the raw source wording over abstract labels
- the sentence must stand on its own with a clear main verb
- do not start with "with", "for", or "as" as a loose fragment
- do NOT start with "I noticed..." — that opener has become the 2025 AI-tell equivalent of "hope this finds you well"
- do NOT use the formula "[generic descriptor] usually means [generic pain]" when you have a specific source — that's template inference, not observation
- must feel like you actually looked at their profile for 30 seconds, not derived a pattern from their industry
- must not signal the sale yet
- do not turn a size band into an exact headcount
- do not abbreviate the company name or invent shorthand like an acronym
- good (specific): "saw your last post about pulling hubspot attribution reports every monday"
- good (specific): "partner-led delivery on performance audits usually pulls ownership away from new business replies"
- good (specific): "fractional cmo engagements with three to five retainers typically stretch bandwidth during campaign launches"
- bad (generic, template): "month to month contracts usually mean new business follow up gets whatever time is left"
- bad (generic): "paid media delivery usually means warm replies compete with live client work"
- bad (praise): "love your growth journey in pr and how passionate you are about clients"
- bad (fragment): "with monthly pr delivery, new business follow up gets squeezed"

who_i_help
- job: the reference group you work with
- length: aim for around {thresholds['who_i_help_max_words']} words (tight noun phrase)
- should be a tight group, not "businesses like yours"
- good: "performance agencies"
- bad: "businesses like yours"

problem_i_fix
- job: the bottleneck class you solve for that reference group
- length: aim for around {thresholds['problem_i_fix_max_words']} words (tight phrase)
- use concrete lane language, not generic automation language
- good: "reply handling and handoff leaks"
- bad: "automation problems"

bottleneck_trigger
- job: explain when the problem shows up
- length: aim for around {thresholds['bottleneck_trigger_max_words']} words — target, not hard cap. a complete natural thought beats one chopped to fit.
- should answer "when does this start breaking?"
- good: "client delivery starts crowding out new business follow up"
- bad: "things get busy"

offer_observation
- job: the specific observation that makes the give-first offer feel earned
- length: aim for around {thresholds['offer_observation_max_words']} words — target, not hard cap.
- it appears after "my guess is"
- no first person pronouns, no CTA language, no promises
- good: "a few reply leaks are probably already showing up once delivery gets heavy"
- bad: "i can definitely help you make more money"

offer_payoff
- job: the ROI or relief the mapped leaks would create
- length: aim for around {thresholds['offer_payoff_max_words']} words — target, not hard cap.
- should mention time saved, fewer leaks, steadier pipeline, faster response, or similar
- keep it short and direct
- good: "warm replies stop stalling"
- good: "more pipeline stays live"
- bad: "you get amazing results fast"

FORMATTING:
- return only valid JSON
- every value should be a short fragment or sentence piece that fits the fixed template
- no markdown
- no commentary
- no extra keys
"""


def _format_dossier_block(dossier: dict) -> str:
    return f"""--- LEAD ({dossier['email']}) ---
name: {dossier['first_name']}
title: {dossier['title']}
company: {dossier['company_name']}
company size: {dossier['company_size']}
lane: {dossier['lane']}
role bucket: {dossier['role_bucket']}
copy selector: {dossier['copy_selector']}
constraint: {dossier['constraint_id']} — {dossier['constraint_implication']}

SOURCE TRUTH
raw linkedin headline: {dossier['raw_linkedin_headline']}
raw linkedin job description: {dossier['raw_linkedin_job_description']}
raw company context: {dossier['raw_company_context']}
linkedin skills: {dossier.get('linkedin_skills', '')}
tenure at current company (years): {dossier.get('linkedin_tenure_years', '')}
website context: {dossier.get('website_context', '')}
recent linkedin posts: {dossier.get('linkedin_recent_posts', '')}
primary observation: {dossier['primary_observation']}
source observations: {", ".join(dossier['source_observations'])}
source snippets: {", ".join(dossier['source_snippets'])}
source anchors: {", ".join(dossier['source_anchors'])}
inside language: {", ".join(dossier['inside_language'])}

FIXED FRAGMENTS
offer_scope: {dossier['offer_scope']}
followup_ping: {dossier['followup_ping']}

FIXED EMAIL SHAPE
email 1:
  hi {dossier['first_name'] or 'there'},
  {{personalization_line}}.
  i work with {{who_i_help}} on {{problem_i_fix}}. i'm usually pulled in when {{bottleneck_trigger}}.
  my guess is {{offer_observation}}. if useful, i can map the 3 leaks in your {dossier['offer_scope']} so {{offer_payoff}}.
  want me to map the 3 biggest leaks i see?

PERSONALIZATION RULE
- use the primary observation if one is provided
- prefer a raw source snippet over an abstract label
- if recent linkedin posts are available, reference ONE specific post topic as the personalization hook (this is gold, use it)
- if website context reveals specific services, team size, or client types, weave one concrete detail into the personalization
- if linkedin skills include relevant specialties (e.g. "PPC", "Salesforce", "HubSpot"), use that to sharpen the observation
- if tenure is 3+ years, that suggests delivery has been scaling and the bottleneck is real
- keep it to one sentence
- the line should read like: [observed thing] + [what that usually means]
- the sentence must feel complete, not like a loose clause
- do not generalize away from the observed thing

Generate the seven requested variables so this email reads like one clean human thought.
"""


def _build_batch_prompt(batch: list[dict], strategy: dict | None = None) -> str:
    dossier_blocks = "\n\n".join(_format_dossier_block(item["lead_dossier"]) for item in batch)
    return f"""Generate the requested cold-email variables for each lead below.

{dossier_blocks}

Return ONLY a valid JSON array with {len(batch)} objects.
Each object must include:
- email
- signal_topic
- personalization_line
- who_i_help
- problem_i_fix
- bottleneck_trigger
- offer_observation
- offer_payoff"""


def _synthesize_batch(
    batch: list[dict],
    client: anthropic.Anthropic,
    campaign_strategy: dict | None = None,
) -> list[dict]:
    prompt = _build_batch_prompt(batch, campaign_strategy)
    for attempt in range(2):
        try:
            response = client.messages.create(
                model=_get_strategy(campaign_strategy).get("model_settings", {}).get("personalization_model", CLAUDE_MODEL),
                max_tokens=max(600, len(batch) * 250),
                temperature=0.3,
                system=_build_synthesis_system_prompt(campaign_strategy),
                messages=[{"role": "user", "content": prompt}],
                timeout=ANTHROPIC_TIMEOUT_SECONDS,
            )

            text = response.content[0].text.strip()
            results = _parse_json_payload(text)
            if not isinstance(results, list):
                results = [results]
            return results
        except json.JSONDecodeError as exc:
            log.warning("synthesis.batch_json_error", error=str(exc), attempt=attempt + 1)
            if attempt == 0:
                continue
            log.error("synthesis.batch_json_final", attempt=attempt + 1, prompt_preview=prompt[:300])
        except anthropic.RateLimitError:
            wait = (2 ** attempt) * 5
            log.warning("synthesis.rate_limit", wait=wait)
            time.sleep(wait)
        except Exception as exc:
            log.error("synthesis.batch_error", error=str(exc))
            break

    log.error("synthesis.batch_failed", batch_size=len(batch), prompt_preview=prompt[:300])
    return []


def personalize_leads(leads: list[dict], campaign_strategy: dict | None = None) -> tuple[list[dict], float]:
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not set in .env")

    client = anthropic.Anthropic(api_key=api_key, timeout=ANTHROPIC_TIMEOUT_SECONDS)
    current_strategy = _get_strategy(campaign_strategy)
    constraint_library = _get_constraint_library(current_strategy)
    default_constraint_id = _get_default_constraint_id(current_strategy, constraint_library)

    eligible = [lead for lead in leads if check_lead_eligibility(lead)]
    excluded = len(leads) - len(eligible)
    if excluded:
        log.info("personalize.eligibility_excluded", count=excluded)

    batch_items = []
    for lead in eligible:
        primitives = extract_primitives(lead)
        constraint_id = choose_constraint(primitives, constraint_library)
        copy_selector = _select_copy_selector(lead, primitives)
        lead["constraint_id"] = constraint_id
        lead["copy_selector"] = copy_selector
        lead_dossier = _build_lead_dossier(lead, constraint_id, current_strategy, primitives=primitives)
        lead["lead_dossier"] = lead_dossier
        batch_items.append(
            {
                "lead": lead,
                "constraint_id": constraint_id,
                "copy_selector": copy_selector,
                "primitives": primitives,
                "lead_dossier": lead_dossier,
            }
        )

    batches = [batch_items[i:i + BATCH_SIZE] for i in range(0, len(batch_items), BATCH_SIZE)]
    total_batches = len(batches)
    log.info("personalize.starting_synthesis", leads=len(eligible), batches=total_batches, batch_size=BATCH_SIZE)

    total_cost = 0.0
    failed_batches = 0

    def _process_batch(batch_idx_and_batch):
        batch_idx, batch = batch_idx_and_batch
        return batch_idx, batch, _synthesize_batch(batch, client, campaign_strategy=campaign_strategy)

    with ThreadPoolExecutor(max_workers=MAX_PARALLEL_CLAUDE) as executor:
        futures = {executor.submit(_process_batch, (idx, batch)): idx for idx, batch in enumerate(batches)}

        for future in as_completed(futures):
            try:
                batch_idx, batch, results = future.result()
            except Exception as exc:
                batch_idx = futures[future]
                batch = batches[batch_idx]
                results = []
                log.error("personalize.batch_future_error", batch=batch_idx, error=str(exc))

            # Empty results means every lead in this batch will fall back to
            # deterministic defaults — producing *identical* copy across those
            # leads. That's a deliverability AND brand risk, so track it and
            # abort the run if a meaningful slice of batches fail. Threshold
            # chosen to tolerate transient rate limits without letting a broken
            # prompt or API outage ship boilerplate at scale.
            if not results:
                failed_batches += 1

            result_map = {}
            for result in results:
                email = (result.get("email") or "").strip().lower()
                if email:
                    result_map[email] = result

            for item in batch:
                lead = item["lead"]
                email = (lead.get("email") or "").strip().lower()
                generated = result_map.get(email, {})
                bundle = _finalize_generated_variables(generated, lead, item["constraint_id"], campaign_strategy)
                bundle = _repair_bundle(bundle, lead, item["constraint_id"], campaign_strategy)
                is_valid, reason = validate_bundle(bundle, lead, item["constraint_id"], campaign_strategy)
                if not is_valid:
                    log.debug("personalize.bundle_invalid", email=email, reason=reason)
                    bundle = _get_default_variables(item["constraint_id"], campaign_strategy, lead=lead)
                    bundle = _repair_bundle(bundle, lead, item["constraint_id"], campaign_strategy)
                    is_valid, reason = validate_bundle(bundle, lead, item["constraint_id"], campaign_strategy)
                    if not is_valid:
                        log.debug("personalize.bundle_defaulting", email=email, reason=reason)
                lead["custom_variables"] = bundle

            total_cost += len(batch) * CLAUDE_COST_PER_LEAD
            if (batch_idx + 1) % 10 == 0 or batch_idx == total_batches - 1:
                log.info("personalize.progress", completed=batch_idx + 1, total=total_batches)

    # Abort if too many batches fell back to defaults. See note above the
    # `if not results: failed_batches += 1` line for rationale.
    BATCH_FAILURE_ABORT_RATIO = float(os.getenv("PERSONALIZE_BATCH_FAILURE_ABORT_RATIO", "0.10"))
    if total_batches and failed_batches / total_batches > BATCH_FAILURE_ABORT_RATIO:
        raise RuntimeError(
            f"Personalization batch failure rate too high: "
            f"{failed_batches}/{total_batches} batches fell back to defaults "
            f"(> {BATCH_FAILURE_ABORT_RATIO:.0%} threshold). "
            f"Shipping would mean identical boilerplate copy across many leads. "
            f"Check Claude rate limits, API errors, and the synthesis prompt."
        )
    if failed_batches:
        log.warning(
            "personalize.batch_failure_tolerated",
            failed_batches=failed_batches,
            total_batches=total_batches,
            threshold=BATCH_FAILURE_ABORT_RATIO,
        )

    personalized = [lead for lead in eligible if "custom_variables" in lead]
    log.info("personalize.complete", personalized=len(personalized), claude_cost_usd=round(total_cost, 2))
    return personalized, total_cost


def _build_regen_system_prompt(strategy: dict | None) -> str:
    thresholds = _get_qa_thresholds(strategy)
    banned_block = _build_banned_reminders()
    return f"""You are fixing cold-email variables that failed quality checks.

{banned_block}

Regenerate all of the dynamic variables:
- signal_topic
- personalization_line
- who_i_help
- problem_i_fix
- bottleneck_trigger
- offer_observation
- offer_payoff

Keep the same Nick-aligned structure:
- personalization must be source grounded and have a point
- who_i_help + problem_i_fix + bottleneck_trigger must make the identity paragraph feel relevant
- offer_observation + offer_scope + offer_payoff must make the give-first offer feel earned
- stay reply first
- all output lowercase

Length targets (prose targets, not hard caps — write complete natural
sentences; don't pad to hit a number, don't chop to stay under):
- signal_topic: around {thresholds['signal_topic_max_words']} words (short subject, genuinely critical)
- personalization_line: around {thresholds['personalization_line_max_words']} words
- who_i_help: around {thresholds['who_i_help_max_words']} words (tight noun phrase)
- problem_i_fix: around {thresholds['problem_i_fix_max_words']} words (tight phrase)
- bottleneck_trigger: around {thresholds['bottleneck_trigger_max_words']} words
- offer_observation: around {thresholds['offer_observation_max_words']} words
- offer_payoff: around {thresholds['offer_payoff_max_words']} words

Return ONLY a single JSON object with the seven keys above."""


def _build_regen_prompt(
    lead: dict,
    current_vars: dict,
    failures: list[str],
    constraint_id: str,
    strategy: dict | None = None,
) -> str:
    dossier = lead.get("lead_dossier") or _build_lead_dossier(lead, constraint_id, strategy)
    return f"""Fix these cold-email variables. They failed QA with these specific issues:

FAILURES:
{chr(10).join(f"  - {failure}" for failure in failures)}

CURRENT VARIABLES:
{json.dumps({key: current_vars.get(key, "") for key in LLM_GENERATED_KEYS}, indent=2)}

LEAD DOSSIER:
{json.dumps(dossier, indent=2)}

FIXED EMAIL 1 TEMPLATE:
Hi {lead.get('first_name', 'there')},
{{personalization_line}}.
i work with {{who_i_help}} on {{problem_i_fix}}. i'm usually pulled in when {{bottleneck_trigger}}.
my guess is {{offer_observation}}. if useful, i can map the 3 leaks in your {dossier['offer_scope']} so {{offer_payoff}}.
want me to map the 3 biggest leaks i see?

CRITICAL:
- use the primary observation from the dossier inside personalization_line
- prefer the raw source snippets in the dossier over abstract labels
- keep personalization_line in the format: observed thing + what that usually means
- keep personalization_line as a complete sentence, not a fragment starting with "with"

Fix the logic jumps and weak phrasing. Return ONLY valid JSON with the seven dynamic keys."""


def regenerate_lead(
    lead: dict,
    failures: list[str],
    constraint_id: str,
    client: anthropic.Anthropic,
    campaign_strategy: dict | None = None,
) -> tuple[dict | None, float]:
    current_vars = lead.get("custom_variables", {})
    prompt = _build_regen_prompt(lead, current_vars, failures, constraint_id, campaign_strategy)
    cost = 0.0

    for attempt in range(2):
        try:
            response = client.messages.create(
                model=_get_strategy(campaign_strategy).get("model_settings", {}).get("personalization_model", CLAUDE_MODEL),
                max_tokens=700,
                temperature=0.3,
                system=_build_regen_system_prompt(campaign_strategy),
                messages=[{"role": "user", "content": prompt}],
                timeout=ANTHROPIC_TIMEOUT_SECONDS,
            )
            cost += CLAUDE_COST_PER_LEAD

            text = response.content[0].text.strip()
            result = _parse_json_payload(text)
            if isinstance(result, list):
                result = result[0]
            repaired = _finalize_generated_variables(result, lead, constraint_id, campaign_strategy)
            repaired = _repair_bundle(repaired, lead, constraint_id, campaign_strategy)
            return repaired, cost
        except json.JSONDecodeError as exc:
            log.warning("regen.json_error", email=lead.get("email"), error=str(exc), attempt=attempt + 1)
            if attempt == 0:
                continue
            log.error("regen.json_final", email=lead.get("email"), prompt_preview=prompt[:300])
        except Exception as exc:
            log.error("regen.error", email=lead.get("email"), error=str(exc))
            break

    log.error("regen.failed", email=lead.get("email"), failures=failures)
    return None, cost


def regenerate_failed_leads(
    failed_leads: list[dict],
    campaign_strategy: dict | None = None,
    max_attempts: int = MAX_REGEN_ATTEMPTS,
) -> tuple[list[dict], list[dict], float]:
    from execution.qa_check import qa_check_lead

    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        log.warning("regen.no_api_key", msg="Cannot regenerate without ANTHROPIC_API_KEY")
        return [], failed_leads, 0.0

    client = anthropic.Anthropic(api_key=api_key, timeout=ANTHROPIC_TIMEOUT_SECONDS)
    total_cost = 0.0
    newly_passed = []
    still_in_play = list(failed_leads)

    for attempt_num in range(1, max_attempts + 1):
        if not still_in_play:
            break

        log.info("regen.attempt", attempt=attempt_num, leads=len(still_in_play))
        attempt_failures = []

        def _regen_one(lead: dict):
            qa_failures = lead.get("qa_failures", []) or lead.get("_qa_failures", [])
            constraint_id = lead.get("constraint_id", lead.get("_constraint_id", DEFAULT_CONSTRAINT_ID))
            new_vars, regen_cost = regenerate_lead(lead, qa_failures, constraint_id, client, campaign_strategy)
            return lead, new_vars, regen_cost

        with ThreadPoolExecutor(max_workers=MAX_PARALLEL_CLAUDE) as executor:
            futures = {executor.submit(_regen_one, lead): lead for lead in still_in_play}
            for future in as_completed(futures):
                try:
                    lead, new_vars, regen_cost = future.result()
                    total_cost += regen_cost
                    if new_vars:
                        lead["custom_variables"] = new_vars
                        is_pass, new_failures = qa_check_lead(lead, campaign_strategy=campaign_strategy)
                        if is_pass:
                            lead["qa_status"] = "passed"
                            lead.pop("qa_failures", None)
                            lead.pop("_qa_failures", None)
                            lead["regen_attempts"] = attempt_num
                            newly_passed.append(lead)
                        else:
                            lead["qa_failures"] = new_failures
                            attempt_failures.append(lead)
                    else:
                        attempt_failures.append(lead)
                except Exception as exc:
                    lead = futures[future]
                    log.error("regen.future_error", email=lead.get("email"), error=str(exc))
                    attempt_failures.append(lead)

        log.info(
            "regen.attempt_result",
            attempt=attempt_num,
            recovered=len(newly_passed),
            still_failing=len(attempt_failures),
        )
        still_in_play = attempt_failures

    return newly_passed, still_in_play, total_cost
