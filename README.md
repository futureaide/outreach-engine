# Outreach Engine

An outbound email pipeline I built in Python for Futureaide, my automation practice. It has been in production since February 2026, and the first live campaign went out to 554 leads.

This is a public copy of a private repository. Lead data, campaign briefs and email copy are not included, and the tests that used real prospect data were removed. The code expects campaign briefs in `campaigns/`, so it will not run end to end as published.

## What it does

One CLI command takes a search query and a target count, and ends with a launched campaign:

1. Source leads (Apify, Apollo or a CSV) and pretest them against the ideal customer profile
2. Verify email addresses (Reoon)
3. Enrich each lead with LinkedIn data and company context (Apify scrapers, Perplexity)
4. Write the personalization with Claude
5. Run QA on every lead's copy, plus an LLM coherence score
6. Send failures back to Claude with the specific reasons, up to 3 retries
7. Build a launch preflight report
8. Deploy to Instantly
9. Log the run to Google Sheets and send a Telegram alert

## How it is built

The system has three layers. `directives/` holds Markdown SOPs that get updated as the system learns. `run_campaign.py` is the orchestrator: it reads the directives and calls the scripts in `execution/`, which handle the API calls and data processing.

Claude is used in two places: writing the personalization and scoring how well each email sequence flows. Sourcing, verification, the QA rules, deployment and logging are plain code. Each campaign is defined by a JSON brief that sets the strategy, the email sequence and the QA thresholds. The copy structure follows Nick Saraev's cold-email framework; the file that summarizes it is not included.

## Reliability

- The first 100 leads are pretested before the full scrape runs. Below a 75% pass rate the query is refined, up to 5 attempts, and then the run stops with an alert.
- 22 deterministic QA checks run on every lead's generated copy: required fields, banned phrases, word and punctuation limits, and no invented numbers. Leads that recover after regeneration rejoin the campaign.
- Every step writes its output to a run cache, so a crashed run resumes from the step that failed (`--resume`). A dry-run mode runs the whole pipeline without deploying.
- Errors map to fixed actions. Auth and billing errors stop the run and alert. Rate limits back off and retry. Malformed JSON from the model is retried once and then replaced with safe fallback values. A partial upload is logged as a warning.
- Silent failures stop the run. If more than 10% of Claude batches fall back to default values, the run aborts before near-identical emails go out. If more than 25% of leads skip the coherence check, which usually means the Anthropic API is down, it aborts too.
- Each run has a cost cap.

## What changed after real runs

- Version 1 scraped company websites itself and succeeded only 50 to 75% of the time because of SSL and DNS failures. Version 2 dropped that scraper and gets context from LinkedIn data, managed Apify scrapers and Perplexity.
- On 14 April 2026 a lead-sourcing run passed its limit under the wrong parameter name. The actor fell back to its default of 100,000 and pulled 11,691 leads ($23) in 36 minutes. Two more actors turned out to have the same kind of unbounded default, and every actor names its limit differently. All Apify calls now go through one wrapper (`_run_actor_capped` in `execution/enrich_leads.py`): the caller must declare the maximum number of items it expects, and the run fails if more come back. A hard fetch cap also refuses any run that asks for more than 2,000 leads.
- In testing, a run where only Perplexity returned data passed the old 50% enrichment gate at 51.7% coverage, yet 23 of 53 leads got the same signal topic. The gate is now 70%. A second check catches Apify's monthly usage cap, where every Apify scraper returns nothing while Perplexity keeps working.
- Instantly's workspace dedupe can silently drop leads at upload and leave a live campaign below target. The target count now means leads actually in the campaign, and large runs collect a buffer of QA-passed leads before deploying.

## Numbers from production

A typical 500-lead campaign: about 850 leads sourced, 580 verified, 550 personalized and 500+ deployed. The all-in cost is about $22, roughly $0.04 per deployed lead, and Apify sourcing is about 77% of that.

## Usage

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # then fill in the keys

# Full pipeline without deploying
python run_campaign.py --query "paid media agency founder" --count 200 --dry-run

# Live campaign
python run_campaign.py --query "performance marketing agency CEO United States" \
  --count 500 --campaign-name "PMA US" --campaign-brief campaigns/<brief>.json

# Resume a crashed run from its last completed step
python run_campaign.py --resume <run_id> --campaign-name "Resumed Campaign"
```

## Layout

```
run_campaign.py      CLI orchestrator
execution/           sourcing, verification, enrichment, personalization, QA, preflight, deploy, notify
directives/          step-by-step SOPs, API notes, error handling, variable schema
.env.example         every setting, with the reason behind each threshold
```
