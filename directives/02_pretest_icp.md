# Step 2: Pretest (ICP Validation)

> **Goal:** Validate that the sourcing query is producing ICP-aligned leads before spending money on full verification + enrichment.
>
> **Script:** `execution/source_leads.py` (embedded ICP pretest loop)

---

## Input

First 100 leads from the source (Step 1 output).

## Output

Pass/fail gate plus failure analysis. If pass rate >= 75%, proceed with the winning params. If below 75%, refine or rotate query profiles and try again.

---

## How It Works

1. Take the first 100 leads from the current source profile
2. Run ICP alignment check against the campaign brief criteria
3. Check title against the allowlist patterns
4. Check exclusion keywords, company size, industry, and free-mail domains
5. Calculate pass rate and capture the dominant failure reason
6. If pass rate is below 75%, tighten the current profile or switch to the next query profile and run a fresh 100-lead pretest

**Gate:** 75% of pretest leads must match ICP before the pipeline is allowed to trust a query family for a full scrape.

**Why this exists:** prevents wasting $20 on a 1000-lead scrape with bad filters vs $2 on a 100-lead pretest.

---

## Title Pattern Groups

```python
TITLE_PATTERNS = {
    "c_level": ["CEO", "CFO", "CTO", "COO", "CMO", "CRO", "Chief * Officer"],
    "founder": ["Founder", "Co-Founder", "Owner", "Partner", "Principal"],
    "president": ["President", "VP", "Vice President", "Managing Director"],
    "director": ["Director", "Head of", "SVP", "EVP"],
    "ops": ["Operations", "RevOps", "Revenue Operations", "Sales Ops", "Marketing Ops"],
    "growth": ["Growth", "Demand Gen", "Demand Generation"]
}
```

---

## Error Handling

| Error | Action |
|-------|--------|
| ICP pretest below 75% | Refine current query family, retry with a new 100-lead sample |
| All query profiles fail the gate | Abort run and notify |

---

## References

- `icp.md` — title allowlist and hard exclusions
- `01_source_leads.md` — source data format
