# Step 5: Personalize

> Generate a small set of per-lead variables that fit Nick Saraev's cold-email framework.
>
> **Script:** `execution/personalize.py`

---

## Goal

Turn enriched lead context into Nick's cold-email building blocks:
1. a short sourced personalization
2. a merged "who i am / why i matter" paragraph split into variables
3. a specific give-first offer with a clear payoff
4. a light follow-up ping
5. a matched proof story with problem -> fix -> payoff

The system does **not** generate full emails. It generates variables that slot into the fixed 3-email sequence.

---

## Inputs

- verified + enriched leads
- campaign brief and compiled strategy
- constraint library
- matched proof library

---

## Runtime Flow

1. Exclude obvious non-ICP leads like enterprise board/chair profiles.
2. Extract primitives from title, LinkedIn headline, and company context.
3. Assign one likely bottleneck theme per lead.
4. Build a lead dossier from LinkedIn + company context and ask Claude for the high-variance variables only.
5. Validate the bundle shape.
6. Fall back to deterministic defaults if the generated bundle is weak.

---

## Constraint Themes

The current starter library is:

| Constraint | Use When | Core Problem |
|-----------|----------|--------------|
| `agency_delivery_drag` | agency founder/growth leads | client delivery crowds out new business follow-up |
| `handoff_leakage_ops` | ops leads | routing and ownership break as the team scales |
| `saas_followup_drag` | SaaS founder/growth leads | speed to lead leaks when follow-up is still manual |

The constraint is there to keep the offer grounded in a believable bottleneck. It is not exposed directly in the email.

---

## Nick Framework in Generation

Claude is prompted to generate only the high-variance variables:

- `signal_topic`
  Short lowercase subject tied to a live pain point.
- `personalization_line`
  One sourced sentence that feels personal and has a point.
- `who_i_help`
  Relevant reference group.
- `problem_i_fix`
  Specific bottleneck class.
- `bottleneck_trigger`
  When the bottleneck usually appears.
- `offer_observation`
  Plausible observation that sets up the give-first offer.
- `offer_payoff`
  Clear payoff in ROI, time saved, fewer leaks, or steadier pipeline terms.

These stay deterministic:

- `offer_scope`
  The systems area to map.
- `followup_ping`
  Human Email 2 restatement of the live issue.
- `proof_problem`
- `proof_fix`
- `proof_payoff`

The generation prompt passes a lead dossier built from title, LinkedIn headline, LinkedIn job description, Perplexity company context, company size, copy selector, matched anchor terms, and inside-language candidates so the personalization can feel native to the prospect's lane instead of sounding like outside commentary.

---

## Guardrails

- Use `I`, never `we`
- No LinkedIn mentions in copy
- No fake metrics
- No tool-name flexing
- Offer must stay reply-first in cold email
- Proof must match the lead's lane as closely as possible
- Use in-group language only when it is grounded in real context
- A tiny casual imperfection is allowed when it feels natural, but it must stay subtle and never be forced

---

## Fallbacks

Every constraint has deterministic fallback variables. If the model output is missing the framework shape or fails validation, the fallback bundle is used so the pipeline keeps moving.

---

## Output

Each lead leaves this step with a `custom_variables` dict matching `variable_schema.md`.
