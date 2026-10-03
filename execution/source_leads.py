"""
Step 1: Source Leads (with embedded ICP Pretest)
Three sources: Apify Leads Finder (primary), Apollo free search (secondary), CSV import (tertiary).
All leads are checked against the suppression list before returning.

The Apify path runs a pretest loop:
  1. Fetch 100 leads with the current query params
  2. Run ICP pretest on the 100
  3. If >= 75% pass: fetch the remaining leads with the same params, return all
  4. If < 75%: refine the query params and go back to step 1
  5. Max 5 attempts before aborting
"""

import os
import csv
import re
import time
from collections import Counter
from typing import Optional

import requests
import structlog
from apify_client import ApifyClient
from dotenv import load_dotenv

load_dotenv()
log = structlog.get_logger()

# Standard lead schema — every source maps to these keys
LEAD_SCHEMA_KEYS = [
    "email", "first_name", "last_name", "company_name", "title",
    "linkedin_url", "company_website", "email_status",
    "company_size", "company_industry",
]

SENIORITY_MAP = {
    "founder": "founder",
    "co-founder": "founder",
    "owner": "owner",
    "ceo": "c_suite",
    "coo": "c_suite",
    "cfo": "c_suite",
    "cto": "c_suite",
    "cmo": "c_suite",
    "cro": "c_suite",
    "chief": "c_suite",
    "president": "c_suite",
    "managing director": "director",
    "managing partner": "partner",
    "partner": "partner",
    "vp": "vp",
    "vice president": "vp",
    "head of": "head",
    "head": "head",
    "director": "director",
    "svp": "vp",
    "evp": "vp",
}


# ---------------------------------------------------------------------------
# ICP Pretest — title patterns, exclusions, scoring
# ---------------------------------------------------------------------------

TITLE_PATTERNS = {
    "c_level": ["CEO", "CFO", "CTO", "COO", "CMO", "CRO", "Chief"],
    "founder": ["Founder", "Co-Founder", "Owner", "Partner", "Principal"],
    "president": ["President", "VP", "Vice President", "Managing Director"],
    "director": ["Director", "Head of", "SVP", "EVP"],
    "ops": ["Operations", "RevOps", "Revenue Operations", "Sales Ops", "Marketing Ops"],
    "growth": ["Growth", "Demand Gen", "Demand Generation"],
}

EXCLUSION_KEYWORDS = [
    "freelance", "freelancer", "consultant", "solo",
    "holding", "portfolio", "umbrella",
    "seo specialist", "content strategist", "social media manager",
    "executive chairman", "board member", "board of directors",
    "creative director", "brand strategist", "design director",
    "internal", "in-house",
]

ICP_PASS_THRESHOLD = 0.75
MAX_PRETEST_ATTEMPTS = 6
MAX_PROFILE_REFINEMENTS = 3
PRETEST_SAMPLE_SIZE = 100

# Company-size ranges that are clearly outside ICP (solo operators & huge enterprises)
_COMPANY_SIZE_EXCLUSIONS = {
    "1", "2-10", "self-employed", "self employed",
    "5001-10000", "10001+", "10001-", "10000+",
}

# Industries that strongly align with our ICP
ICP_INDUSTRIES = {
    "marketing & advertising", "marketing and advertising",
    "internet", "online media", "public relations & communications",
    "public relations and communications", "media production",
    "information technology and services", "computer software",
    "management consulting", "design", "graphic design",
    "market research", "advertising", "marketing",
    "digital marketing", "media", "creative",
}


def _get_allowed_titles(icp: dict | None = None) -> list[str]:
    if icp and icp.get("titles"):
        return [str(title).strip() for title in icp.get("titles", []) if str(title).strip()]

    titles = []
    for patterns in TITLE_PATTERNS.values():
        titles.extend(patterns)
    return titles


def _get_allowed_industries(icp: dict | None = None) -> set[str]:
    if icp and icp.get("industries"):
        return {str(industry).lower().strip() for industry in icp.get("industries", []) if str(industry).strip()}
    return ICP_INDUSTRIES


def _get_allowed_industry_list(icp: dict | None = None) -> list[str]:
    if icp and icp.get("industries"):
        return [str(industry).lower().strip() for industry in icp.get("industries", []) if str(industry).strip()]
    return sorted(ICP_INDUSTRIES)


def _get_allowed_company_sizes(icp: dict | None = None) -> set[str]:
    if icp and icp.get("company_size_ranges"):
        return {str(size).lower().strip() for size in icp.get("company_size_ranges", []) if str(size).strip()}
    return set()


def _parse_company_size_bounds(size_str: str) -> tuple[int | None, int | None]:
    """Parse company size strings like '39', '11-50', '10001+', or '51-200 employees'."""
    if not size_str:
        return None, None

    cleaned = size_str.lower().strip().replace(",", "")
    cleaned = re.sub(r"\bemployees?\b", "", cleaned).strip()

    if cleaned in {"self-employed", "self employed"}:
        return 1, 1

    range_match = re.search(r"(\d+)\s*-\s*(\d+)", cleaned)
    if range_match:
        low = int(range_match.group(1))
        high = int(range_match.group(2))
        return low, high

    plus_match = re.search(r"(\d+)\s*\+", cleaned)
    if plus_match:
        low = int(plus_match.group(1))
        return low, None

    exact_match = re.fullmatch(r"\d+", cleaned)
    if exact_match:
        value = int(cleaned)
        return value, value

    return None, None


def _size_ranges_overlap(left: tuple[int | None, int | None], right: tuple[int | None, int | None]) -> bool:
    left_low, left_high = left
    right_low, right_high = right
    if left_low is None or right_low is None:
        return False

    left_high = float("inf") if left_high is None else left_high
    right_high = float("inf") if right_high is None else right_high
    return max(left_low, right_low) <= min(left_high, right_high)


def _title_matches_icp(title: str, icp: dict | None = None) -> bool:
    """Check if a title matches any ICP allowlist pattern."""
    title_lower = title.lower()
    for pattern in _get_allowed_titles(icp):
        if pattern.lower() in title_lower:
            return True
    return False


def _is_excluded(lead: dict) -> bool:
    """Check if a lead matches any hard exclusion criteria."""
    title_lower = (lead.get("title") or "").lower()
    company_lower = (lead.get("company_name") or "").lower()
    combined = f"{title_lower} {company_lower}"

    for keyword in EXCLUSION_KEYWORDS:
        if keyword in combined:
            return True
    return False


def _is_free_mail(email: str) -> bool:
    """Check if an email is from a free-mail provider (not a business domain)."""
    if "@" not in email:
        return True
    domain = email.split("@", 1)[1].split(".")[0].lower()
    return domain in FREE_MAIL_DOMAINS


def _company_size_excluded(size_str: str, icp: dict | None = None) -> bool:
    """Returns True if the company size is clearly outside ICP range."""
    if not size_str:
        return False
    size_lower = size_str.lower().strip()
    allowed_sizes = _get_allowed_company_sizes(icp)
    if allowed_sizes:
        parsed_size = _parse_company_size_bounds(size_lower)
        if parsed_size[0] is not None:
            for allowed in allowed_sizes:
                if _size_ranges_overlap(parsed_size, _parse_company_size_bounds(allowed)):
                    return False
            return True
        return not any(allowed in size_lower for allowed in allowed_sizes)

    parsed_size = _parse_company_size_bounds(size_lower)
    if parsed_size[0] is not None:
        low, high = parsed_size
        if low is not None and high is not None and low == high and low <= 1:
            return True
        if low is not None and low >= 5001:
            return True
    for excl in _COMPANY_SIZE_EXCLUSIONS:
        if excl in size_lower:
            return True
    return False


def _industry_matches_icp(industry: str, icp: dict | None = None) -> bool:
    """Check if the company industry aligns with our ICP."""
    if not industry:
        return True  # unknown industry is not a disqualifier
    return industry.lower().strip() in _get_allowed_industries(icp)


def icp_pretest(leads: list[dict], icp: dict | None = None) -> tuple[bool, float, dict]:
    """
    Run ICP alignment check on up to 100 leads.
    Checks: title match, exclusion keywords, free-mail email, company size, industry.
    Returns (passed, pass_rate, failure_analysis).
    """
    sample = leads[:PRETEST_SAMPLE_SIZE]
    passed = 0
    excluded_reasons = {
        "no_title": 0, "title_mismatch": 0, "exclusion_hit": 0,
        "free_mail": 0, "bad_company_size": 0, "industry_mismatch": 0,
    }
    failing_titles = Counter()
    failing_industries = Counter()
    failing_company_sizes = Counter()

    def _record_failure(lead: dict):
        title = (lead.get("title") or "").strip()
        if title:
            failing_titles[title] += 1
        industry = (lead.get("company_industry") or "").strip().lower()
        if industry:
            failing_industries[industry] += 1
        company_size = (lead.get("company_size") or "").strip()
        if company_size:
            failing_company_sizes[company_size] += 1

    for lead in sample:
        email = lead.get("email", "")
        title = lead.get("title", "")

        if _is_free_mail(email):
            excluded_reasons["free_mail"] += 1
            _record_failure(lead)
            continue
        if not title:
            excluded_reasons["no_title"] += 1
            _record_failure(lead)
            continue
        if _is_excluded(lead):
            excluded_reasons["exclusion_hit"] += 1
            _record_failure(lead)
            continue
        if _company_size_excluded(lead.get("company_size", ""), icp):
            excluded_reasons["bad_company_size"] += 1
            _record_failure(lead)
            continue
        if not _industry_matches_icp(lead.get("company_industry", ""), icp):
            excluded_reasons["industry_mismatch"] += 1
            _record_failure(lead)
            continue
        if _title_matches_icp(title, icp):
            passed += 1
        else:
            excluded_reasons["title_mismatch"] += 1
            _record_failure(lead)

    pass_rate = passed / len(sample) if sample else 0
    is_pass = pass_rate >= ICP_PASS_THRESHOLD
    dominant_failure_reason = None
    if any(excluded_reasons.values()):
        dominant_failure_reason = max(excluded_reasons.items(), key=lambda item: item[1])[0]

    failure_analysis = {
        "pass_rate": pass_rate,
        "passed": passed,
        "sample_size": len(sample),
        **excluded_reasons,
        "dominant_failure_reason": dominant_failure_reason,
        "mismatched_titles_sample": [title for title, _count in failing_titles.most_common(10)],
        "failed_industries_sample": [industry for industry, _count in failing_industries.most_common(10)],
        "failed_company_sizes_sample": [size for size, _count in failing_company_sizes.most_common(10)],
    }

    log.info(
        "pretest.result",
        sample_size=len(sample),
        passed=passed,
        pass_rate=round(pass_rate * 100, 1),
        threshold=ICP_PASS_THRESHOLD * 100,
        verdict="PASS" if is_pass else "FAIL",
        no_title=excluded_reasons["no_title"],
        title_mismatch=excluded_reasons["title_mismatch"],
        exclusion_hit=excluded_reasons["exclusion_hit"],
        free_mail=excluded_reasons["free_mail"],
        bad_company_size=excluded_reasons["bad_company_size"],
        industry_mismatch=excluded_reasons["industry_mismatch"],
    )

    return is_pass, pass_rate, failure_analysis


# ---------------------------------------------------------------------------
# Lead normalization + deduplication
# ---------------------------------------------------------------------------

def _str(val) -> str:
    """Safely coerce any value to a stripped string."""
    if val is None:
        return ""
    return str(val).strip()


FREE_MAIL_DOMAINS = {
    "gmail", "yahoo", "outlook", "hotmail", "aol", "icloud", "protonmail",
    "mail", "yandex", "zoho", "gmx", "live", "msn", "me", "comcast",
    "att", "verizon", "cox", "charter", "sbcglobal", "bellsouth",
}

# Suffixes stripped during company name simplification (order matters: longer first)
_COMPANY_SUFFIXES = [
    "incorporated", "corporation", "limited liability company",
    "limited liability", "limited", "company",
    "llc", "inc.", "inc", "corp.", "corp", "ltd.", "ltd", "co.", "co",
    "l.l.c.", "l.l.c", "p.c.", "pllc",
    "group", "holdings", "enterprises", "international", "worldwide",
    "associates", "partners", "consulting", "consultants",
    "solutions", "services", "technologies", "technology",
    "digital", "media", "agency", "studio", "studios",
    "communications", "productions", "creative", "strategies",
    "global", "network", "networks",
]


_TLDS = frozenset({".com", ".net", ".org", ".io", ".co", ".ai", ".biz", ".info", ".us", ".ca", ".uk"})

_PREPOSITIONS_ARTICLES = frozenset({
    "in", "of", "for", "at", "by", "on", "the", "a", "an", "to", "and", "&",
})


def _simplify_company_name(name: str) -> str:
    """
    Shorten a company name to 1-2 clean words for cold email use.
    'Legendary Marketing - the world's leader in Golf Resort...' → 'Legendary Marketing'
    'Local Results Agency LLC' → 'Local Results'
    'SWX Global Design and Production' → 'SWX Global'
    'Pancoemedia' → 'Pancoemedia' (single word, suffix is embedded — do NOT strip)
    'Women In Automotive' → 'WIA' (abbreviate when 2-word cap would leave a fragment)
    'Tapinto.net' → 'Tapinto' (strip TLD extensions from source-provided domain names)
    """
    if not name:
        return name

    # Strip TLD extensions from company names provided as domains
    for tld in _TLDS:
        if name.lower().endswith(tld):
            name = name[:-len(tld)]
            break

    for sep in [" - ", " – ", " — ", " | ", " : "]:
        if sep in name:
            name = name.split(sep)[0].strip()

    # Remove suffixes only when they appear as whole trailing words
    # (preceded by a space), and only while the name still has >2 words.
    # This prevents stripping brand-meaningful words from short names
    # like "Capital Media" or "SWX Global".
    lower = name.lower()
    changed = True
    while changed:
        changed = False
        if len(name.split()) <= 2:
            break
        for suffix in _COMPANY_SUFFIXES:
            trailing = " " + suffix
            if lower.endswith(trailing):
                cut_point = len(lower) - len(trailing)
                prefix = name[:cut_point].rstrip(" ,.")
                if prefix:
                    name = prefix
                    lower = name.lower()
                    changed = True
                    break

    # Always strip legal suffixes regardless of word count
    _LEGAL_ONLY = ["llc", "inc.", "inc", "corp.", "corp", "ltd.", "ltd",
                   "co.", "l.l.c.", "l.l.c", "p.c.", "pllc",
                   "incorporated", "corporation", "limited liability company",
                   "limited liability", "limited"]
    lower = name.lower()
    for suffix in _LEGAL_ONLY:
        trailing = " " + suffix
        if lower.endswith(trailing):
            name = name[:len(lower) - len(trailing)].rstrip(" ,.")
            break

    name = name.strip(" ,.-&")

    words = name.split()
    if len(words) > 2:
        # If word at index 1 is a preposition/article, the 2-word cap would
        # create a fragment ("Women In"). Abbreviate to initials instead —
        # looks more human, like someone who knows the company ("WIA").
        if words[1].lower() in _PREPOSITIONS_ARTICLES:
            abbrev = "".join(w[0].upper() for w in words)
            if len(abbrev) >= 2:
                name = abbrev
            else:
                name = " ".join(words[:3])
        else:
            name = " ".join(words[:2])

    # Final cleanup: trailing punctuation, ampersands, commas
    name = name.strip(" ,.-&;:")
    # If we end up with a single non-word token like "&", drop it
    final_words = name.split()
    if final_words and final_words[-1] in ("&", "and", ",", "-", "+"):
        name = " ".join(final_words[:-1])

    # Normalize short acronyms to uppercase (e.g. "Hds" → "HDS")
    cleaned = name.strip()
    if len(cleaned) <= 4 and cleaned.replace(".", "").replace("+", "").isalpha():
        cleaned = cleaned.upper()
        return cleaned

    return name.strip()


def _company_from_domain(email: str) -> str:
    """Best-effort company name from email domain (e.g. 'wcdenterprises.com' -> 'Wcdenterprises')."""
    if "@" not in email:
        return ""
    domain = email.split("@", 1)[1].split(".")[0]
    if domain.lower() in FREE_MAIL_DOMAINS:
        return ""
    return domain.title()


def _normalize_lead(raw: dict) -> dict:
    """Map any source's raw output to the standard lead schema."""
    email = _str(raw.get("email") or raw.get("Email") or "").lower()
    company = _str(raw.get("company_name") or raw.get("companyName") or raw.get("organization_name") or "")
    if not company and email:
        company = _company_from_domain(email)
        if company:
            log.debug("source.company_from_domain", email=email, derived=company)

    company = _simplify_company_name(company)

    first_name = _str(raw.get("first_name") or raw.get("firstName") or "")
    last_name = _str(raw.get("last_name") or raw.get("lastName") or "")
    full_name = _str(raw.get("full_name") or raw.get("fullName") or raw.get("name") or "")

    # Fallback to full_name when first/last split is incomplete
    if first_name and not last_name and full_name:
        parts = full_name.strip().split()
        if len(parts) >= 2:
            first_name = parts[0]
            last_name = " ".join(parts[1:])
    elif not first_name and last_name:
        first_name, last_name = last_name, first_name
        if full_name:
            parts = full_name.strip().split()
            if len(parts) >= 2:
                first_name = parts[0]
                last_name = " ".join(parts[1:])
    elif not first_name and not last_name and full_name:
        parts = full_name.strip().split()
        if len(parts) >= 2:
            first_name = parts[0]
            last_name = " ".join(parts[1:])
        elif len(parts) == 1:
            first_name = parts[0]

    name_review_needed = bool(first_name and not last_name)

    return {
        "email": email,
        "first_name": first_name,
        "last_name": last_name,
        "company_name": company,
        "name_review_needed": name_review_needed,
        "title": _str(raw.get("title") or raw.get("job_title") or raw.get("jobTitle") or ""),
        "linkedin_url": _str(raw.get("linkedin_url") or raw.get("linkedinUrl") or raw.get("linkedin_profile_url") or raw.get("linkedin") or ""),
        "company_website": _str(raw.get("company_website") or raw.get("companyWebsite") or raw.get("website_url") or ""),
        "email_status": _str(raw.get("email_status") or raw.get("emailStatus") or "unknown").lower(),
        "company_size": _str(raw.get("company_size") or raw.get("companySize") or raw.get("organization_num_employees_ranges") or ""),
        "company_industry": _str(raw.get("company_industry") or raw.get("companyIndustry") or raw.get("organization_industry") or raw.get("industry") or ""),
        "linkedin_headline": _str(raw.get("headline") or ""),
        "company_description": _str(raw.get("company_description") or ""),
    }


def _dedupe_leads(leads: list[dict]) -> list[dict]:
    """Remove duplicate leads by email address."""
    seen = set()
    unique = []
    for lead in leads:
        email = lead.get("email", "").lower()
        if email and email not in seen:
            seen.add(email)
            unique.append(lead)
    return unique


# ---------------------------------------------------------------------------
# Apify query building + refinement
# ---------------------------------------------------------------------------

# Progressive title refinement: each attempt narrows to higher-signal titles
_TITLE_REFINEMENT_TIERS = [
    # Tier 0: whatever the query produces (initial)
    None,
    # Tier 1: core decision-maker titles only
    ["CEO", "Founder", "Co-Founder", "Owner", "COO", "Managing Director", "President"],
    # Tier 2: tighter — founder/CEO only
    ["CEO", "Founder", "Co-Founder", "Owner", "Managing Partner"],
    # Tier 3: add ops/growth back with explicit seniority
    ["CEO", "Founder", "Co-Founder", "Owner", "Head of Operations", "Head of Growth", "VP Growth"],
    # Tier 4: widest net of ICP titles
    ["CEO", "Founder", "Co-Founder", "Owner", "COO", "President",
     "Managing Director", "Managing Partner", "Partner",
     "Head of Operations", "Director of Operations",
     "Head of Revenue Operations", "Head of Growth", "VP Growth",
     "Head of Sales", "VP Sales"],
]

_INDUSTRY_REFINEMENT = [
    "marketing & advertising",
    "marketing & advertising",
    "internet",
    "marketing & advertising",
    "marketing & advertising",
]


def _infer_seniority(query: str) -> list[str]:
    """Extract seniority levels from a natural-language query."""
    query_lower = query.lower()
    seniorities = set()
    for keyword, level in SENIORITY_MAP.items():
        if keyword in query_lower:
            seniorities.add(level)
    return list(seniorities) if seniorities else ["founder", "owner", "c_suite", "director", "vp"]


def _infer_titles(query: str) -> list[str]:
    """Extract title keywords from a query for the Apify filter."""
    query_lower = query.lower()
    matched = []
    for _group, patterns in TITLE_PATTERNS.items():
        for pattern in patterns:
            if pattern.lower() in query_lower:
                matched.append(pattern)
    return matched if matched else []


def _refinement_titles(attempt: int, icp: dict | None = None) -> list[str]:
    if attempt <= 0:
        return []

    if attempt < len(_TITLE_REFINEMENT_TIERS) and _TITLE_REFINEMENT_TIERS[attempt]:
        allowed = {title.lower() for title in _get_allowed_titles(icp)}
        filtered = [title for title in _TITLE_REFINEMENT_TIERS[attempt] if title.lower() in allowed]
        if filtered:
            return filtered

    allowed_titles = _get_allowed_titles(icp)
    return allowed_titles[: min(len(allowed_titles), 7)]


def _refinement_industry(attempt: int, query: str, icp: dict | None = None) -> str:
    allowed_industries = _get_allowed_industry_list(icp)
    query_lower = query.lower()

    if "performance marketing" in query_lower or "agency" in query_lower or "marketing" in query_lower or "advertising" in query_lower:
        preferred = "marketing & advertising"
        if preferred in allowed_industries:
            return preferred
    if "saas" in query_lower or "software" in query_lower:
        preferred = "computer software"
        if preferred in allowed_industries:
            return preferred

    if allowed_industries:
        idx = min(attempt, len(allowed_industries) - 1)
        return allowed_industries[idx]

    industry_idx = min(attempt, len(_INDUSTRY_REFINEMENT) - 1)
    return _INDUSTRY_REFINEMENT[industry_idx]


def _normalize_profile(profile: dict | None) -> dict:
    normalized = {}
    if not profile:
        return normalized

    for key in (
        "contact_job_title",
        "company_industry",
        "seniority_level",
        "organization_num_employees_ranges",
    ):
        values = profile.get(key)
        if not values:
            continue
        if isinstance(values, str):
            values = [values]
        cleaned = [str(value).strip() for value in values if str(value).strip()]
        if cleaned:
            normalized[key] = cleaned
    return normalized


def _profile_signature(profile: dict) -> tuple:
    return tuple(
        (key, tuple(profile.get(key, [])))
        for key in (
            "contact_job_title",
            "company_industry",
            "seniority_level",
            "organization_num_employees_ranges",
        )
    )


def _query_profile_from_query(query: str, icp: dict | None = None) -> dict:
    if not query:
        return {}

    params = _build_apify_params(
        query=query,
        location="United States",
        count=PRETEST_SAMPLE_SIZE,
        attempt=0,
        icp=icp,
    )
    params.pop("contact_location", None)
    params.pop("email_status", None)
    params.pop("fetch_count", None)
    return _normalize_profile(params)


def _build_pretest_profiles(
    query: str,
    icp: dict | None = None,
    seed_profile: dict | None = None,
) -> list[dict]:
    profiles = []
    seen = set()

    def _push(params: dict, label: str, query_rotation_index: int | None):
        normalized = _normalize_profile(params)
        if not normalized:
            return
        signature = _profile_signature(normalized)
        if signature in seen:
            return
        seen.add(signature)
        profiles.append(
            {
                "label": label,
                "query_rotation_index": query_rotation_index,
                "params": normalized,
            }
        )

    if seed_profile:
        _push(seed_profile, "rotated-profile", None)

    query_profile = _query_profile_from_query(query, icp)
    if query_profile:
        _push(query_profile, "query-derived", None)

    if icp:
        for idx, profile in enumerate(icp.get("query_rotation", [])):
            _push(profile, f"query-rotation-{idx + 1}", idx)

    if not profiles:
        fallback = _query_profile_from_query(query or "founder ceo owner united states", icp)
        if fallback:
            _push(fallback, "fallback", None)

    return profiles


def _prioritize_titles(titles: list[str]) -> list[str]:
    priority_tokens = (
        "founder",
        "owner",
        "ceo",
        "president",
        "managing director",
        "managing partner",
        "coo",
        "partner",
    )
    priority = [title for title in titles if any(token in title.lower() for token in priority_tokens)]
    secondary = [title for title in titles if title not in priority]
    return priority + secondary


def _build_profile_attempt_params(
    profile: dict,
    location: str,
    count: int,
    refinement_round: int,
    icp: dict | None = None,
    analysis: dict | None = None,
) -> dict:
    run_input = {
        "contact_location": [location.lower()] if isinstance(location, str) else [l.lower() for l in location],
        "email_status": ["validated"],
        "fetch_count": count,
    }

    allowed_sizes = profile["params"].get("organization_num_employees_ranges") or list(_get_allowed_company_sizes(icp))
    if allowed_sizes:
        run_input["organization_num_employees_ranges"] = allowed_sizes

    title_pool = profile["params"].get("contact_job_title") or _get_allowed_titles(icp)
    title_pool = _prioritize_titles(list(dict.fromkeys(title_pool)))
    if title_pool:
        title_noise = 0
        if analysis:
            title_noise = (
                analysis.get("title_mismatch", 0)
                + analysis.get("exclusion_hit", 0)
                + analysis.get("no_title", 0)
            )
        if refinement_round == 0 and not analysis:
            selected_titles = title_pool
        else:
            if title_noise >= max((analysis or {}).get("industry_mismatch", 0), (analysis or {}).get("bad_company_size", 0)):
                limit = 4 if refinement_round >= 1 else 6
                selected_titles = title_pool[: min(len(title_pool), limit)]
            else:
                limit = 6 if refinement_round == 0 else 5
                selected_titles = title_pool[: min(len(title_pool), limit)]
        run_input["contact_job_title"] = selected_titles

    industry_pool = profile["params"].get("company_industry") or _get_allowed_industry_list(icp)
    if industry_pool:
        industry_idx = min(refinement_round, len(industry_pool) - 1)
        if analysis and analysis.get("industry_mismatch", 0):
            industry_idx = min(refinement_round + 1, len(industry_pool) - 1)
        run_input["company_industry"] = [industry_pool[industry_idx]]

    base_seniority = profile["params"].get("seniority_level") or ["founder", "owner", "c_suite", "director", "head", "vp"]
    title_noise = 0
    if analysis:
        title_noise = (
            analysis.get("title_mismatch", 0)
            + analysis.get("exclusion_hit", 0)
            + analysis.get("no_title", 0)
        )
    if title_noise or refinement_round >= 1:
        run_input["seniority_level"] = [level for level in base_seniority if level in {"founder", "owner", "c_suite", "director", "head", "vp"}]
    else:
        run_input["seniority_level"] = base_seniority

    return run_input


def _should_switch_profile(analysis: dict | None, refinement_round: int) -> bool:
    if not analysis:
        return False

    sample_size = max(analysis.get("sample_size", 0), 1)
    pass_rate = analysis.get("pass_rate", 0.0)
    dominant_reason = analysis.get("dominant_failure_reason")

    if dominant_reason == "industry_mismatch":
        return True
    if analysis.get("bad_company_size", 0) >= sample_size * 0.5:
        return True
    if pass_rate == 0 and refinement_round >= 1:
        return True
    if pass_rate < 0.25 and refinement_round >= 2:
        return True

    return False


def _build_apify_params(
    query: str,
    location: str,
    count: int,
    attempt: int = 0,
    icp: dict | None = None,
) -> dict:
    """
    Build Apify run_input params. On attempt 0 uses the raw query.
    On subsequent attempts, progressively refines title and industry filters.
    """
    seniorities = _infer_seniority(query)

    run_input = {
        "contact_location": [location.lower()] if isinstance(location, str) else [l.lower() for l in location],
        "seniority_level": seniorities,
        "email_status": ["validated"],
        "fetch_count": count,
    }

    # Title filter: use refinement tier if available for this attempt
    refined_titles = _refinement_titles(attempt, icp)
    if refined_titles:
        run_input["contact_job_title"] = refined_titles
        log.info("pretest.refine_titles", attempt=attempt, titles=refined_titles)
    else:
        titles = _infer_titles(query)
        if titles:
            run_input["contact_job_title"] = titles

    run_input["company_industry"] = [_refinement_industry(attempt, query, icp)]

    if attempt >= 2:
        run_input["seniority_level"] = ["founder", "owner", "c_suite"]

    query_lower = query.lower()
    allowed_sizes = list(_get_allowed_company_sizes(icp))
    if allowed_sizes:
        run_input["organization_num_employees_ranges"] = allowed_sizes
    elif any(term in query_lower for term in ["smb", "small", "medium", "mid market", "midmarket"]):
        run_input["organization_num_employees_ranges"] = ["11-50", "51-200", "201-500"]

    return run_input


# ---------------------------------------------------------------------------
# Source 1: Apify Leads Finder (with pretest loop)
# ---------------------------------------------------------------------------

# Hard ceiling on any single actor run. Cannot be bypassed by caller params.
# Chosen so a single runaway fetch tops out at ~$40 at the $0.02/lead rate —
# well below the Apify per-run cost cap, but enough headroom for legitimate
# large batches. Override with APIFY_HARD_FETCH_CAP env var if truly needed.
APIFY_HARD_FETCH_CAP = int(os.getenv("APIFY_HARD_FETCH_CAP", "2000"))


def _abort_apify_run(client: ApifyClient, run_id: str, reason: str) -> None:
    """
    Abort an Apify actor run and surface failures loudly.

    Why this exists: the previous implementation swallowed abort errors with
    a bare `except Exception: pass`, which allowed the actor to keep running
    (and charging) after our local code believed it had stopped it. On one
    occasion this produced a 36-minute, 11,691-lead, $23 runaway. If the
    first abort call fails we retry once; if both fail we raise so the
    caller halts the pipeline instead of continuing unattended.
    """
    last_err = None
    for attempt in (1, 2):
        try:
            client.run(run_id).abort()
            log.info("apify.aborted", run_id=run_id, reason=reason, attempt=attempt)
            return
        except Exception as err:
            last_err = err
            log.error("apify.abort_failed", run_id=run_id, attempt=attempt, error=str(err))
    raise RuntimeError(
        f"Failed to abort Apify run {run_id} after 2 attempts (reason: {reason}): {last_err}. "
        f"Check the Apify console immediately to stop the actor manually."
    )


def _fetch_from_apify(run_input: dict, client: ApifyClient) -> list[dict]:
    """
    Start the Apify actor, poll the dataset, and abort the run as soon as
    we have enough leads. This prevents the actor from over-fetching.

    IMPORTANT: the actor's result-limit parameter is `fetch_count`, not
    `max_results`. Sending the wrong name causes the actor to default to
    100,000 — see the 2026-04-14 incident. `run_input["fetch_count"]` is
    required and capped at APIFY_HARD_FETCH_CAP before the run starts.
    """
    import time as _time

    requested = run_input.get("fetch_count")
    if requested is None:
        raise ValueError(
            "fetch_count missing from Apify run_input. Without it the actor defaults "
            "to 100,000 leads and will run up a catastrophic bill."
        )
    if requested > APIFY_HARD_FETCH_CAP:
        raise ValueError(
            f"fetch_count={requested} exceeds APIFY_HARD_FETCH_CAP={APIFY_HARD_FETCH_CAP}. "
            f"Refusing to start actor. If this is genuinely needed, raise the env var."
        )

    cap = requested
    log.info("apify.starting", actor="code_crafter~leads-finder", cap=cap, params={
        k: v for k, v in run_input.items() if k != "email_status"
    })

    actor_client = client.actor("code_crafter~leads-finder")
    run_info = actor_client.start(run_input=run_input)
    run_id = run_info["id"]
    dataset_id = run_info["defaultDatasetId"]

    log.info("apify.run_started", run_id=run_id)

    items = []
    poll_interval = 5
    max_wait_seconds = 300
    deadline = _time.time() + max_wait_seconds

    while _time.time() < deadline:
        _time.sleep(poll_interval)

        items = list(client.dataset(dataset_id).iterate_items())

        run_status = client.run(run_id).get().get("status", "")
        elapsed = int(_time.time() - (deadline - max_wait_seconds))
        log.info("apify.polling", elapsed=elapsed, items=len(items), status=run_status)

        if len(items) >= cap:
            log.info("apify.cap_reached", items=len(items), cap=cap, msg="Aborting actor run")
            _abort_apify_run(client, run_id, reason="cap_reached")
            break

        if run_status in ("SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT"):
            log.info("apify.run_finished", status=run_status, items=len(items))
            break
    else:
        log.warning("apify.timeout", msg=f"Actor did not finish in {max_wait_seconds}s, aborting")
        _abort_apify_run(client, run_id, reason="wall_clock_timeout")

    items = items[:cap]
    log.info("apify.complete", leads_fetched=len(items))
    return [_normalize_lead(item) for item in items]


def source_from_apify(
    query: str,
    location: str = "United States",
    count: int = 100,
    reuse_run_id: Optional[str] = None,
    icp: Optional[dict] = None,
    seed_profile: Optional[dict] = None,
) -> tuple[list[dict], dict]:
    """
    Fetch leads from Apify with ICP pretest loop.
    1. Fetch 100 leads
    2. Run ICP pretest
    3. If >= 75% pass: fetch the rest with same params, return all
    4. If < 75%: refine params and retry (up to MAX_PRETEST_ATTEMPTS)

    Returns (leads, pretest_info).
    """
    token = os.getenv("APIFY_API_TOKEN") or os.getenv("APIFY_API_KEY")
    if not token:
        raise RuntimeError("APIFY_API_TOKEN not set in .env")

    client = ApifyClient(token)

    if reuse_run_id:
        log.info("apify.reuse_run", run_id=reuse_run_id)
        dataset = client.run(reuse_run_id).dataset()
        items = list(dataset.iterate_items())
        log.info("apify.reuse_complete", leads=len(items))
        leads = [_normalize_lead(item) for item in items]

        is_pass, pass_rate, analysis = icp_pretest(leads, icp=icp)
        pretest_info = {"passed": is_pass, "pass_rate": pass_rate, "attempts": 1, "reused_run": True}
        if not is_pass:
            raise RuntimeError(
                f"ICP pretest failed on reused run ({pass_rate * 100:.1f}% < 75%). "
                f"Can't refine — the run was pre-fetched. Try a fresh query instead."
            )
        return leads, pretest_info

    # --- Pretest loop ---
    winning_params = None
    winning_pretest_leads = []
    winning_profile = None
    pass_rate = 0.0
    attempt_history = []
    total_attempts = 0
    last_analysis = None
    profiles = _build_pretest_profiles(query, icp=icp, seed_profile=seed_profile)

    for profile_idx, profile in enumerate(profiles):
        profile_analysis = None
        for refinement_round in range(MAX_PROFILE_REFINEMENTS):
            if total_attempts >= MAX_PRETEST_ATTEMPTS:
                break

            total_attempts += 1
            log.info(
                "pretest.attempt",
                attempt=total_attempts,
                max=MAX_PRETEST_ATTEMPTS,
                profile=profile["label"],
                refinement_round=refinement_round + 1,
            )

            pretest_params = _build_profile_attempt_params(
                profile,
                location=location,
                count=PRETEST_SAMPLE_SIZE,
                refinement_round=refinement_round,
                icp=icp,
                analysis=profile_analysis,
            )
            pretest_leads = _fetch_from_apify(pretest_params, client)

            if not pretest_leads:
                attempt_history.append(
                    {
                        "attempt": total_attempts,
                        "profile": profile["label"],
                        "query_rotation_index": profile.get("query_rotation_index"),
                        "refinement_round": refinement_round + 1,
                        "result": "no_leads",
                    }
                )
                log.warning(
                    "pretest.no_leads",
                    attempt=total_attempts,
                    profile=profile["label"],
                    msg="Query profile returned 0 leads, moving to the next profile",
                )
                break

            is_pass, pass_rate, analysis = icp_pretest(pretest_leads, icp=icp)
            last_analysis = analysis
            attempt_history.append(
                {
                    "attempt": total_attempts,
                    "profile": profile["label"],
                    "query_rotation_index": profile.get("query_rotation_index"),
                    "refinement_round": refinement_round + 1,
                    "pass_rate": pass_rate,
                    "dominant_failure_reason": analysis.get("dominant_failure_reason"),
                }
            )

            if is_pass:
                log.info(
                    "pretest.passed",
                    attempt=total_attempts,
                    pass_rate=round(pass_rate * 100, 1),
                    profile=profile["label"],
                )
                winning_params = pretest_params
                winning_pretest_leads = pretest_leads
                winning_profile = profile
                break

            profile_analysis = analysis
            log.warning(
                "pretest.failed_retrying",
                attempt=total_attempts,
                profile=profile["label"],
                pass_rate=round(pass_rate * 100, 1),
                title_mismatches=analysis["title_mismatch"],
                industry_mismatches=analysis["industry_mismatch"],
                exclusions=analysis["exclusion_hit"],
                bad_company_size=analysis["bad_company_size"],
                dominant_failure_reason=analysis.get("dominant_failure_reason"),
            )

            if _should_switch_profile(analysis, refinement_round):
                log.info(
                    "pretest.profile_switch_reason",
                    profile=profile["label"],
                    refinement_round=refinement_round + 1,
                    pass_rate=round(pass_rate * 100, 1),
                    dominant_failure_reason=analysis.get("dominant_failure_reason"),
                )
                break

        if winning_params is not None or total_attempts >= MAX_PRETEST_ATTEMPTS:
            break

        if profile_idx < len(profiles) - 1:
            log.info("pretest.profile_switch", from_profile=profile["label"], to_profile=profiles[profile_idx + 1]["label"])

    if winning_params is None:
        raise RuntimeError(
            f"ICP pretest failed after {total_attempts} attempts across {len(profiles)} query profiles. "
            f"Last pass rate: {pass_rate * 100:.1f}%. "
            f"Last dominant failure: {(last_analysis or {}).get('dominant_failure_reason') or 'unknown'}. "
            f"Refine your --query or tighten the campaign ICP."
        )

    # --- Fetch remaining leads with winning params ---
    remaining_count = max(count - len(winning_pretest_leads), 0)

    if remaining_count > 0:
        existing_emails = {l["email"] for l in winning_pretest_leads}

        full_params = dict(winning_params)
        full_params["fetch_count"] = remaining_count

        full_leads = _fetch_from_apify(full_params, client)

        new_leads = [l for l in full_leads if l["email"] not in existing_emails]
        new_leads = new_leads[:remaining_count]
        all_leads = winning_pretest_leads + new_leads
        log.info("apify.merged", pretest=len(winning_pretest_leads), new=len(new_leads), total=len(all_leads))
    else:
        all_leads = winning_pretest_leads

    pretest_info = {
        "passed": True,
        "pass_rate": pass_rate,
        "attempts": total_attempts,
        "attempt_history": attempt_history,
        "winning_profile": winning_profile["label"] if winning_profile else None,
        "winning_query_rotation_index": winning_profile.get("query_rotation_index") if winning_profile else None,
        "winning_params": {k: v for k, v in winning_params.items() if k != "email_status"},
    }

    return all_leads, pretest_info


# ---------------------------------------------------------------------------
# Source 2: Apollo Free Search
# ---------------------------------------------------------------------------

def source_from_apollo(
    query: str,
    location: str = "United States",
    count: int = 100,
) -> list[dict]:
    """
    Apollo's free people search endpoint (no credits consumed).
    Supplements the primary Apify source.
    """
    api_key = os.getenv("APOLLO_API_KEY")
    if not api_key:
        log.warning("apollo.no_api_key", msg="APOLLO_API_KEY not set, skipping Apollo")
        return []

    url = "https://api.apollo.io/v1/mixed_people/search"
    headers = {"Content-Type": "application/json", "Cache-Control": "no-cache"}

    payload = {
        "api_key": api_key,
        "q_keywords": query,
        "person_locations": [location],
        "page": 1,
        "per_page": min(count, 100),
    }

    log.info("apollo.searching", query=query, count=count)

    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        people = data.get("people", [])

        leads = []
        for person in people:
            raw = {
                "email": person.get("email", ""),
                "first_name": person.get("first_name", ""),
                "last_name": person.get("last_name", ""),
                "title": person.get("title", ""),
                "linkedin_url": person.get("linkedin_url", ""),
                "organization_name": person.get("organization", {}).get("name", ""),
                "company_website": person.get("organization", {}).get("website_url", ""),
                "email_status": "not_validated",
                "organization_num_employees_ranges": person.get("organization", {}).get("estimated_num_employees", ""),
                "organization_industry": person.get("organization", {}).get("industry", ""),
            }
            raw["company_name"] = raw.pop("organization_name", "")
            leads.append(_normalize_lead(raw))

        log.info("apollo.complete", leads_found=len(leads))
        return leads

    except requests.RequestException as e:
        log.error("apollo.error", error=str(e))
        return []


# ---------------------------------------------------------------------------
# Source 3: CSV Import
# ---------------------------------------------------------------------------

def source_from_csv(csv_path: str) -> list[dict]:
    """
    Import leads from a CSV file.
    Required columns: email, first_name, last_name, company_name, title.
    Optional: linkedin_url, company_website, email_verified.
    """
    if not os.path.exists(csv_path):
        raise FileNotFoundError(f"CSV not found: {csv_path}")

    leads = []
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            email_verified = str(row.get("email_verified", "")).lower() == "true"
            raw = {
                "email": row.get("email", ""),
                "first_name": row.get("first_name", ""),
                "last_name": row.get("last_name", ""),
                "company_name": row.get("company_name", ""),
                "title": row.get("title", ""),
                "linkedin_url": row.get("linkedin_url", ""),
                "company_website": row.get("company_website", ""),
                "email_status": "validated" if email_verified else "not_validated",
                "company_size": row.get("company_size", ""),
                "company_industry": row.get("company_industry", ""),
            }
            leads.append(_normalize_lead(raw))

    log.info("csv.imported", path=csv_path, leads=len(leads))
    return leads


# ---------------------------------------------------------------------------
# Suppression List
# ---------------------------------------------------------------------------

# Module-level state so the preflight report can inspect the outcome of the
# most recent suppression load. Without this, a failed-to-load suppression list
# looks identical to a genuinely-empty one — which was the H3 finding.
_LAST_SUPPRESSION_STATUS: dict = {
    "loaded": False,
    "count": 0,
    "reason": "not_attempted",
    "error": None,
}


def _load_suppression_list() -> set[str]:
    """Load suppressed emails from Google Sheets Suppression tab."""
    global _LAST_SUPPRESSION_STATUS
    try:
        import gspread
        from google.oauth2.credentials import Credentials

        token_path = os.getenv("GOOGLE_TOKEN_PATH", "./token.json")
        spreadsheet_name = os.getenv("STATE_SPREADSHEET_NAME", "Outbound_Engine_State")

        if not os.path.exists(token_path):
            log.warning("suppression.no_token", msg="Google token not found, skipping suppression check")
            _LAST_SUPPRESSION_STATUS = {"loaded": False, "count": 0, "reason": "no_token", "error": None}
            return set()

        creds = Credentials.from_authorized_user_file(token_path)
        gc = gspread.authorize(creds)

        sh = gc.open(spreadsheet_name)
        try:
            ws = sh.worksheet("Suppression")
        except gspread.WorksheetNotFound:
            log.info("suppression.no_tab", msg="Suppression tab not found, creating it")
            ws = sh.add_worksheet(title="Suppression", rows=1000, cols=4)
            ws.update("A1:D1", [["email", "reason", "added_at", "source"]])
            _LAST_SUPPRESSION_STATUS = {"loaded": True, "count": 0, "reason": "tab_created", "error": None}
            return set()

        emails = ws.col_values(1)[1:]  # skip header
        suppressed = {e.strip().lower() for e in emails if e.strip()}
        log.info("suppression.loaded", count=len(suppressed))
        _LAST_SUPPRESSION_STATUS = {"loaded": True, "count": len(suppressed), "reason": "ok", "error": None}
        return suppressed

    except Exception as e:
        log.warning("suppression.load_error", error=str(e), msg="Continuing without suppression list")
        _LAST_SUPPRESSION_STATUS = {"loaded": False, "count": 0, "reason": "load_error", "error": str(e)}
        return set()


def get_suppression_readiness() -> dict:
    """
    Returns the status of the most recent `_load_suppression_list` call.
    Preflight uses this to distinguish a genuinely-empty suppression list from
    one that failed to load (auth missing, API error, etc.).
    """
    return dict(_LAST_SUPPRESSION_STATUS)


def apply_suppression(leads: list[dict]) -> list[dict]:
    """Remove leads whose emails appear on the suppression list."""
    suppressed = _load_suppression_list()
    if not suppressed:
        return leads

    before = len(leads)
    filtered = [l for l in leads if l.get("email", "").lower() not in suppressed]
    removed = before - len(filtered)
    if removed:
        log.info("suppression.removed", removed=removed)
    return filtered


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def source_leads(
    query: Optional[str] = None,
    location: str = "United States",
    count: int = 100,
    csv_path: Optional[str] = None,
    reuse_run_id: Optional[str] = None,
    exclude_emails: Optional[set] = None,
    winning_params: Optional[dict] = None,
    query_profile: Optional[dict] = None,
    icp: Optional[dict] = None,
) -> tuple[list[dict], dict]:
    """
    Source leads from the appropriate provider with embedded ICP pretest.
    Returns (deduplicated + suppression-checked leads, pretest_info).

    For Apify: runs the pretest loop (fetch 100 → check ICP → refine query → retry).
    For CSV/reuse-run: runs pretest once (pass or fail, no refinement possible).

    If winning_params is provided, skips the pretest and fetches directly
    using those params (used for top-up iterations in the target loop).
    exclude_emails filters out already-processed leads.
    """
    pretest_info = {"passed": True, "pass_rate": 1.0, "attempts": 0}

    if csv_path:
        leads = source_from_csv(csv_path)
        if leads:
            is_pass, pass_rate, analysis = icp_pretest(leads, icp=icp)
            pretest_info = {"passed": is_pass, "pass_rate": pass_rate, "attempts": 1, "source": "csv"}
            if not is_pass:
                log.warning(
                    "pretest.csv_warning",
                    pass_rate=round(pass_rate * 100, 1),
                    msg="CSV leads below ICP threshold — proceeding anyway (can't refine CSV)",
                )

    elif winning_params:
        token = os.getenv("APIFY_API_TOKEN") or os.getenv("APIFY_API_KEY")
        if not token:
            raise RuntimeError("APIFY_API_TOKEN not set in .env")
        client = ApifyClient(token)

        fetch_params = dict(winning_params)
        fetch_params["fetch_count"] = count
        log.info("source.topup", count=count, msg="Fetching more leads with winning params (skipping pretest)")
        leads = _fetch_from_apify(fetch_params, client)
        pretest_info = {"passed": True, "pass_rate": 1.0, "attempts": 0, "topup": True}

    elif query or reuse_run_id or query_profile:
        leads, pretest_info = source_from_apify(
            query or "",
            location,
            count,
            reuse_run_id=reuse_run_id,
            icp=icp,
            seed_profile=query_profile,
        )

    else:
        raise ValueError("Must provide either --query or --csv")

    leads = [l for l in leads if l.get("email")]
    leads = _dedupe_leads(leads)
    leads = apply_suppression(leads)

    if exclude_emails:
        before = len(leads)
        leads = [l for l in leads if l["email"] not in exclude_emails]
        excluded = before - len(leads)
        if excluded:
            log.info("source.dedup_cross_iteration", removed=excluded, remaining=len(leads))

    log.info("source.final", total_leads=len(leads), pretest_attempts=pretest_info.get("attempts", 0))
    return leads, pretest_info
