# Working with Firmable — read this before you spend anything

You are about to query Firmable. Most of what you need is **free**, and the paid
endpoints are the ones an agent reaches for first out of habit. This file exists
because that has already happened here: a session burned ~20 credits on REST
company lookups after correctly printing a table showing search was free.

Run `firmable costs --json` for the machine-readable contract. This file is the
reasoning around it.

## The one rule

**Searching is free. Retrieving a record costs 1 credit.**

That is the whole model. Everything below follows from it.

| Free, unlimited | 1 credit each |
| --- | --- |
| `ai_search`, `filter_search`, REST `people-search` | `get_company`, `get_person` |
| `reveal_person_email`, `bulk_reveal_person_emails` | `bulk_get_companies`, `bulk_get_people` |
| `get_filter_options`, `list_saved_searches` | `reveal_person_phone`, `bulk_reveal_person_phones` |
| lists, signal agents, lookalikes | `push_to_crm`, REST `company` / `people` |
| a lookup that misses; re-fetching a record already bought | |

**Work emails are free. Only mobile numbers actually cost money.**

## Decide in this order

1. **Do you need a record at all, or just to know it exists?** Free search
   returns `has_email`, `has_mobile`, `has_dnd_phone` per person. That is often
   the answer on its own — you can size an account, count contactable people, and
   filter out DNC-listed numbers without spending anything.
2. **Do you need emails?** Free. `firmable emails` — 100 per call, no charge.
   Never use REST `/people` for this; it charges a credit for a record carrying
   the same address.
3. **Do you need a mobile?** This is the only thing worth paying for. Qualify
   first on the free flags, then buy only people where `has_mobile` is true and
   `has_dnd_phone` is false.
4. **Buying a phone? Buy the whole profile instead.** `bulk_get_people` costs the
   same 1 credit as `bulk_reveal_person_phones` but returns the profile and email
   too. There is no reason to buy the narrow one.

## Free recipes

**Company from a domain — free, but indirect.** There is *no* domain, website,
`fqdn` or company-name filter in `filter_search`; the only identifier filters are
`company_id` and `person_id`. And `ai_search` on a bare domain returns nonsense
(`"bamlabs.ai"` returned Commonwealth Bank). What works is searching the company
**name** and verifying the `fqdn` that comes back:

```bash
firmable mcp call ai_search --arg query="BamLabs" --arg category=company --arg country=AU
# -> f000562417551 | BamLabs | https://bamlabs.ai   <- match this fqdn to your domain
```

Name search is fuzzy — `"Heidi Health"` returns the right company first, then two
unrelated Heidis. **Always confirm on `fqdn` before using the id.** Only fall back
to a paid `get_company --arg domain=...` when no result's `fqdn` matches.

**Everyone at a company — free.**

```bash
firmable people-search --company-id f000562417551 --all --csv people.csv
```

Paging is free too. Returns name, position, LinkedIn and the `has_*` flags.

**Their emails — free.**

```bash
firmable emails people.csv --csv contacts.csv
```

**Their mobiles — this spends.** Filter to the qualified subset first, then:

```bash
firmable phones qualified.csv --csv mobiles.csv --limit 200 --yes
```

`--yes` is required. Without it the command refuses and tells you the maximum
spend. `FIRMABLE_ASSUME_YES=1` unblocks it for unattended runs — set that only
when the spend is already bounded by `--limit`.

## Rate limits

Limits are **per tool bucket**, and they are not uniform:

- **Search is not throttled.** 64 concurrent / 31 req/s sustained across ~450
  calls produced zero errors.
- **Bulk enrichment is throttled hard.** `bulk_reveal_person_emails` allows a
  couple of calls then backs you off, returning retry hints of 21s and 60s.
  Practical sustained throughput is roughly **100 records per 20–60 seconds**.

**The trap:** a throttled call returns **HTTP 200** with the error inside the
body:

```json
{"success": false, "code": "rate_limited", "error": "... Retry in 36s.", "status": 429}
```

A client that only checks HTTP status records this as a **success and silently
drops every record in the chunk**. That is exactly what happened here on a first
run: 250 people in, 200 rows out, nothing flagged. `firmable emails` now detects
it and honours the retry hint, so use these commands rather than rolling your own
loop over `firmable mcp call`.

Defaults are 5 req/s over 4 workers — deliberately below what the server allows.
Sustained load from a named account invites a limit being imposed. The bulk tools
take 100 records per call, so a slow request rate still moves plenty.

## Things that will catch you out

- **No balance endpoint.** You cannot check credits programmatically — not on
  REST, not among the 28 MCP tools, not in any response header. Bound runs up
  front with `--limit`; you discover exhaustion by failing.
- **`bulk_reveal_person_phones` is atomic.** A batch larger than the remaining
  balance is rejected whole. No partial charge, but no partial result either.
- **Credit-charging MCP tools have no server-side approval gate.** They run the
  moment you call them. This CLI adds the gate; `firmable mcp call` does not.
- **A REST miss returns HTTP 500**, not 404. Retrying it wastes five calls.
- **The app's "Credit used" figure is wrong** — it under-reports by a constant
  offset. Read "Credits remaining".

## Verification status

Costs marked `measured` in `firmable costs` were spent against a live balance one
action at a time on 2026-08-21, with a positive control. Those marked `declared`
are Firmable's own tool descriptions and have not been independently confirmed.
Do not treat `declared` as proven — if you are about to spend real volume on one,
test a single call against the balance first.
