# Step 3: Verify Emails

> **Goal:** Ensure every lead has a verified email address before enrichment. Pre-verified leads skip this step.
>
> **Script:** `execution/verify_emails.py`

---

## Input

Leads from Step 1/2 with `email_status` field.

## Output

Verified leads pool: all leads with either Apify-validated emails or Reoon-verified "safe" emails.

---

## How It Works

Leads from Apify come with an `email_status` field. Use this to split:

- **`email_status: "validated"`** → skip Reoon, go straight to enrichment
- **`email_status: "not_validated"` or `"unknown"`** → send to Reoon for verification

### Reoon Verification

- **Endpoint:** `https://emailverifier.reoon.com/api/v1/verify`
- **Mode:** Power (deep verification)
- **Cost:** $0.0012/email (credit-based: $12/10K credits)
- **Timeout:** 90 seconds per email (Power mode can be slow)
- **Parallel processing:** 5 workers max
- **Status mapping:** only `"safe"` = verified. Everything else (`"risky"`, `"invalid"`, `"unknown"`, `"disposable"`) = dropped.

---

## Error Handling

| Error | Action |
|-------|--------|
| 402 (Payment Required) | Abort immediately, never retry |
| 401 (Unauthorized) | Abort immediately |
| 403 (Forbidden) | Abort immediately |
| 429 (Rate Limit) | Exponential backoff, retry up to 3x |
| Zero verified leads | Abort run, notify via Telegram |

---

## Operational Learnings

- **Rate limiting:** Reoon aggressively 429s when parallel workers exceed ~5. The script uses 5 workers with exponential backoff (2s, 4s, 8s).
- **UnboundLocalError fix:** If `resp` is never assigned (connection error), accessing `resp.status_code` throws `UnboundLocalError`. Fixed by initializing `resp = None` and guarding the check.

---

## References

- `api_reference.md` — Reoon API details
- `error_handling.md` — full error action table
