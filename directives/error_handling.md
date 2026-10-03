# Error Handling

> Central error-to-action lookup table. Individual action directives include their own specific error handling; this is the cross-cutting reference.

---

## Error Action Table

| Error | Action |
|-------|--------|
| API 401/402/403 | Abort run immediately, notify via Telegram |
| API 429 | Exponential backoff, retry up to 3x |
| API 5xx | Retry up to 3x with backoff |
| Missing LinkedIn URL | Skip LinkedIn enrichment, use Perplexity-only |
| Missing LinkedIn data | Use Perplexity-only for personalization context |
| ICP pretest below 75% | Abort run, notify "bad query" |
| QA failure | Exclude lead from campaign, log reason |
| Zero verified leads | Abort run, notify via Telegram |
| JSON parse failure from Claude | Retry once, then use fallback variables |
| Instantly upload <90% success | Log warning, continue with uploaded leads |
