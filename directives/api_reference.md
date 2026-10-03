# API Reference

> All API endpoints, auth, costs, and rate limits. Referenced by individual action directives.

---

## Apify (Lead Sourcing + LinkedIn)

| | Leads Finder | LinkedIn Scraper |
|---|---|---|
| Actor | `code_crafter~leads-finder` | `dev_fusion/linkedin-profile-scraper` |
| Cost | $0.02/lead (confirmed) | $0.01/profile (confirmed) |
| Auth | `Authorization: Bearer {APIFY_API_TOKEN}` | Same |
| Min batch | 100 leads | 1 profile |
| Rate limits | Actor-managed | Actor-managed |

---

## Reoon (Email Verification)

- **Endpoint:** `https://emailverifier.reoon.com/api/v1/verify`
- **Auth:** API key as query parameter
- **Cost:** $0.0012/email (credit-based: $12/10K credits)
- **Mode:** Power (deep verification)
- **Timeout:** 90 seconds
- **Status mapping:** `"safe"` = verified; everything else = dropped
- **Critical errors:** 402/401/403 = abort immediately

---

## Instantly v2 (Campaigns)

- **Base URL:** `https://api.instantly.ai/api/v2`
- **Auth:** `Authorization: Bearer {INSTANTLY_API_KEY}`
- **Rate limits:** 100 req/10s, 600 req/min
- **Campaign creation:** `POST /campaigns`
- **Lead upload:** `POST /leads` (single lead per request, parallel workers)
- **Campaign status:** 0=draft, 1=active, 2=paused, 3=completed

---

## Anthropic (Claude)

- **Model:** `claude-sonnet-4-20250514`
- **Cost:** $3/MTok input, $15/MTok output
- **Estimated per lead:** ~$0.004 (batches of 5, ~2500 input + ~750 output tokens per batch)
- **Max parallel requests:** 15-20
- **Retry:** exponential backoff on 429

---

## Perplexity

- **Model:** `sonar`
- **Cost:** ~$0.0004/query ($1/MTok in+out, ~300 tokens avg per query)
- **Auth:** `Authorization: Bearer {PERPLEXITY_API_KEY}`
- **Purpose:** Company context for personalization_line, identity/offer variables, proof matching, and inside-language selection

---

## Google Sheets

- **Auth:** Service account (`GOOGLE_SHEETS_CREDENTIALS_PATH`) or OAuth (`GOOGLE_OAUTH_CREDENTIALS_PATH` + `GOOGLE_TOKEN_PATH`)
- **State spreadsheet:** `Outbound_Engine_State`
- **Tabs:** Campaign_Index, Cost_Tracking, Suppression

---

## Telegram

- **Endpoint:** `https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage`
- **Chat ID:** from `TELEGRAM_CHAT_ID` env var
- **Purpose:** Run completion notifications
