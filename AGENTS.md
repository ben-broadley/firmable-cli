# Working with Firmable: read this before you spend anything

You are about to query Firmable. Most of what you need is **free**, and the paid
endpoints are the ones an agent reaches for first, because they are the obvious
ones: they have clear names, they take the identifier you already hold, and they
return a whole record. The free routes are less discoverable and need a step of
indirection. Reading the cost table is not enough on its own. The habit wins
unless you check before each call.

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
   the answer on its own. You can size an account, count contactable people, and
   filter out DNC-listed numbers without spending anything.
2. **Do you need emails?** Free. `firmable emails` does 100 per call, no charge.
   Never use REST `/people` for this; it charges a credit for a record carrying
   the same address.
3. **Do you need a mobile?** This is the only thing worth paying for. Qualify
   first on the free flags, then buy only people where `has_mobile` is true and
   `has_dnd_phone` is false.
4. **Buying a phone? Buy the whole profile instead.** `bulk_get_people` costs the
   same 1 credit as `bulk_reveal_person_phones` but returns the profile and email
   too. There is no reason to buy the narrow one.

## Free recipes

**Company from a domain: free, but indirect.** There is *no* domain, website,
`fqdn` or company-name filter in `filter_search`; the only identifier filters are
`company_id` and `person_id`. And `ai_search` does not understand domains. It
matches them as text and returns whatever is prominent:

```bash
firmable mcp call ai_search --arg query="smec.com" --arg category=company
# -> Commonwealth Bank | Queensland Health | NAB        <- useless

firmable mcp call ai_search --arg query="SMEC" --arg category=company
# -> f000000147616 | SMEC | http://www.smec.com         <- match this fqdn
#    SMEC Power & Technology | smecpt.com.au
#    SMEC Testing Services   | smectesting.com.au
```

Search the **name**, then confirm the returned `fqdn` matches the domain you were
given. Note the second and third results above: near-name subsidiaries with
different domains. Taking the first hit without checking `fqdn` silently attaches
your contacts to the wrong entity. Only fall back to a paid
`get_company --arg domain=...` when nothing matches.

**The name you were given is often not the name Firmable indexes.** This is the
single biggest recall problem on Australian small business, and it is invisible:
the search succeeds, returns nothing, and reads like the company is absent from
the dataset.

Registers, licence lists and government datasets carry a *registered or trading*
name. Firmable indexes what the business actually calls itself, which is usually
what is in its domain. Measured on 552 Australian aged care providers taken from
the government's own service list:

| Query | Resolved |
| --- | --- |
| The register's trading name | 63 / 552 |
| Adding a second pass on the domain's brand words | **153 / 552** |

```bash
firmable mcp call ai_search --arg query="Gorrinn Village" --arg category=company
# -> nothing

# its domain is araratretirementvillage.com.au
firmable mcp call ai_search --arg query="Ararat Retirement Village" --arg category=company
# -> Ararat Retirement Village                          <- resolves first time
```

Same shape: Deloraine Private Nursing Home is `goldage.com.au` and indexes as
GoldAge; Christophorus House is `chrv.com.au` and indexes as CHRV; Clendon Care
Pty Ltd indexes as CLENDON RESIDENCES. **Always run both queries.** Both are
free, so there is no reason to run only one, and the `fqdn` check still guards
against a wrong match either way.

Split the domain on its brand words rather than passing it whole:
`araratretirementvillage.com.au` becomes `ararat retirement village`. Splitting on
the common sector suffixes (`agedcare`, `homecare`, `healthcare`, `retirement`,
`village`, `community`, `services`, `group`, `care`, `health`) is enough.

**Everyone at a company, free.**

```bash
firmable people-search --company-id f000000147616 --all --csv people.csv
```

Paging is free too. Returns name, position, LinkedIn and the `has_*` flags.

**Their emails, free.**

```bash
firmable emails people.csv --csv contacts.csv
```

**Size the spend before you commit to it, free.** The `has_*` flags turn a
1-credit-per-person question into a free one. Over 4,472 people at 153 Australian
aged care providers:

| | Count | |
| --- | ---: | --- |
| People found | 4,472 | free |
| `has_email` | 2,273 | free to reveal too |
| `has_mobile`, `has_dnd_phone` false | 935 | **the only rows worth buying** |
| `has_mobile`, on the DNC Register | 380 | free to exclude |

Buying that roster blind is 4,472 credits. Buying the qualified subset is 935, and
capped at three per account it is 159. **Count first, then decide the cap, then
spend.** Nearly a third of the mobiles in that pool were DNC-registered, and the
free flag removed them without an ACMA wash.

**Their mobiles. This is the part that spends.** Filter to the qualified subset first, then:

```bash
firmable phones qualified.csv --csv mobiles.csv --limit 200 --yes
```

`--yes` is required. Without it the command refuses and tells you the maximum
spend. `FIRMABLE_ASSUME_YES=1` unblocks it for unattended runs. Set that only
when the spend is already bounded by `--limit`.

## Rate limits

Two separate things constrain you: what the server enforces, and what the terms
of use permit. They are not the same, and the terms are the tighter of the two.

**What the server enforces** is per tool bucket and not uniform:

- **Search returns no throttling errors**, even at high concurrency. That is an
  absence of enforcement, not permission. See the terms below.
- **Bulk enrichment is throttled hard.** `bulk_reveal_person_emails` allows a
  couple of calls then backs you off, returning retry hints of 21s and 60s.
  Practical sustained throughput is roughly **100 records per 20 to 60 seconds**.

**What the terms permit** is narrower. Clause 3.2(c)(v) defines unreasonable use
to include activity "which constitutes abnormal or excessive use of the Service
compared to the average use by other users on similar plans", and 3.2(c) also
covers anything "which attempts to bypass or manipulate limitations or usage
restrictions". An endpoint answering quickly at high concurrency is not an
invitation to run there. Stay near the defaults.

**The trap:** a throttled call returns **HTTP 200** with the error inside the
body:

```json
{"success": false, "code": "rate_limited", "error": "... Retry in 36s.", "status": 429}
```

A client that only checks HTTP status records this as a **success and silently
drops every record in the chunk**. 250 ids in, 200 rows out, nothing raised.
`firmable emails` and `firmable phones` inspect the body and honour the retry
hint, which is the reason to use them rather than rolling your own loop over
`firmable mcp call`.

Defaults are 5 req/s over 4 workers, well below what the server allows.
Sustained load from a named account invites a limit being imposed. The bulk tools
take 100 records per call, so a slow request rate still moves plenty.

## The terms of use bind tighter than the rate limiter

Read these before designing a high-volume run. Quoting the clauses because a
paraphrase loses the teeth.

- **There is a profile-view ceiling on top of credits.** Clause 3.2(a): "credit
  limits are tied to your Subscription tier and profile views are limited to
  approximately **10 times the number of credits** allocated to your account on a
  monthly basis." Free search is free of *credits*, not unlimited. On an 8,000
  credit plan that is roughly 80,000 profile views a month. "Profile view" is not
  defined anywhere in the terms, and whether search results count is unclear, so
  treat volume search as drawing on a budget you cannot measure.
- **Permitted Use is your own business only.** Clause 20.11 defines it as
  "business to business marketing for your own ordinary business use (**and not
  for the benefit of any third party**)". If you are running this on someone
  else's behalf, check that against the account holder's subscription first.
- **Bulk extraction is prohibited.** Clause 3.3(e) forbids "export, extract, or
  otherwise scrape any of the content or data on the Service ... including
  resharing, rehosting any of our content outside the Service **or for the
  purposes of training a large language model**". Using the documented API to
  enrich records into the account holder's own CRM is the intended path.
  Bulk-dumping the database is not, and feeding it to a model is called out by
  name.
- **One seat, one human.** Clause 6.1(b): sharing login credentials "is strictly
  prohibited". The MCP OAuth token is tied to an individual.
- **They can suspend you for load.** Clause 10.3(c) covers use that "consumes
  excessive bandwidth or storage, or is causing harm to our platform", and 3.4
  reserves the right to block whole territories during "high volumes of bot
  scraping or data theft".

This is a summary, not legal advice. If a run matters commercially, read the
terms yourself.

## Things that will catch you out

- **No balance endpoint.** You cannot check credits programmatically. Not on
  REST, not among the 28 MCP tools, not in any response header. Bound runs up
  front with `--limit`; you discover exhaustion by failing.
- **`bulk_reveal_person_phones` is atomic.** A batch larger than the remaining
  balance is rejected whole. No partial charge, but no partial result either.
- **Credit-charging MCP tools have no server-side approval gate.** They run the
  moment you call them. This CLI adds the gate; `firmable mcp call` does not.
- **A REST miss returns HTTP 500**, not 404. Retrying it wastes five calls.
- **The app's "Credit used" figure is wrong.** It under-reports by a constant
  offset. Read "Credits remaining".

## Verification status

Costs marked `measured` in `firmable costs` were spent against a live balance one
action at a time on 2026-08-21, with a positive control. Those marked `declared`
are Firmable's own tool descriptions and have not been independently confirmed.
Do not treat `declared` as proven. If you are about to spend real volume on one,
test a single call against the balance first.
