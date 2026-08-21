# firmable-cli

A command-line interface to [Firmable](https://firmable.com), the Australian and New Zealand
B2B database: around 1.5 million companies and 10 million people, carrying ABNs, ACNs and
NZBNs, ANZSIC codes, technographics, work emails, mobile numbers and Do Not Call status.

Unofficial. Not affiliated with, endorsed by, or sponsored by Firmable.

```bash
firmable company --fqdn canva.com
firmable people-search --company-id f000000117274 --seniority C-Suite --all
firmable company-batch domains.csv --by fqdn --csv enriched.csv
firmable mcp call company_search --arg query="physiotherapy clinics in Victoria"
```

Two files and a spec. No dependencies, Python 3.9 or newer.

- **Every documented REST operation**, with flags and help text generated from Firmable's
  own `openapi.json`.
- **Batch enrichment.** Point it at a CSV column, get back a flattened CSV. Rate limited,
  resumable, and it will not re-buy a row you already paid for.
- **MCP from the shell.** Firmable's MCP server normally only answers to Claude, ChatGPT or
  Cursor. `firmable mcp` speaks the protocol directly, so the same tools work in a terminal,
  a cron job, or anything that can shell out.

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

**It costs what you'd expect.** One credit per row that hits, nothing for a miss. `roster` is
the exception — see [Credits](#credits) before pointing it at large companies.

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

| Action | Cost |
| --- | --- |
| `company` / `people` lookup that hits | 1 credit |
| `people-search` | **1 credit per call** |
| lookup that misses | 0 |
| re-fetching a record you already bought | 0 |
| `--dry-run`, `help`, `commands` | 0 |

**`people-search` bills per call, not per record.** This is the one that catches people out.
A single call returning 200 people costs the same as one returning 2, so paging is what costs
you: `--all` over 2,000 people at the default page size is 20 calls and 20 credits. Pull the
biggest page you can rather than walking small ones.

```bash
firmable people-search --company-id X --all --page-size 200   # cheaper
firmable people-search --company-id X --all --page-size 10    # 20x the calls
```

`roster` inherits this. Listing everyone at a 1,600-person company is around 16 credits before
you enrich anybody, and `--enrich` then buys one lookup per person on top. Bound both ends with
`--per-company` and `--only-contactable`.

Batch commands resume from their JSONL by default, so an interrupted run never re-buys a row.
That is the main defence against paying twice, and `--force` is the only way to override it.

## Licence

MIT.
