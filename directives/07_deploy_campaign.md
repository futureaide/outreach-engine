# Step 7: Deploy to Instantly

> Create the campaign in Instantly from the active campaign brief, then upload all QA-passed leads.
>
> **Script:** `execution/deploy_instantly.py`

---

## Input

- QA-passed leads with `custom_variables`
- compiled campaign strategy
- sending accounts from `accounts.csv`

---

## Deployment Rules

- One Instantly campaign = one fixed sequence
- All leads in the campaign share the same template
- Personalization happens only through merge fields
- Campaigns are created in DRAFT status
- Lead upload uses `skip_if_in_campaign: true`
- Campaign `daily_limit` now comes from the active campaign brief's `launch_settings`
- Large launches can over-collect QA-passed leads before deploy so workspace dedupe does not pull the actual in-campaign count under target

---

## Sequence Source of Truth

The active campaign brief owns:
- campaign name
- sequence steps
- subject variants
- body lines
- CTA ladder

`execution/deploy_instantly.py` renders the Instantly HTML from the compiled strategy. There is no second hardcoded sequence.

---

## Current Variable Keys

The current cold-email sequence expects:

```python
[
    "signal_topic",
    "personalization_line",
    "who_i_help",
    "problem_i_fix",
    "bottleneck_trigger",
    "offer_observation",
    "offer_scope",
    "offer_payoff",
    "followup_ping",
    "proof_problem",
    "proof_fix",
    "proof_payoff",
]
```

The deploy script now derives these keys from the active sequence templates so both the agencies brief and the SMB SaaS brief stay in sync.

---

## Campaign Naming

Deploy prefixes the Instantly campaign with a bracketed brief key, for example:

```text
[fa_agencies] Futureaide Performance Agencies - 2026-03-25
```

That prefix is used by the reply webhook to resolve the correct campaign brief later.

---

## Lead Upload Payload Shape

Each lead is uploaded as:

```json
{
  "campaign": "abc123",
  "skip_if_in_campaign": true,
  "email": "john@agency.com",
  "first_name": "John",
  "last_name": "Doe",
  "company_name": "Acme",
  "custom_variables": {
    "signal_topic": "agency handoffs",
    "personalization_line": "with monthly pr delivery new business follow up usually gets whatever time is left",
    "who_i_help": "performance agencies",
    "problem_i_fix": "reply handling and handoff leaks",
    "bottleneck_trigger": "client delivery starts crowding out new business follow up",
    "offer_observation": "there are probably a few avoidable leaks in how replies get handled once delivery gets heavy",
    "offer_scope": "reply routing, follow up ownership, and handoffs",
    "offer_payoff": "the team spends less time chasing replies by hand and pipeline stays steadier",
    "followup_ping": "the drag is usually showing up in follow up ownership once delivery weeks stack up",
    "proof_problem": "client delivery kept eating new business time",
    "proof_fix": "one owner routing flow",
    "proof_payoff": "saved follow up time and kept more warm replies moving"
  }
}
```

---

## Operational Notes

- Instantly v2 still requires one lead per request.
- The deploy script verifies that the campaign is still in DRAFT after creation.
- Native merge fields use camelCase: `{{firstName}}`, `{{lastName}}`, `{{companyName}}`.
- Workspace dedupe can still silently skip leads already present elsewhere. The post-upload verification step records `actually_in_campaign` and the run now treats under-target campaigns as launch failures instead of silent success.
