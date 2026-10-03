# Step 4: Enrich Leads

> **Goal:** Add LinkedIn profile data and company context to each lead for personalization.
>
> **Script:** `execution/enrich_leads.py`

---

## Input

Verified leads from Step 3.

## Output

Enriched leads with LinkedIn profile data and Perplexity company context attached.

---

## LinkedIn Profile Scraping (Primary)

- **Actor:** `dev_fusion/linkedin-profile-scraper`
- **Cost:** $0.01/profile (confirmed from Apify dashboard)
- **Input:** LinkedIn profile URL from lead data
- **Key output fields:**
  - `linkedin_headline` — THE key personalization signal (how they describe themselves)
  - `linkedin_job_description` — what they spend time on
  - `linkedin_skills` — expertise areas (use as angle selector, never cite directly)
  - `linkedin_company_size` — employee range
  - `linkedin_company_industry` — industry classification
  - `linkedin_tenure_years` — time in current role

**If LinkedIn URL is missing:** skip LinkedIn enrichment for that lead, use Perplexity-only.

---

## Perplexity (Company Context)

- **Model:** `sonar` (lightweight, web-grounded)
- **Cost:** ~$0.0004/query (Sonar: $1/MTok in+out, ~300 tokens avg)
- **Purpose:** company overview, service lines, recent signals
- **Used for:** generating `personalization_line`, `who_i_help`, `problem_i_fix`, `bottleneck_trigger`, `offer_observation`, and `offer_payoff`, plus improving proof matching and inside-language selection

**Queries to run per lead:**
1. Company query: "What does {company_name} do? What services do they offer? Who are their clients?"
2. Signal query (optional): "What recent news, hiring, or funding has {company_name} had?"

---

## No Website Scraping

Website scraping was unreliable in v1 (50-75% success rate, SSL issues, DNS failures). Do not implement it.

---

## Error Handling

| Error | Action |
|-------|--------|
| Missing LinkedIn URL | Skip LinkedIn enrichment, use Perplexity-only |
| Missing LinkedIn data | Use Perplexity-only for personalization context |
| API 401/402/403 | Abort run immediately, notify via Telegram |
| API 429 | Exponential backoff, retry up to 3x |

---

## References

- `api_reference.md` — Apify LinkedIn Scraper + Perplexity API details
- `05_personalize.md` — how enrichment data feeds into personalization
