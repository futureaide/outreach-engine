"""
Step 4: Enrich Leads
Four enrichment sources:
  1. LinkedIn Profile Scraper via Apify (primary personalization signal)
  2. Perplexity Sonar (company context)
  3. Website Content Crawler via Apify (company-specific details)
  4. LinkedIn Posts Scraper via Apify (recent activity for personalization hooks)
"""

import os
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
import structlog
from apify_client import ApifyClient
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()
log = structlog.get_logger()

LINKEDIN_ACTOR = "dev_fusion/linkedin-profile-scraper"
LINKEDIN_POSTS_ACTOR = "supreme_coder/linkedin-post"
WEBSITE_CRAWLER_ACTOR = "apify/website-content-crawler"
LINKEDIN_COST_PER_PROFILE = 0.01  # confirmed from Apify dashboard
LINKEDIN_POSTS_COST_PER_PROFILE = 0.03  # estimate
WEBSITE_COST_PER_SITE = 0.02  # estimate
PERPLEXITY_COST_PER_QUERY = 0.0004  # Sonar: $1/MTok in+out, ~300 tokens/query avg


# ---------------------------------------------------------------------------
# Apify cost-safety wrapper
# ---------------------------------------------------------------------------
# History: we've hit THREE separate over-fetch incidents with Apify actors,
# each a different parameter-name bug:
#   2026-04-14: code_crafter/leads-finder used `max_results` (actual: fetch_count),
#               defaulted to 100,000 leads → $23 runaway in 36 min
#   2026-04-20: supreme_coder/linkedin-post missing `limitPerSource`, defaulted
#               to unlimited → ~1,000 posts/run, ~$7 burned across Stages 2-3
#   2026-04-20: apify/website-content-crawler maxCrawlPages defaults to 9,999,999
#               if missing (we had set it, but the pattern is dangerous)
#
# Per-actor cap params don't scale — every new actor has its own convention
# and an unbounded default. This wrapper enforces cost safety POST-run by
# comparing actual itemCount against an explicit `max_expected_items`
# budget the caller MUST declare. If the actor over-fetches for any reason
# (missing cap, API change, upstream bug), we catch it and raise.
def _run_actor_capped(
    client: ApifyClient,
    actor_slug: str,
    run_input: dict,
    max_expected_items: int,
    timeout_secs: int | None = None,
) -> tuple[dict, list]:
    """
    Wrapper around client.actor(slug).call() that enforces a post-run item-count
    budget. Returns (run_info, items). Raises RuntimeError on runaway over-fetch.

    Why post-run check: each actor has its own cap parameter name (fetch_count,
    limitPerSource, maxResults, maxCrawlPages, maxItems...). Relying on the
    caller to set the right one has failed three times now. A dataset
    itemCount sanity check works regardless of which param was missed.
    """
    if max_expected_items <= 0:
        raise ValueError(f"max_expected_items must be > 0, got {max_expected_items}")

    call_kwargs = {"run_input": run_input}
    if timeout_secs is not None:
        call_kwargs["timeout_secs"] = timeout_secs

    run = client.actor(actor_slug).call(**call_kwargs)
    dataset_id = run.get("defaultDatasetId")
    items = list(client.dataset(dataset_id).iterate_items()) if dataset_id else []
    item_count = len(items)

    # Warn at 2x expected (yellow flag), raise at 10x (runaway). The 2x
    # threshold catches drift early; the 10x raise ensures we never quietly
    # burn money on a misconfigured actor.
    if item_count > max_expected_items * 10:
        raise RuntimeError(
            f"Apify actor {actor_slug} returned {item_count} items, "
            f"{item_count / max_expected_items:.1f}x the expected max of {max_expected_items}. "
            f"This is the cost-burn pattern — abort before retrying. "
            f"run_input keys: {list(run_input.keys())}"
        )
    if item_count > max_expected_items * 2:
        log.warning(
            "apify.over_fetch_warning",
            actor=actor_slug,
            items=item_count,
            expected_max=max_expected_items,
            ratio=round(item_count / max_expected_items, 2),
            msg="actor returned more items than expected; verify cap param",
        )

    return run, items


# ---------------------------------------------------------------------------
# LinkedIn Enrichment
# ---------------------------------------------------------------------------

LINKEDIN_BATCH_SIZE = 25


def _parse_linkedin_profile(profile: dict) -> dict:
    """Extract fields from a single LinkedIn profile result."""
    import datetime

    experience = profile.get("experience", [])
    current_tenure = ""
    if experience:
        current = experience[0]
        start = current.get("starts_at", {})
        if start and start.get("year"):
            years = datetime.datetime.now().year - start["year"]
            current_tenure = str(max(years, 0))

    skills = profile.get("skills", [])
    if isinstance(skills, list):
        skill_names = [s if isinstance(s, str) else s.get("name", "") for s in skills[:10]]
        skills_str = ", ".join(s for s in skill_names if s)
    else:
        skills_str = ""

    return {
        "linkedin_headline": profile.get("headline", ""),
        "linkedin_job_description": profile.get("summary", ""),
        "linkedin_skills": skills_str,
        "linkedin_company_size": str(profile.get("company_size", "")),
        "linkedin_company_industry": profile.get("industry", ""),
        "linkedin_tenure_years": current_tenure,
    }


def _scrape_linkedin_batch(urls: list[str], client: ApifyClient) -> dict[str, dict]:
    """Scrape a batch of LinkedIn profiles in a single Apify actor run. Returns {url: profile_data}."""
    try:
        # Actor is 1:1 input→output by design (docs confirm no hidden
        # multiplier param). We allow 2x the URL count as headroom for the
        # occasional duplicate-URL dedupe case.
        _, items = _run_actor_capped(
            client=client,
            actor_slug=LINKEDIN_ACTOR,
            run_input={"profileUrls": urls},
            max_expected_items=max(len(urls) * 2, 4),
        )

        result = {}
        for item in items:
            url = item.get("url", "") or item.get("linkedinUrl", "") or item.get("profileUrl", "")
            if url:
                result[url] = _parse_linkedin_profile(item)
        return result

    except Exception as e:
        log.error("linkedin.batch_error", count=len(urls), error=str(e))
        return {}


def enrich_linkedin(leads: list[dict]) -> tuple[list[dict], float]:
    """
    Enrich leads with LinkedIn profile data.
    Skips leads that already have a linkedin_headline (sourced from Apify).
    Skips leads without a linkedin_url.
    Returns (enriched_leads, cost).
    """
    fully_enriched = [(i, l) for i, l in enumerate(leads)
                      if l.get("linkedin_headline") and l.get("linkedin_skills")]
    needs_scrape = [(i, l) for i, l in enumerate(leads)
                    if l.get("linkedin_url") and not (l.get("linkedin_headline") and l.get("linkedin_skills"))]
    no_data = len(leads) - len(fully_enriched) - len(needs_scrape)

    if fully_enriched:
        log.info("linkedin.already_enriched", count=len(fully_enriched), msg="Headline + skills from source, skipping scrape")

    if no_data:
        log.info("linkedin.no_url_no_headline", count=no_data)

    if not needs_scrape:
        log.info("linkedin.nothing_to_scrape", already=len(fully_enriched))
        return leads, 0.0

    token = os.getenv("APIFY_API_TOKEN") or os.getenv("APIFY_API_KEY")
    if not token:
        raise RuntimeError("APIFY_API_TOKEN not set in .env")

    client = ApifyClient(token)

    cost = len(needs_scrape) * LINKEDIN_COST_PER_PROFILE
    log.info("linkedin.starting", profiles=len(needs_scrape), estimated_cost_usd=round(cost, 2))

    # Batch scrape in groups of LINKEDIN_BATCH_SIZE
    url_to_idx = {lead["linkedin_url"]: idx for idx, lead in needs_scrape}
    all_urls = [lead["linkedin_url"] for _, lead in needs_scrape]
    url_batches = [all_urls[i:i + LINKEDIN_BATCH_SIZE] for i in range(0, len(all_urls), LINKEDIN_BATCH_SIZE)]

    for batch_num, url_batch in enumerate(url_batches):
        log.info("linkedin.batch", batch=batch_num + 1, total=len(url_batches), profiles=len(url_batch))
        results = _scrape_linkedin_batch(url_batch, client)
        for url, profile_data in results.items():
            idx = url_to_idx.get(url)
            if idx is not None:
                leads[idx].update(profile_data)

    enriched_count = sum(1 for i, _ in needs_scrape if leads[i].get("linkedin_headline"))
    log.info("linkedin.complete", scraped=enriched_count, already_had=len(fully_enriched))

    return leads, cost


# ---------------------------------------------------------------------------
# Perplexity Enrichment
# ---------------------------------------------------------------------------

# Meta-language Perplexity uses as reasoning scaffolding. When this leaks
# into the personalization_line, prospects see phrases like "based on the
# search results tends to mean..." in the opener — an immediate AI-tell.
# Found in the 2026-04-20 prospect-POV audit after one prospect got
# "based on the search results" as her Email 1 opener.
import re as _re

_PERPLEXITY_META_PATTERNS = [
    _re.compile(r"^\s*based on (?:the\s+)?(?:search results|sources|my research|information provided|above)[\s,:.\-]+", _re.IGNORECASE),
    _re.compile(r"^\s*according to (?:the\s+)?(?:sources|search results|information|research)[\s,:.\-]+", _re.IGNORECASE),
    _re.compile(r"^\s*the (?:search results|sources|information) (?:indicate|suggest|show)s?[\s,:.\-]+", _re.IGNORECASE),
    _re.compile(r"^\s*(?:from|per) (?:the\s+)?(?:search results|sources|available information)[\s,:.\-]+", _re.IGNORECASE),
    _re.compile(r"^\s*i (?:couldn'?t|cannot|could not|can'?t) find[^.]*\.\s*", _re.IGNORECASE),
]

# Ambiguity disclaimers indicating Perplexity couldn't disambiguate which
# company we meant. Output is not usable as source-of-truth — treat as empty.
_PERPLEXITY_AMBIGUITY_MARKERS = [
    "there are multiple companies",
    "could refer to several",
    "could refer to multiple",
    "the answer depends on which",
    "without more context, i can'",
    "it'?s unclear which",
    "ambiguous reference",
]


def _clean_perplexity_context(raw: str) -> str:
    """
    Strip Perplexity reasoning scaffolding from the front of a response and
    return empty if the response is an ambiguity disclaimer.

    Why: our personalization prompt tells Claude to "prefer raw source wording
    over abstract labels", which is right for LinkedIn headlines but wrong
    when Perplexity prefixes its answer with "Based on the search results...".
    Claude obediently pulls that meta-language into the email opener.
    """
    if not raw:
        return ""
    text = raw.strip()

    lowered = text.lower()
    for marker in _PERPLEXITY_AMBIGUITY_MARKERS:
        if _re.search(marker, lowered):
            return ""

    # Strip markdown bold / italic markers throughout (Perplexity wraps key
    # phrases in **bold** mid-sentence too, not just at the start — one post-production
    # prospect had "specializing in **editing and post-production
    # services**" which tripped the source-anchor extraction).
    text = _re.sub(r"\*+", "", text).strip()

    # Iteratively strip meta-language prefixes (up to 3 rounds in case a
    # response chains "Based on... According to...").
    for _ in range(3):
        before = text
        for pattern in _PERPLEXITY_META_PATTERNS:
            text = pattern.sub("", text).strip()
        if text == before:
            break

    # If cleaning consumed most of the content, the response was mostly
    # scaffolding — not usable.
    if len(text) < 40:
        return ""

    return text


def _query_perplexity(company_name: str, client: OpenAI) -> dict:
    """Query Perplexity Sonar for company context."""
    try:
        response = client.chat.completions.create(
            model="sonar",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a research assistant. Provide concise, factual answers. "
                        "Focus on what the company does, their services, and their target clients. "
                        "Do not preface answers with 'Based on the search results' or similar — "
                        "state facts directly. If you cannot identify the company, reply with only: "
                        "NO_CONTEXT_AVAILABLE"
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"What does {company_name} do? What services do they offer? "
                        f"Who are their typical clients? Keep it to 3-4 sentences."
                    ),
                },
            ],
            max_tokens=300,
        )

        company_context = response.choices[0].message.content.strip()
        if "NO_CONTEXT_AVAILABLE" in company_context:
            return {"perplexity_company_context": ""}
        cleaned = _clean_perplexity_context(company_context)
        return {"perplexity_company_context": cleaned}

    except Exception as e:
        log.error("perplexity.query_error", company=company_name, error=str(e))
        return {"perplexity_company_context": ""}


def enrich_perplexity(leads: list[dict]) -> tuple[list[dict], float]:
    """
    Enrich leads with Perplexity company context.
    Skips leads that already have company_description from the source.
    Returns (enriched_leads, cost).
    """
    # Promote company_description from source into perplexity_company_context
    already_have = 0
    for lead in leads:
        if lead.get("company_description") and not lead.get("perplexity_company_context"):
            lead["perplexity_company_context"] = lead["company_description"]
            already_have += 1

    if already_have:
        log.info("perplexity.from_source", count=already_have, msg="Company description from source, skipping API")

    needs_query = [(i, l) for i, l in enumerate(leads)
                   if l.get("company_name") and not l.get("perplexity_company_context")]

    if not needs_query:
        log.info("perplexity.nothing_to_query", already=already_have)
        return leads, 0.0

    api_key = os.getenv("PERPLEXITY_API_KEY")
    if not api_key:
        log.warning("perplexity.no_api_key", msg="PERPLEXITY_API_KEY not set, skipping")
        return leads, 0.0

    client = OpenAI(
        api_key=api_key,
        base_url="https://api.perplexity.ai",
    )

    # Dedupe companies first
    company_to_indices = {}
    for idx, lead in needs_query:
        company = lead["company_name"]
        if company not in company_to_indices:
            company_to_indices[company] = []
        company_to_indices[company].append(idx)

    unique_companies = list(company_to_indices.keys())
    log.info("perplexity.starting", unique_companies=len(unique_companies))

    from concurrent.futures import ThreadPoolExecutor, as_completed
    MAX_PERPLEXITY_WORKERS = 10

    companies_seen = {}

    def _query_one(company_name):
        return company_name, _query_perplexity(company_name, client)

    with ThreadPoolExecutor(max_workers=MAX_PERPLEXITY_WORKERS) as executor:
        futures = {executor.submit(_query_one, c): c for c in unique_companies}
        for future in as_completed(futures):
            try:
                company_name, result = future.result()
                companies_seen[company_name] = result
                for idx in company_to_indices[company_name]:
                    leads[idx].update(result)
            except Exception as e:
                company_name = futures[future]
                log.error("perplexity.query_failed", company=company_name, error=str(e))

    cost = len(companies_seen) * PERPLEXITY_COST_PER_QUERY
    log.info("perplexity.complete", unique_companies_queried=len(companies_seen), already_had=already_have, cost_usd=round(cost, 2))

    return leads, cost


# ---------------------------------------------------------------------------
# Website Content Enrichment (company about/services pages)
# ---------------------------------------------------------------------------

WEBSITE_BATCH_SIZE = 10


def _scrape_website(url: str, apify_client: ApifyClient) -> str:
    """Scrape a company website's about/services page for 1-2 paragraphs of context."""
    try:
        # maxCrawlPages + maxResults + maxCrawlDepth all capped explicitly; any
        # of them defaults to 9,999,999 if omitted. The safety wrapper is our
        # belt-and-suspenders — if ANY of those caps gets dropped in a future
        # edit, the post-run item-count check catches the overfetch.
        run_input = {
            "startUrls": [{"url": url}],
            "maxCrawlPages": 3,
            "maxCrawlDepth": 1,
            "crawlerType": "cheerio",
            "maxResults": 3,
        }
        _, items = _run_actor_capped(
            client=apify_client,
            actor_slug=WEBSITE_CRAWLER_ACTOR,
            run_input=run_input,
            max_expected_items=6,  # 2x headroom over the 3-page cap
            timeout_secs=60,
        )
        if not items:
            return ""
        texts = []
        for item in items[:3]:
            text = (item.get("text") or "").strip()
            if text and len(text) > 50:
                texts.append(text[:500])
        return " | ".join(texts)[:1000] if texts else ""
    except Exception as e:
        log.warning("website_scrape.failed", url=url, error=str(e))
        return ""


def enrich_website(leads: list[dict]) -> tuple[list[dict], float]:
    """Enrich leads with company website content. Deduplicates by domain."""
    api_token = os.getenv("APIFY_API_TOKEN") or os.getenv("APIFY_TOKEN") or os.getenv("APIFY_API_KEY")
    if not api_token:
        log.warning("website.no_api_token")
        return leads, 0.0

    client = ApifyClient(api_token)
    domain_to_indices: dict[str, list[int]] = {}
    domains_seen: dict[str, str] = {}

    for idx, lead in enumerate(leads):
        if lead.get("website_context"):
            continue
        website = lead.get("company_website") or ""
        if not website:
            email = lead.get("email", "")
            if "@" in email:
                domain = email.split("@")[1]
                website = f"https://{domain}"
        if not website:
            continue
        if not website.startswith("http"):
            website = f"https://{website}"
        domain = website.split("//")[-1].split("/")[0].lower()
        domain_to_indices.setdefault(domain, []).append(idx)

    unique_domains = [d for d in domain_to_indices if d not in domains_seen]
    log.info("website.start", unique_domains=len(unique_domains), total_leads=len(leads))

    if not unique_domains:
        return leads, 0.0

    def _scrape_one(domain: str) -> tuple[str, str]:
        url = f"https://{domain}"
        return domain, _scrape_website(url, client)

    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = {executor.submit(_scrape_one, d): d for d in unique_domains}
        for future in as_completed(futures):
            try:
                domain, content = future.result()
                domains_seen[domain] = content
                for idx in domain_to_indices[domain]:
                    if content:
                        leads[idx]["website_context"] = content
            except Exception as e:
                domain = futures[future]
                log.error("website.scrape_failed", domain=domain, error=str(e))

    cost = len(domains_seen) * WEBSITE_COST_PER_SITE
    log.info("website.complete", domains_scraped=len(domains_seen), cost_usd=round(cost, 2))
    return leads, cost


# ---------------------------------------------------------------------------
# LinkedIn Posts Enrichment (recent activity for personalization hooks)
# ---------------------------------------------------------------------------

LINKEDIN_POSTS_BATCH_SIZE = 10
MAX_POSTS_PER_PROFILE = 3


def enrich_linkedin_posts(leads: list[dict]) -> tuple[list[dict], float]:
    """
    Enrich leads with recent LinkedIn post content for personalization hooks.
    Uses supreme_coder/linkedin-post (Wpp1BZ6yGWjySadk3) — $1/1k results.
    Batches all profile URLs into a single actor run for efficiency.
    """
    api_token = os.getenv("APIFY_API_TOKEN") or os.getenv("APIFY_TOKEN") or os.getenv("APIFY_API_KEY")
    if not api_token:
        log.warning("linkedin_posts.no_api_token")
        return leads, 0.0

    client = ApifyClient(api_token)

    # Collect profiles that need post scraping
    url_to_indices: dict[str, list[int]] = {}
    for idx, lead in enumerate(leads):
        if lead.get("linkedin_recent_posts"):
            continue
        url = lead.get("linkedin_url", "").strip()
        if url:
            url_to_indices.setdefault(url, []).append(idx)

    if not url_to_indices:
        log.info("linkedin_posts.nothing_to_scrape")
        return leads, 0.0

    all_urls = list(url_to_indices.keys())
    log.info("linkedin_posts.starting", profiles=len(all_urls))

    # Batch URLs into groups to control run size and timeout
    url_batches = [all_urls[i:i + LINKEDIN_POSTS_BATCH_SIZE]
                   for i in range(0, len(all_urls), LINKEDIN_POSTS_BATCH_SIZE)]

    total_results = 0
    for batch_num, url_batch in enumerate(url_batches):
        log.info("linkedin_posts.batch", batch=batch_num + 1, total=len(url_batches), profiles=len(url_batch))
        try:
            # limitPerSource MUST be set. Without it, the actor defaults to
            # "unlimited extraction per profile" — in practice ~40-100 posts
            # per profile. A 16-profile batch returns ~1,000 posts at $1/1k
            # posts, then we use only the top 3 per profile (48 posts) and
            # discard the rest. That's ~96% waste, ~$1/batch, and it's what
            # burned through the Apify top-up during Stages 2-3. Cap at 5
            # (we use 3, plus headroom for scrapeUntil date filtering).
            # _run_actor_capped enforces the same bound post-run in case the
            # param gets dropped by a future edit.
            _, items = _run_actor_capped(
                client=client,
                actor_slug=LINKEDIN_POSTS_ACTOR,
                run_input={"urls": url_batch, "limitPerSource": 5},
                max_expected_items=len(url_batch) * 5,
                timeout_secs=min(30 * len(url_batch), 120),
            )
            total_results += len(items)

            # Group posts by input URL (profile), take top 3 most recent per profile
            posts_by_profile: dict[str, list[dict]] = {}
            for item in items:
                input_url = item.get("inputUrl", "")
                if input_url:
                    posts_by_profile.setdefault(input_url, []).append(item)

            for profile_url, posts in posts_by_profile.items():
                # Sort by timestamp descending, take top 3
                posts.sort(key=lambda p: p.get("postedAtTimestamp", 0), reverse=True)
                top_posts = []
                for post in posts[:MAX_POSTS_PER_PROFILE]:
                    text = (post.get("text") or "").strip()
                    if text and len(text) > 20:
                        top_posts.append(text[:300])

                if top_posts:
                    combined = " ||| ".join(top_posts)
                    for idx in url_to_indices.get(profile_url, []):
                        leads[idx]["linkedin_recent_posts"] = combined

        except Exception as e:
            log.warning("linkedin_posts.batch_failed", batch=batch_num + 1, error=str(e))

    scraped = sum(1 for url in all_urls
                  if any(leads[idx].get("linkedin_recent_posts") for idx in url_to_indices[url]))
    cost = total_results * LINKEDIN_POSTS_COST_PER_PROFILE / 1000  # $1 per 1k results
    log.info("linkedin_posts.complete", profiles_scraped=scraped, total_posts=total_results, cost_usd=round(cost, 4))
    return leads, cost


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def enrich_leads(
    leads: list[dict],
    skip_linkedin: bool = False,
    skip_website: bool = False,
    skip_linkedin_posts: bool = False,
) -> tuple[list[dict], dict]:
    """
    Run all enrichment steps on verified leads.
    Returns (enriched_leads, cost_breakdown).
    """
    costs = {"linkedin_cost": 0.0, "perplexity_cost": 0.0, "website_cost": 0.0, "linkedin_posts_cost": 0.0}

    if not skip_linkedin:
        leads, linkedin_cost = enrich_linkedin(leads)
        costs["linkedin_cost"] = linkedin_cost
    else:
        log.info("linkedin.skipped", msg="--skip-linkedin flag set")

    leads, perplexity_cost = enrich_perplexity(leads)
    costs["perplexity_cost"] = perplexity_cost

    if not skip_website:
        leads, website_cost = enrich_website(leads)
        costs["website_cost"] = website_cost
    else:
        log.info("website.skipped", msg="--skip-website flag set")

    if not skip_linkedin_posts:
        leads, posts_cost = enrich_linkedin_posts(leads)
        costs["linkedin_posts_cost"] = posts_cost
    else:
        log.info("linkedin_posts.skipped", msg="--skip-linkedin-posts flag set")

    # Aggregate success-rate metrics per enrichment source. These drop silently
    # to empty when a scraper errors; operators should see the rate at a glance
    # and abort if enrichment is effectively off (e.g. LinkedIn actor down).
    total = len(leads) or 1
    rates = {
        "linkedin_headline": sum(1 for l in leads if l.get("linkedin_headline")) / total,
        "linkedin_skills": sum(1 for l in leads if l.get("linkedin_skills")) / total,
        "linkedin_posts": sum(1 for l in leads if l.get("linkedin_recent_posts")) / total,
        "website_context": sum(1 for l in leads if l.get("website_context")) / total,
        "perplexity_context": sum(1 for l in leads if l.get("perplexity_company_context")) / total,
    }
    any_signal_rate = sum(
        1 for l in leads
        if any(
            l.get(k)
            for k in ("linkedin_headline", "linkedin_skills", "linkedin_recent_posts", "website_context", "perplexity_company_context")
        )
    ) / total

    log.info(
        "enrich.success_rates",
        **{k: round(v, 3) for k, v in rates.items()},
        any_signal=round(any_signal_rate, 3),
    )

    # Two-tier threshold. Stage 3 at 100 leads revealed that Perplexity alone
    # (51.7%) can squeak past a single-signal gate, yet produce 23/53 leads
    # with identical subject lines because every other source was at 0%. So
    # require BOTH: (1) high overall signal AND (2) at least one Apify-based
    # source alive (linkedin_headline OR website), which catches the classic
    # "monthly usage hard limit exceeded" pattern.
    min_any_signal = float(os.getenv("ENRICH_MIN_ANY_SIGNAL_RATIO", "0.70"))
    min_apify_signal = float(os.getenv("ENRICH_MIN_APIFY_SIGNAL_RATIO", "0.30"))
    apify_signal_rate = max(
        rates.get("linkedin_headline", 0),
        rates.get("linkedin_posts", 0),
        rates.get("website_context", 0),
    )
    if total > 10:
        if any_signal_rate < min_any_signal:
            raise RuntimeError(
                f"Enrichment yielded signal on only {any_signal_rate:.1%} of {total} leads "
                f"(< {min_any_signal:.0%} threshold). Likely a scraper outage — check the "
                f"LinkedIn, website, and Perplexity enrichment actors before continuing."
            )
        if apify_signal_rate < min_apify_signal:
            raise RuntimeError(
                f"All Apify-based enrichment sources are below {min_apify_signal:.0%} "
                f"(best was {apify_signal_rate:.1%}). With only Perplexity working, "
                f"personalization falls back to specialization defaults and produces "
                f"near-identical copy across many leads. Check Apify monthly usage cap "
                f"and actor status before continuing."
            )

    log.info("enrich.complete", total_leads=len(leads), costs=costs)
    return leads, costs
