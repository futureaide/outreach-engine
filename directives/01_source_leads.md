# Step 1: Source Leads

> **Goal:** Fetch a list of leads matching the ICP from Apify, Apollo, or CSV import.
>
> **Script:** `execution/source_leads.py`

---

## Input

- Search query (titles, location, industry)
- Max lead count
- Source type: Apify (primary), Apollo (secondary), or CSV (tertiary)

## Output

A list of leads with standardized fields:
- `first_name`, `last_name`, `full_name`
- `email`, `email_status` ("validated" | "not_validated" | "unknown")
- `job_title` (maps to `title`)
- `company_name`, `company_website`
- `linkedin_url`
- `company_size`, `company_industry`

---

## Primary: Apify Leads Finder

- **Actor:** `code_crafter~leads-finder`
- **Cost:** $0.02/lead (confirmed from Apify dashboard)
- **Minimum batch:** 100 leads per run (actor constraint)

**Input parameters:**
- `contact_job_title`: titles from the allowlist (see `icp.md`)
- `contact_location`: "United States" or "Canada"
- `company_industry`: "Marketing and Advertising", "Internet", etc.
- `email_status`: `["validated"]` for pre-verified leads (reduces Reoon costs)
- `fetch_count`: number of leads to fetch. **CRITICAL — this must be named exactly `fetch_count`.** If omitted or misnamed, the actor defaults to 100,000 leads and will run up a catastrophic bill. We learned this the hard way on 2026-04-14 (36-minute, 11,691-lead, $23 runaway caused by sending `max_results` instead). `source_leads.py` also enforces a hard ceiling via `APIFY_HARD_FETCH_CAP` (default 2000) before starting any run.

**Query-to-filter mapping:** Convert natural language queries like "performance marketing agency CEO United States" into structured Apify filters:
- Extract titles → `contact_job_title`
- Extract location → `contact_location`
- Extract industry → `company_industry`
- Extract seniority → `seniority_level` (Founder, Owner, C-Level, Director, VP)

**Embedded ICP gate:**
- Always fetch a 100-lead pretest sample first.
- Run ICP scoring on the pretest sample before trusting the query family.
- If pass rate is at least 75%, keep the winning params and fetch the remaining leads with those same params.
- If pass rate is below 75%, refine the current profile and retry.
- If the current profile is clearly off-ICP, switch to the next structured query profile from the campaign brief and run a fresh 100-lead pretest again.
- Only fully scrape once a query family earns a passing pretest.

---

## Secondary: Apollo Free Search

Apollo's free search endpoint (no credits consumed) can supplement lead lists. Output needs field mapping to match the standard schema.

---

## Tertiary: CSV Import

Import leads from CSV with columns: email, first_name, last_name, company_name, title, linkedin_url, company_website. If CSV has an `email_verified` column with value `true`, those leads skip Reoon.

---

## Suppression List

Before processing, check all leads against the suppression list (bounces, unsubscribes, complaints stored in Google Sheets). Remove any matches.

---

## Error Handling

| Error | Action |
|-------|--------|
| API 401/402/403 | Abort run immediately, notify via Telegram |
| API 429 | Exponential backoff, retry up to 3x |
| API 5xx | Retry up to 3x with backoff |

---

## Operational Learnings

- **Input enums:** `seniority_level` must be lowercase with underscores (e.g. `c_suite`, `director`, `vp`). Capitalized values are rejected.
- **Array fields:** `contact_location` and `company_industry` must be arrays, not strings.
- **company_size normalization:** `company_size` is returned as an integer — normalize with `str()` before calling `.strip()`.
- **Over-fetching prevention:** Use `start()` + poll + `abort()` pattern instead of `call()` to cap at `fetch_count`. Abort failures must be surfaced loudly — do NOT swallow them — or the actor will keep scraping server-side after our code thinks it stopped.
- **Empty company_name:** Some Apify results arrive with empty `company_name`. `_normalize_lead` in `source_leads.py` derives a fallback from the email domain (e.g. `wcdenterprises.com` → `Wcdenterprises`). Free-mail domains (gmail, yahoo, etc.) are excluded.

---

## References

- `icp.md` — title allowlist and exclusions for query building
- `api_reference.md` — Apify API details
