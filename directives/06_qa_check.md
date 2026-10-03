# Step 6: QA

> Validate every generated variable bundle before deployment.
>
> **Script:** `execution/qa_check.py`

---

## Goal

Catch copy that is:
- generic
- corporate
- structurally off
- off-wedge
- too long
- not actually following Nick's framework

---

## Required Variables

Every lead must have:

- `signal_topic`
- `personalization_line`
- `who_i_help`
- `problem_i_fix`
- `bottleneck_trigger`
- `offer_observation`
- `offer_scope`
- `offer_payoff`
- `followup_ping`
- `proof_problem`
- `proof_fix`
- `proof_payoff`

Missing any of these is a hard fail.

---

## Core Checks

- Banned AI words, phrases, and formal transitions
- Contraction enforcement
- No collective voice: `we`, `our`, `us`
- No wedge drift
- Subject line stays short and lowercase
- Email 1 stays under the configured word ceiling
- No hyphens, em dashes, semicolons, or colons
- All generated lines start lowercase
- `who_i_help`, `problem_i_fix`, and `bottleneck_trigger` must form a believable operator identity block
- `offer_observation`, `offer_scope`, and `offer_payoff` must form a believable give-first offer block
- `followup_ping` must stay reply-first and avoid meeting asks
- `proof_problem`, `proof_fix`, and `proof_payoff` must form a matched proof story
- proof cannot contain fake performance numbers

---

## Nick-Specific Structural Checks

- Email 1 must read as: personalization -> who i help / problem i fix -> offer -> CTA
- The CTA is give-first and low-friction
- The sequence is optimized for interested replies, not booked meetings
- Follow-ups should feel like human pings, not new pitch stacks
- Personalization must be source-grounded and imply why the observation matters
- The offer must clearly communicate the payoff of mapping the leaks

The coherence pass uses a lightweight Claude check on Email 1 and Email 3 flow.

---

## What Happens on Fail

1. QA failure reasons are logged onto the lead.
2. `execution/personalize.py` gets those exact failures back in the regeneration prompt.
3. If the lead still fails after regeneration attempts, it is excluded from deployment.
