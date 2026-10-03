# Variable Schema

> Per-lead Instantly `custom_variables` for the active 3-email cold sequence.

---

## Per-Lead Variables

| Variable | Email | Purpose | Max Words | Example |
|----------|-------|---------|-----------|---------|
| `signal_topic` | E1 subject | Short plausible subject | 4 | `agency handoffs` |
| `personalization_line` | E1 | Nick step 1: sourced cold-read personalization | 18 | `with monthly PR delivery, new business follow up usually gets whatever time is left` |
| `who_i_help` | E1 | Relevant reference group | 6 | `performance agencies` |
| `problem_i_fix` | E1 | Bottleneck class | 7 | `reply handling and handoff leaks` |
| `bottleneck_trigger` | E1 | When the bottleneck appears | 13 | `client delivery starts crowding out new business follow up` |
| `offer_observation` | E1 | Plausible observation that sets up the offer | 16 | `there are probably a few avoidable leaks in how replies get handled once delivery gets heavy` |
| `offer_scope` | E1 | Systems area to map | 8 | `reply routing, follow up ownership, and handoffs` |
| `offer_payoff` | E1 | Clear payoff for the mapped leaks | 14 | `the team spends less time chasing replies by hand and pipeline stays steadier` |
| `followup_ping` | E2 | Simple human follow-up restatement | 15 | `the drag is usually showing up in follow up ownership once delivery weeks stack up` |
| `proof_problem` | E3 | Proof story: before state | 14 | `client delivery kept eating new business time` |
| `proof_fix` | E3 | Proof story: system or mechanism | 7 | `one owner routing flow` |
| `proof_payoff` | E3 | Proof story: payoff | 12 | `saved follow up time and kept more warm replies moving` |

---

## Native Lead Fields

These stay as top-level Instantly lead fields and resolve via native merge tags:

| Field | Purpose |
|------|---------|
| `first_name` | Greeting |
| `last_name` | Lead record |
| `company_name` | Subject variant / lead record |

---

## Runtime Metadata

These are not part of Instantly `custom_variables`, but they shape generation and QA:

| Field | Purpose |
|------|---------|
| `constraint_id` | Chosen bottleneck theme |
| `copy_selector` | Trust/reference-group selector |
| `role_bucket` | Founder / growth / ops classification |
| `lane` | Agency / SaaS / generic classification |
| `lead_dossier` | Structured LinkedIn + company context used by the prompt layer |
