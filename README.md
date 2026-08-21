# firmable-cli

A command-line interface to [Firmable](https://firmable.com), the Australian and New Zealand
B2B database: around 1.5 million companies and 10 million people, carrying ABNs, ACNs and
NZBNs, ANZSIC codes, technographics, work emails, mobile numbers and Do Not Call status.

Unofficial. Not affiliated with, endorsed by, or sponsored by Firmable.

```bash
firmable costs                                    # what spends, and what does not
firmable search --query "physio clinics in Melbourne" --csv companies.csv   # free
firmable people-search --company-id f000000147616 --all --csv people.csv    # free
firmable emails people.csv --csv contacts.csv                               # free
firmable phones people.csv --csv mobiles.csv --limit 200 --yes              # 1 credit each
```

No dependencies, Python 3.9 or newer.

- **Free discovery and free work emails.** Searching costs nothing on either surface, and
  `bulk_reveal_person_emails` returns unmasked work addresses 100 at a time for nothing.
  Mobile numbers are the only thing you really pay for.
- **A cost model you can query.** `firmable costs --json` publishes what each operation
  costs, which numbers were measured rather than quoted, and the free alternative to each
  paid call. Commands that spend refuse to run without `--yes`.
- **Every documented REST operation**, with flags and help text generated from Firmable's
  own `openapi.json`, plus resumable batch enrichment from a CSV column.
- **MCP from the shell.** Firmable's MCP server normally only answers to Claude, ChatGPT or
  Cursor. `firmable mcp` speaks the protocol directly, so all 28 tools work in a terminal,
  a cron job, or anything that can shell out.

> **Agents: read [AGENTS.md](AGENTS.md) first, or run `firmable costs --json`.**
> Searching is free; only record retrieval spends. The paid endpoints are the
> discoverable ones. They take the identifier you already hold and return a whole
> record, while the free routes need a step of indirection. It is easy to spend
> credits answering a question search would have answered for nothing.

## Which half should I use?

The CLI covers two different Firmable surfaces, and they are not equivalent. If you have MCP
access, it is the better one for most work.

| If you want to | Use | Costs |
| --- | --- | --- |
| **find** companies or people by criteria | `firmable mcp call filter_search` | **free** |
| see who works somewhere, and who has a mobile | `people-search` or `roster` | **free** |
| **work emails at volume** | `firmable mcp call bulk_reveal_person_emails` | **free** |
| mobile numbers | `bulk_get_people` (profile + email + phone) | 1 per person |
| enrich a list of domains you already have | `company-batch` | 1 per row |
| script something unattended with an API key | the REST commands | per lookup |
| push into HubSpot or Salesforce | `firmable mcp call push_to_crm` | 1 per profile |

The REST API cannot search. Every endpoint needs an identifier you already hold, so it enriches
but never discovers. It also authenticates with a plain API key, which makes it the right choice
for unattended jobs, where MCP wants a user login.

## Install

```bash
gh repo clone ben-broadley/firmable-cli ~/dev/firmable-cli
export PATH="$HOME/dev/firmable-cli:$PATH"
export FIRMABLE_API_KEY=fbl_...
```

The API key comes from the Firmable app under **Profile → Integrations → API**. The CLI reads
it from the environment and never writes it anywhere.

## Looking things up

Three endpoints, three commands.

| Command | Method | What it does |
| --- | --- | --- |
| `company` | `GET /company` | one company, by any identifier you have |
| `people` | `GET /people` | one person, by any identifier you have |
| `people-search` | `POST /people/search` | everyone at a company, filtered |

```bash
firmable commands                  # list everything
firmable help company              # flags for one command

firmable company --fqdn smec.com
firmable company --abn 47065475149 --csv smec.csv
firmable company --ln-slug smec --country NZ

firmable people --ln-url https://www.linkedin.com/in/some-slug/
firmable people --work-email someone@example.com.au

firmable people-search --company-id f000000117274 --department Engineering --all
```

`company` takes any one of `--id`, `--ln-slug`, `--ln-url`, `--fqdn`, `--abn`, `--website`.
`people` takes any one of `--id`, `--ln-slug`, `--ln-url`, `--work-email`, `--personal-email`.
Both accept `--country`, covering AU, NZ, SG, MY, ID, PH, HK, JP, VN, KR, TH, US and CA, and
defaulting to AU.

Every command writes JSON to stdout. Add `--csv PATH` for a flattened row per record,
`--out PATH` to write the JSON to a file, `--raw` to keep the response envelope untouched, or
`--dry-run` to see the request without sending it.

## Enriching a list

```bash
# one /company lookup per row, keyed on a column
firmable company-batch domains.csv --by fqdn --csv enriched.csv --keep-columns crm_id

# one /people lookup per row
firmable people-batch profiles.csv --by ln_url --csv contacts.csv

# everyone at each company, then full contact records for each of them
firmable roster companies.csv --enrich --only-contactable --csv people.csv
```

**It resumes.** Results stream to a JSONL sidecar as they land, and a re-run skips anything
already in it. Kill a job halfway through a 40,000-row list and start it again; it picks up
where it stopped and you pay for nothing twice. `--force` overrides.

**It only charges for records.** One credit per row that hits, nothing for a miss, nothing for
the searching. `roster` lists everyone at a company for free. Only `--enrich` spends, at one
credit per person, so `--only-contactable` is doing real work.

**It stays inside the rate limit.** Firmable allows 50 requests per second per key. The
default is 20 across 8 threads, tunable with `--rps` and `--concurrency`, and the CLI refuses
to go above 50.

**It never loses a row.** Every output row is marked `hit`, `miss` or `error` in a `_status`
column, so a short CSV is always explained rather than mysterious. `--keep-columns a,b` passes
input columns through as `in_a`, `in_b` so the result joins straight back onto your source.

**It tells misses apart from failures.** A lookup with no match answers `HTTP 500`, which
naive clients treat as a server fault and retry five times. This one recognises them, records
them as misses, and moves on. Real `5xx` and `429` responses are retried with backoff.

## Do Not Call

Firmable integrates the ACMA [Do Not Call Register](https://www.donotcall.gov.au) into its
dataset and keeps it refreshed. By their account they are the only Australian provider that
does. So the `is_dnd` flag on a phone number is genuine DNC Register status, not a vendor
guess, and it is one of the better reasons to use Firmable for Australian phone outreach.

The person flattener splits numbers on it, so the distinction survives into your CSV instead
of being buried in a nested field:

| Column | Contains |
| --- | --- |
| `mobile` | numbers **not** on the DNC Register |
| `mobile_dnd` | numbers **on** the DNC Register |
| `phones_all` | every number, unsplit |

Point a dialler at `mobile` and the registered numbers are already gone.

One caveat worth keeping in mind: the flag reflects the register as at Firmable's most recent
refresh, so it is a point-in-time status rather than a live check. For a list that will sit
for a while before anyone calls it, confirm currency at send time.

## MCP

Firmable runs an MCP server with a larger tool set than the three REST endpoints. This CLI
speaks MCP Streamable HTTP directly, so those tools are available without an MCP client.

```bash
firmable mcp login          # browser sign-in, once
firmable mcp status
firmable mcp tools          # every tool, with its input schema
firmable mcp call <tool> --arg query=physiotherapy --arg limit=25
firmable mcp call <tool> --args '{"filters":{"state":"VIC"}}'
firmable mcp raw resources/list
firmable mcp logout
```

Authentication is OAuth 2.1 with PKCE against Firmable's Clerk tenant. You sign in once in a
browser; the refresh token is cached in `~/.config/firmable/mcp.json` at mode `0600` and
renewed automatically on expiry or a `401`.

`firmable mcp` does **not** use `FIRMABLE_API_KEY`. It authenticates as your Firmable user, so
the two halves of this CLI carry independent credentials, and revoking one leaves the other
working.

Override any of it with `FIRMABLE_MCP_URL`, `FIRMABLE_MCP_CLIENT_ID`,
`FIRMABLE_MCP_CALLBACK_PORT` or `FIRMABLE_MCP_TOKEN_FILE`.

If the server answers `MCP access is disabled for this user`, MCP is not enabled on your
account yet. That is a switch on Firmable's side and no amount of client configuration will
move it.

### What the MCP server gives you that REST does not

28 tools, and the difference is not cosmetic. Run `firmable mcp tools` for the live list with
full schemas; the summary:

| Group | Tools | Cost |
| --- | --- | --- |
| **Discovery** | `ai_search`, `filter_search`, `get_filter_options`, `list_saved_searches` | **free** |
| **Lookalikes** | `find_similar_companies`, `find_similar_people` | free |
| **Profiles** | `get_company`, `get_person`, `bulk_get_companies`, `bulk_get_people` | 1 credit each, free on repeat |
| **Emails** | `reveal_person_email`, `bulk_reveal_person_emails` | **free, not credit-gated** |
| **Phones** | `reveal_person_phone`, `bulk_reveal_person_phones` | 1 credit each |
| **Lists** | `list_lists`, `get_list`, `create_list`, `rename_list`, add / remove / copy / move | free |
| **Signals** | `list_signal_agents`, `get_signal_agent`, `agent_signal_hits` | free |
| **CRM push** | `push_to_crm`, `bulk_push_list_to_crm` | 1 credit each |

Three things stand out.

**Search exists.** The REST API cannot find anything. Every endpoint needs an identifier you
already hold. `filter_search` and `ai_search` do actual discovery, by industry, location, size,
revenue, seniority, technographics or contact availability, and both are free. Their own
guidance is to prefer `filter_search`: it is more precise, and `ai_search` runs heavy
natural-language parsing server-side. Resolve option ids with `get_filter_options` first rather
than guessing them.

One gap worth knowing: **nothing searches by domain.** `filter_search` has no domain, website or
company-name filter. Its only identifier filters are `company_id` and `person_id`, and
`ai_search` treats a domain as text, so `"smec.com"` returns Commonwealth Bank and NAB. To turn a
domain into a company id for free, search the company *name* and confirm the `fqdn` that comes
back. Watch for near-name subsidiaries: `"SMEC"` also returns SMEC Power & Technology and SMEC
Testing Services, on different domains.

**Emails are free here.** `reveal_person_email` and `bulk_reveal_person_emails` cost nothing,
confirmed against a live balance, where the REST `/people` endpoint charges a credit for a
record containing the same address. If you want work emails at volume, this is the cheaper path
by a wide margin. Phones still cost a credit either way.

MCP phone records are also richer: `is_mobile`, `is_verified`, `is_primary` and `is_personal`
alongside `is_dnd`, so you can separate mobiles from landlines. REST gives you neither.

**Bulk means bulk.** `bulk_get_people` and `bulk_reveal_person_emails` take up to 100 profiles
per call. The REST batch commands in this CLI issue one HTTP request per row because that is all
the REST API offers.

Those costs are measured rather than quoted. See [Credits](#credits). Firmable draws them from
separate pools (`api_enrichment`, `people_phone_viewed`, `crm_push`), so a phone budget and an
enrichment budget run down independently.

Two behaviours worth knowing before you point an agent at this: credit-charging tools **run
immediately, with no confirmation step**, and `bulk_reveal_person_phones` is atomic, so a batch
larger than your remaining balance is rejected outright rather than partially filled.

Start a session with `get_workflow_guide`. It documents the intended chaining
(`get_*` → `find_similar_*` → `bulk_*` → `reveal_*`) and the filter encoding rules, which are
fiddly enough that guessing them wastes calls.

## Where the published spec and the live API disagree

`openapi.json` in this repo is Firmable's own, taken from
`docs.firmable.com/api-reference/openapi.json`. Three things in it do not match what the API
actually does. The CLI handles all three; anything written straight from the spec will trip
over them.

| The spec says | The API does |
| --- | --- |
| a miss returns `400` | a miss returns `500 {"error":"Company profile not found"}` |
| `POST /people/search` returns the results array | it `307`-redirects to an internal URL, then returns `{"records":[…],"total":N}` |
| nothing about pagination | `total` comes back with every page, and `from`/`size` walk it |

The redirect is the awkward one. Python's `urllib` refuses to replay a POST body across a
`307`, so the CLI follows it by hand, and only ever to the same host, so the bearer token is
never handed to somewhere else.

Firmable also advertises OAuth dynamic client registration at `/oauth/register`, but it
returns `500` for every payload, so the MCP client uses the published client ID and the
callback port from Firmable's own documentation instead.

## Credits

Measured against a live account on 2026-08-21, one call at a time against the balance.
Firmable does not publish an API credit model, and two of their own tool descriptions
disagree with each other, so these are observed numbers rather than quoted ones.

| Action | Cost |
| --- | --- |
| **Any search**: REST `people-search`, MCP `filter_search` / `ai_search` | **free** |
| MCP `reveal_person_email` / `bulk_reveal_person_emails` | **free** |
| MCP list, signal and filter-option tools | free |
| REST `company` / `people` that hits | 1 |
| MCP `get_company` / `get_person`, `bulk_get_*` | 1 each |
| MCP `reveal_person_phone` / `bulk_reveal_person_phones` | 1 each |
| A lookup that misses | 0 |
| Re-fetching a record you already bought | 0 |
| `--dry-run`, `help`, `commands` | 0 |

**Only record retrieval costs anything. Searching never does.** Search freely to size and
qualify before you spend: page every person at a company, see who has a mobile and who is
DNC-flagged, then buy only the ones worth having.

**Work emails are free, and only through MCP.** `bulk_reveal_person_emails` returns unmasked
work addresses for 100 people per call at no cost, where REST `/people` charges a credit for a
record carrying the same address. If you want emails at volume, never use the REST side.

**If you need a phone, buy the whole profile.** `get_person` and `reveal_person_phone` both cost
1 credit, but `get_person` returns the profile, the email *and* the phone, where
`reveal_person_phone` returns only the number. There is no reason to prefer the narrower one.

That gives one sensible default: **search free → reveal emails free → spend credits only on the
people you actually want phone numbers for.**

Repeat fetches are deduped per profile, so re-running a job is safe. Batch commands also resume
from their JSONL by default, which is the main defence against paying twice; `--force` is the
only way to override it.

### Rate limits

Limits are **per tool bucket**, and they are not uniform:

| Bucket | Behaviour |
| --- | --- |
| Search (`ai_search`, `filter_search`, REST `people-search`) | no throttling at 64 concurrent / 31 req/s across ~450 calls |
| Bulk enrichment (`bulk_reveal_person_emails`) | throttled after a couple of calls; retry hints of 21s and 60s, so roughly **100 records per 20–60s** sustained |

**A throttled call returns HTTP 200 with the error in the body:**

```json
{"success": false, "code": "rate_limited", "error": "... Retry in 36s.", "status": 429}
```

A client that only checks the HTTP status records that as a success and **silently
drops every record in the chunk**. 250 ids in, 200 rows out, nothing raised.
`firmable emails` and `firmable phones` inspect the body and honour the retry hint,
which is the main reason to use them rather than looping over `firmable mcp call`
yourself.

Defaults are 5 req/s over 4 workers, deliberately below what the server permits.
Sustained load from a named account is how you get a limit imposed. Since the bulk
tools take 100 records per call, a slow request rate still moves plenty.

### You cannot read your balance programmatically

There is no credits endpoint on the REST API, no usage tool among the 28 MCP tools, and no
usage metadata on any response. REST returns no credit headers, and the MCP envelope carries
only `content`, `structuredContent` and `isError`. The only place a balance appears is the
Firmable web app.

Two consequences if you are scripting this:

- **A run cannot check its own budget before spending.** Bound it up front with `--limit`,
  `--per-company` or `--only-contactable`, rather than expecting it to stop itself.
- **You find out you are empty by failing.** Phone reveals return `code="no_credits"`, and
  `bulk_reveal_person_phones` is atomic, so a batch larger than the remaining balance is
  rejected whole rather than partially filled. Handle that error rather than assuming a partial
  result.

This is also why the costs in this README were measured by watching the balance across single
calls. There was no other way to establish them.

## Licence

MIT.
