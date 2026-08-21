# firmable-cli

Unofficial, single-file, stdlib-only Python 3 CLI over the [Firmable](https://firmable.com)
REST API — plus a client for Firmable's MCP server, so its agent tools work from a
terminal instead of only from inside Claude/ChatGPT/Cursor.

Not affiliated with, endorsed by, or sponsored by Firmable.

Firmable is the AU/NZ B2B sales-intelligence database: ~1.5M companies and ~10M people,
carrying ABN/ACN/NZBN, ANZSIC codes, technographics, work emails and mobiles.

- No dependencies. Python 3.9+. Two files and a spec.
- Every documented REST operation, generated from `openapi.json`.
- Batch enrichment: CSV in, JSONL + flattened CSV out, rate limited, resumable.
- `firmable mcp`: OAuth sign-in, tool discovery, and tool calls against the MCP server.

## Install

```bash
gh repo clone ben-broadley/firmable-cli ~/dev/firmable-cli
export PATH="$HOME/dev/firmable-cli:$PATH"
export FIRMABLE_API_KEY=fbl_...
```

Create the API key in the Firmable app under **Profile → Integrations → API**.

## REST commands

```
company        GET  /company        enrich one company
people         GET  /people         enrich one person
people-search  POST /people/search  find people at a company
```

```bash
firmable commands                 # list everything
firmable help company             # flags for one command
firmable company --fqdn smec.com
firmable company --abn 47065475149 --csv smec.csv
firmable people  --ln-url https://www.linkedin.com/in/some-slug/
firmable people-search --company-id f000000117274 --seniority C-Suite --all
```

`company` accepts any one of `--id --ln-slug --ln-url --fqdn --abn --website`, and
`--country` (AU NZ SG MY ID PH HK JP VN KR TH US CA; AU by default).
`people` accepts `--id --ln-slug --ln-url --work-email --personal-email`.

`--csv PATH` writes a flattened row per record next to the JSON. `--raw` keeps the
response envelope untouched. `--dry-run` prints the request without sending it.

## Batch

```bash
# one /company lookup per row, keyed on a column
firmable company-batch domains.csv --by fqdn --csv enriched.csv --keep-columns crm_id

# one /people lookup per row
firmable people-batch profiles.csv --by ln_url --csv contacts.csv

# every person at each company id, then full contact records for each
firmable roster companies.csv --column id --enrich --only-contactable --csv people.csv
```

Behaviour worth knowing:

- **Resumable by default.** Results stream to a JSONL sidecar as they land; a re-run
  skips inputs already in it, so an interrupted job never re-buys a row. `--force`
  overrides.
- **Rate limited.** Firmable allows 50 requests/second per key. Default is 20 rps over
  8 threads; `--rps` / `--concurrency` to change, and the CLI refuses to exceed 50.
- **Misses are misses.** A lookup with no match answers `HTTP 500
  {"error":"Company profile not found"}`. Those are recorded as `_status=miss` and
  never retried; genuine 5xx and 429 are retried with backoff.
- **`_status` column.** Every output row is `hit`, `miss`, or `error`, so a CSV never
  silently loses rows.
- **`--keep-columns a,b`** echoes input columns through as `in_a`, `in_b` for joining.

### DNC / do-not-call

Person records carry `phones[].is_dnd`. The flattener splits them:

| column | meaning |
| --- | --- |
| `mobile` | numbers **not** flagged do-not-disturb |
| `mobile_dnd` | numbers Firmable flags do-not-disturb |
| `phones_all` | everything, unfiltered |

`is_dnd` is Firmable's own signal. It is not a
[DNCR](https://www.donotcall.gov.au) wash — if you are dialling Australian numbers,
wash them against the register before delivery.

## MCP

Firmable's MCP server exposes a larger tool set than the three REST endpoints. This
CLI speaks MCP Streamable HTTP directly, so those tools work without an MCP client.

```bash
firmable mcp login          # browser sign-in, once (OAuth 2.1 + PKCE)
firmable mcp status
firmable mcp tools          # every tool with its input schema
firmable mcp call <tool> --arg query=physiotherapy --arg limit=25
firmable mcp call <tool> --args '{"filters":{"state":"VIC"}}'
firmable mcp raw resources/list
firmable mcp logout
```

Auth is Clerk-backed OAuth: authorization code + PKCE against a public client, with a
refresh token, so you sign in once. Tokens are cached in `~/.config/firmable/mcp.json`
(mode 0600) and refreshed automatically on expiry or a 401.

`firmable mcp` does **not** use `FIRMABLE_API_KEY` — it authenticates as your Firmable
user. The two auth paths are independent.

Overrides: `FIRMABLE_MCP_URL`, `FIRMABLE_MCP_CLIENT_ID`, `FIRMABLE_MCP_CALLBACK_PORT`,
`FIRMABLE_MCP_TOKEN_FILE`.

## Where the published spec is wrong

`openapi.json` is Firmable's own, fetched from `docs.firmable.com/api-reference/openapi.json`.
Three things in it do not match the live API; the CLI handles all three:

| Spec says | Actually |
| --- | --- |
| a miss is a `400` | a miss is `500 {"error":"Company profile not found"}` |
| `POST /people/search` returns the results array | it 307-redirects to an internal URL, then returns `{"records":[…],"total":N}` |
| no pagination contract | `total` is returned, and `from`/`size` page through it |

The 307 matters: `urllib` refuses to replay a POST body across a 307, so the CLI
handles the redirect itself — and only ever to the same host, so the bearer token is
never forwarded elsewhere.

Firmable also advertises OAuth dynamic client registration at `/oauth/register`, but it
returns `500` for every payload, so the MCP client uses the published client ID and the
callback port (`53612`) from Firmable's own docs.

## Credits

Every successful lookup spends Firmable credits. `roster --enrich` spends one extra
lookup per person found — use `--per-company` and `--only-contactable` to bound it.
Resume-by-default exists for this reason. `--dry-run` costs nothing.

## Licence

MIT.
