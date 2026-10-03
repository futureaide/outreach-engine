# Outreach Engine v2 — Overview

> Start here for the current runtime shape.

---

## Mission

Source the right leads, verify them, enrich them, generate Nick-style cold-email variables, QA them, deploy a fixed Instantly campaign, and handle replies with a booking-oriented reply agent.

The cold-email side optimizes for **interested replies**.
The reply agent optimizes for **booked meetings**.

---

## Pipeline

```text
Source Leads -> ICP Pretest -> Verify Emails -> Enrich -> Personalize -> QA -> Deploy -> Notify
```

---

## Key Runtime Concepts

- One campaign = one fixed Instantly sequence
- Personalization lives in per-lead variables
- Campaign brief is the tactical source of truth
- Nick Saraev doctrine guides copy structure and QA
- Reply webhook resolves the brief from the deployed campaign name

---

## Main Runtime Assets

| Asset | Purpose |
|------|---------|
| `campaigns/futureaide_default.json` | default narrow brief for performance agencies |
| `campaigns/futureaide_smb_saas.json` | narrow brief for SMB SaaS |
| `campaigns/nick_saraev_doctrine.json` | doctrine summary used in prompts |
| `execution/campaign_strategy.py` | loads briefs, doctrine, sequence steps, and reply routing |

---

## Active Sequence Shape

- 3 emails, not 4
- Email 1 uses Nick's `personalization -> identity -> offer -> CTA` framework
- Email 2 is a simple human ping
- Email 3 uses matched proof and a final low-friction ask
