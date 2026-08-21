"""firmable volume commands — run the MCP tools at scale, with the cost model built in.

`firmable mcp call <tool>` is the raw escape hatch. These are the commands you
actually want for volume: they chunk to the bulk endpoints, rate limit
themselves, resume, write CSV, and — the important part — they know what spends
credits and what does not.

    firmable costs                 # the whole cost contract, human readable
    firmable costs --json          # ... machine readable, for agents
    firmable search --query "physio clinics in Melbourne" --csv out.csv
    firmable emails people.csv --csv contacts.csv          # FREE
    firmable profiles people.csv --csv full.csv --yes      # 1 credit each
    firmable phones people.csv --csv mobiles.csv --yes     # 1 credit each

Anything that spends credits refuses to run without --yes, and tells you the
maximum it could cost first. Free commands just run.

Rate limiting: Firmable's MCP endpoint did not throttle at 64 concurrent /
31 req/s in testing, but that is not a licence to hammer it — a named account
generating that load looks like abuse whether or not it is permitted. These
commands default to 5 req/s over 4 workers. Because the bulk tools take 100
records per call, that still moves ~500 records/second, so politeness costs
nothing. --rps and --concurrency raise it; above RPS_CEILING the CLI refuses.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

DEFAULT_RPS = 5.0
DEFAULT_CONCURRENCY = 4
RPS_CEILING = 25.0
BULK_CHUNK = 100

FREE = "free"
PER_ITEM = "per_item"

# ---------------------------------------------------------------------------
# THE COST CONTRACT
#
# basis:
#   measured — spent against a live balance, one action at a time, 2026-08-21
#   declared — Firmable's own tool description, not independently confirmed
#
# Keep this table honest. It is what `firmable costs --json` publishes, and what
# an agent reads to decide whether an action is safe to take unattended.
# ---------------------------------------------------------------------------
COSTS: dict[str, dict] = {
    # ---- MCP: free ----
    "mcp:get_workflow_guide":       {"kind": FREE, "credits": 0, "basis": "declared", "what": "the server's own usage guide"},
    "mcp:ai_search":                {"kind": FREE, "credits": 0, "basis": "declared", "what": "natural-language search"},
    "mcp:filter_search":            {"kind": FREE, "credits": 0, "basis": "measured", "what": "structured search"},
    "mcp:get_filter_options":       {"kind": FREE, "credits": 0, "basis": "declared", "what": "resolve filter option ids"},
    "mcp:list_saved_searches":      {"kind": FREE, "credits": 0, "basis": "declared", "what": "saved searches"},
    "mcp:find_similar_companies":   {"kind": FREE, "credits": 0, "basis": "declared", "what": "company lookalikes"},
    "mcp:find_similar_people":      {"kind": FREE, "credits": 0, "basis": "declared", "what": "persona lookalikes"},
    "mcp:reveal_person_email":      {"kind": FREE, "credits": 0, "basis": "measured", "what": "one person's work + personal email"},
    "mcp:bulk_reveal_person_emails":{"kind": FREE, "credits": 0, "basis": "measured", "what": "emails for up to 100 people"},
    "mcp:list_lists":               {"kind": FREE, "credits": 0, "basis": "declared", "what": "workspace lists"},
    "mcp:get_list":                 {"kind": FREE, "credits": 0, "basis": "declared", "what": "one list's contents"},
    "mcp:create_list":              {"kind": FREE, "credits": 0, "basis": "declared", "what": "create a list"},
    "mcp:rename_list":              {"kind": FREE, "credits": 0, "basis": "declared", "what": "rename a list"},
    "mcp:add_profiles_to_list":     {"kind": FREE, "credits": 0, "basis": "declared", "what": "add to a list"},
    "mcp:remove_profiles_from_list":{"kind": FREE, "credits": 0, "basis": "declared", "what": "remove from a list"},
    "mcp:copy_profiles_between_lists": {"kind": FREE, "credits": 0, "basis": "declared", "what": "copy between lists"},
    "mcp:move_profiles_between_lists": {"kind": FREE, "credits": 0, "basis": "declared", "what": "move between lists"},
    "mcp:list_signal_agents":       {"kind": FREE, "credits": 0, "basis": "declared", "what": "signal agents"},
    "mcp:get_signal_agent":         {"kind": FREE, "credits": 0, "basis": "declared", "what": "one agent's config"},
    "mcp:agent_signal_hits":        {"kind": FREE, "credits": 0, "basis": "declared", "what": "job changes, funding, intent"},
    # ---- MCP: charged ----
    "mcp:get_company":              {"kind": PER_ITEM, "credits": 1, "basis": "declared", "what": "one company profile"},
    "mcp:get_person":               {"kind": PER_ITEM, "credits": 1, "basis": "measured", "what": "one person profile, incl. email AND phone"},
    "mcp:bulk_get_companies":       {"kind": PER_ITEM, "credits": 1, "basis": "declared", "what": "up to 100 company profiles"},
    "mcp:bulk_get_people":          {"kind": PER_ITEM, "credits": 1, "basis": "declared", "what": "up to 100 person profiles"},
    "mcp:reveal_person_phone":      {"kind": PER_ITEM, "credits": 1, "basis": "measured", "what": "one person's mobile"},
    "mcp:bulk_reveal_person_phones":{"kind": PER_ITEM, "credits": 1, "basis": "declared", "what": "mobiles for up to 100 people (ATOMIC)"},
    "mcp:push_to_crm":              {"kind": PER_ITEM, "credits": 1, "basis": "declared", "what": "push profiles to the connected CRM"},
    "mcp:bulk_push_list_to_crm":    {"kind": PER_ITEM, "credits": 1, "basis": "declared", "what": "push a whole list to the CRM"},
    # ---- REST ----
    "rest:people-search":           {"kind": FREE, "credits": 0, "basis": "measured", "what": "people at a company (paging is free too)"},
    "rest:company":                 {"kind": PER_ITEM, "credits": 1, "basis": "measured", "what": "one company record"},
    "rest:people":                  {"kind": PER_ITEM, "credits": 1, "basis": "measured", "what": "one person record"},
}

# Per-tool rate limits, measured 2026-08-21. Buckets are NOT uniform: search is
# effectively unthrottled, bulk enrichment is throttled hard. A throttled call
# returns HTTP 200 with the error in the body — see check_payload().
RATE_LIMITS = {
    "search": {
        "tools": ["ai_search", "filter_search", "get_filter_options", "people-search"],
        "observed": "no throttling at 64 concurrent / 31 req/s across ~450 calls",
        "throttled": False,
    },
    "enrichment": {
        "tools": ["bulk_reveal_person_emails", "reveal_person_email"],
        "observed": "throttled after a couple of calls; retry hints of 21s and 60s",
        "sustained": "~100 records per 20-60s",
        "throttled": True,
        "signal": 'HTTP 200 with {"success": false, "code": "rate_limited", "status": 429}',
    },
}

# The decision an agent should make BEFORE reaching for a paid call. Published in
# `firmable costs --json` so it does not depend on anyone reading the prose.
PREFER = [
    {"instead_of": "rest:company", "use": "mcp:ai_search by company NAME, then match the returned fqdn",
     "saves": "1 credit per company",
     "caveat": "ai_search on a bare DOMAIN returns nonsense; there is no domain/name filter in filter_search. "
               "Search the name, verify fqdn. Fall back to the paid lookup only when nothing matches."},
    {"instead_of": "rest:people", "use": "mcp:bulk_reveal_person_emails",
     "saves": "1 credit per person, when you only need the email",
     "caveat": "phones still cost; REST /people charges for a record carrying the same address"},
    {"instead_of": "mcp:bulk_reveal_person_phones", "use": "mcp:bulk_get_people",
     "saves": "nothing, but returns the profile and email for the same 1 credit",
     "caveat": "only worth buying for people whose free has_mobile flag is already true"},
    {"instead_of": "buying records to see who exists", "use": "free search + has_email / has_mobile / has_dnd_phone flags",
     "saves": "everything — sizing an account costs nothing",
     "caveat": "the flags tell you whether contact data exists, not what it is"},
]

NOTES = [
    "Only record retrieval costs credits. Searching never does, on either surface.",
    "Work emails are FREE via MCP, and cost 1 credit via REST /people for the same address.",
    "A lookup that misses costs nothing. Re-fetching a record you already bought costs nothing.",
    "If you need a phone, prefer mcp:get_person over mcp:reveal_person_phone — same 1 credit, but it returns the profile and email too.",
    "bulk_reveal_person_phones is ATOMIC: a batch larger than your balance is rejected whole, no partial charge.",
    "There is NO way to read your credit balance programmatically. Bound runs up front; you discover exhaustion by failing.",
    "Credit-charging MCP tools run immediately — the server has no approval gate. This CLI adds one: paid commands need --yes.",
    "Rate limits are PER TOOL BUCKET: search is unthrottled, bulk enrichment is throttled to ~100 records per 20-60s.",
    "A throttled call returns HTTP 200 with the error in the BODY. Checking only the HTTP status silently drops the whole chunk.",
    "Before any paid call, check PREFER (firmable costs --json) — there is usually a free route.",
]


def is_free(key: str) -> bool:
    entry = COSTS.get(key)
    return bool(entry) and entry["kind"] == FREE


def estimate(key: str, units: int) -> int:
    entry = COSTS.get(key)
    if not entry or entry["kind"] == FREE:
        return 0
    return entry["credits"] * units


def cmd_costs(argv: list[str]) -> None:
    p = argparse.ArgumentParser(prog="firmable costs",
                                description="What spends credits and what does not.")
    p.add_argument("--json", action="store_true", help="Machine-readable, for agents")
    p.add_argument("--free", action="store_true", help="Only the free operations")
    p.add_argument("--paid", action="store_true", help="Only the operations that spend")
    args = p.parse_args(argv)

    items = COSTS.items()
    if args.free:
        items = [(k, v) for k, v in items if v["kind"] == FREE]
    if args.paid:
        items = [(k, v) for k, v in items if v["kind"] != FREE]
    items = list(items)

    if args.json:
        print(json.dumps({
            "credits_per_unit": {k: v["credits"] for k, v in items},
            "operations": {k: v for k, v in items},
            "prefer_free_alternatives": PREFER,
            "rate_limits": RATE_LIMITS,
            "notes": NOTES,
            "measured_on": "2026-08-21",
            "balance_readable_programmatically": False,
        }, indent=2))
        return

    width = max(len(k) for k, _ in items)
    print("FREE — run these as much as you like\n")
    for k, v in items:
        if v["kind"] != FREE:
            continue
        print(f"  {k:<{width}}  {v['what']}   [{v['basis']}]")
    paid = [(k, v) for k, v in items if v["kind"] != FREE]
    if paid:
        print("\nCOSTS CREDITS — 1 per record, and this CLI will not run them without --yes\n")
        for k, v in paid:
            print(f"  {k:<{width}}  {v['what']}   [{v['basis']}]")
    print("\nNotes:")
    for n in NOTES:
        print(f"  - {n}")
    print("\nBEFORE PAYING, CHECK THERE IS NOT A FREE ROUTE:\n")
    for alt in PREFER:
        print(f"  instead of {alt['instead_of']}")
        print(f"    use  {alt['use']}")
        print(f"    why  {alt['saves']}")
        print(f"    but  {alt['caveat']}\n")
    print("Rate limits (per tool bucket, measured 2026-08-21):")
    for name, rl in RATE_LIMITS.items():
        print(f"  {name:<11} {rl['observed']}")
        if rl.get("sustained"):
            print(f"              sustained: {rl['sustained']}")
    print("\nbasis: measured = spent against a live balance 2026-08-21; "
          "declared = Firmable's own description, unconfirmed.")


# ---------------------------------------------------------------------------
# shared plumbing
# ---------------------------------------------------------------------------

def _j(v, sep: str = "; ") -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, list):
        out = []
        for x in v:
            if x is None:
                continue
            out.append(json.dumps(x, ensure_ascii=False) if isinstance(x, (dict, list)) else str(x))
        return sep.join(out)
    if isinstance(v, dict):
        return json.dumps(v, ensure_ascii=False)
    return str(v)


def write_csv(path: str, rows: list[dict], columns: list[str]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)
    sys.stderr.write(f"wrote {len(rows):,} rows to {path}\n")


def read_ids(path: str | None, column: str | None, inline: str | None) -> list[str]:
    """Ids from --ids, or a CSV column, or a bare one-per-line file, or stdin."""
    if inline:
        return [x.strip() for x in inline.split(",") if x.strip()]
    if not path:
        sys.exit("supply an input file or --ids")
    text = sys.stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8-sig")
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        sys.exit(f"{path}: empty")
    if "," in lines[0] or (column and column in lines[0]):
        reader = csv.DictReader(lines)
        fields = list(reader.fieldnames or [])
        chosen = column
        if not chosen:
            for candidate in ("person_id", "id", "profile_id", "company_id"):
                if candidate in fields:
                    chosen = candidate
                    break
        if not chosen and len(fields) == 1:
            chosen = fields[0]
        if not chosen:
            sys.exit(f"{path}: pass --column, one of: {', '.join(fields)}")
        return [r[chosen].strip() for r in reader if (r.get(chosen) or "").strip()]
    return [ln.strip() for ln in lines if ln.strip()]


class RateLimited(Exception):
    """The tool answered HTTP 200 with a rate-limit error in the body."""

    def __init__(self, message: str, retry_after: float):
        self.retry_after = retry_after
        super().__init__(message)


def check_payload(payload):
    """Firmable reports tool-level failures INSIDE a 200 response.

    bulk_reveal_person_emails answers {"success": false, "code": "rate_limited",
    "error": "... Retry in 36s."} with a 200 status, so a client that only looks
    at HTTP codes records a throttled chunk as a success and silently drops
    every record in it. Rate limits are per tool bucket — search is not
    throttled, enrichment is — so this only shows up under real volume.
    """
    if not isinstance(payload, dict) or payload.get("success") is not False:
        return payload
    code = payload.get("code") or ""
    message = str(payload.get("error") or code or "tool reported failure")
    if code == "rate_limited" or payload.get("status") == 429:
        seconds = 30.0
        match = re.search(r"retry in\s*(\d+(?:\.\d+)?)\s*s", message, re.I)
        if match:
            seconds = float(match.group(1))
        raise RateLimited(message, seconds)
    raise RuntimeError(message)


def _chunks(items: list, n: int):
    for i in range(0, len(items), n):
        yield items[i:i + n]


def _confirm(key: str, units: int, yes: bool) -> None:
    """Refuse to spend credits unless explicitly allowed."""
    cost = estimate(key, units)
    if cost == 0:
        sys.stderr.write(f"{key}: free — {units:,} records, 0 credits\n")
        return
    if yes or os.environ.get("FIRMABLE_ASSUME_YES") == "1":
        sys.stderr.write(f"{key}: spending up to {cost:,} credits for {units:,} records\n")
        return
    lines = [f"{key} costs credits: up to {cost:,} for {units:,} records."]
    # Surface the free route at the exact moment someone reaches for the paid one.
    for alt in PREFER:
        if alt["instead_of"] == key:
            lines += ["", "  There may be a free alternative:",
                      f"    use  {alt['use']}",
                      f"    why  {alt['saves']}",
                      f"    but  {alt['caveat']}"]
    lines += ["",
              "  If you do want to spend, re-run with --yes (or FIRMABLE_ASSUME_YES=1 for",
              "  unattended runs — bound it with --limit first).",
              "  `firmable costs` lists everything that is free."]
    sys.exit("\n".join(lines))


class _Limiter:
    def __init__(self, rps: float):
        self.interval = (1.0 / rps) if rps > 0 else 0.0
        self._lock = threading.Lock()
        self._next = 0.0

    def acquire(self) -> None:
        if not self.interval:
            return
        with self._lock:
            now = time.monotonic()
            wait = max(0.0, self._next - now)
            self._next = max(now, self._next) + self.interval
        if wait:
            time.sleep(wait)


def _load_done(path: Path) -> set:
    done = set()
    if path.exists():
        for line in path.open(encoding="utf-8"):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            for i in rec.get("_ids") or []:
                done.add(i)
    return done


def run_chunked(ids: list[str], tool: str, build_args, out_path: Path,
                rps: float, concurrency: int, resume: bool, verbose: bool) -> None:
    """Fan `ids` out over a bulk MCP tool in chunks of 100, appending JSONL."""
    import firmable_mcp as m

    if rps > RPS_CEILING:
        sys.exit(f"--rps {rps} is above this CLI's {RPS_CEILING}/s ceiling. "
                 "The server did not throttle in testing, but sustained load on a named "
                 "account is a good way to get one imposed.")

    done = _load_done(out_path) if resume else set()
    todo = [i for i in dict.fromkeys(ids) if i and i not in done]
    if done:
        sys.stderr.write(f"{tool}: {len(done):,} already done, {len(todo):,} to go\n")
    if not todo:
        sys.stderr.write(f"{tool}: nothing to do\n")
        return

    session = m.McpSession(verbose=verbose)
    session.initialize()
    limiter = _Limiter(rps)
    lock = threading.Lock()
    batches = list(_chunks(todo, BULK_CHUNK))
    counts = {"ok": 0, "err": 0}
    started = time.monotonic()
    out_path.parent.mkdir(parents=True, exist_ok=True)

    def work(batch: list[str], attempts: int = 5):
        """Call the tool, honouring the retry delay the server hands back."""
        for attempt in range(1, attempts + 1):
            limiter.acquire()
            result = session.call_tool(tool, build_args(batch))
            payload = result.get("structuredContent") if isinstance(result, dict) else result
            try:
                return check_payload(payload)
            except RateLimited as e:
                if attempt == attempts:
                    raise
                delay = e.retry_after + 1.0
                with lock:
                    sys.stderr.write(
                        f"\n{tool}: rate limited, waiting {delay:.0f}s "
                        f"(attempt {attempt}/{attempts})\n"
                    )
                time.sleep(delay)
        raise RuntimeError("unreachable")

    with out_path.open("a", encoding="utf-8") as fh, ThreadPoolExecutor(max_workers=concurrency) as ex:
        futures = {ex.submit(work, b): b for b in batches}
        for n, fut in enumerate(as_completed(futures), 1):
            batch = futures[fut]
            try:
                payload = fut.result()
                rec = {"_ids": batch, "_ok": True, "data": payload}
                counts["ok"] += len(batch)
            except Exception as e:  # noqa: BLE001 — one bad chunk must not kill the run
                rec = {"_ids": batch, "_ok": False, "_error": str(e)[:400]}
                counts["err"] += len(batch)
            with lock:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
                fh.flush()
            elapsed = max(0.001, time.monotonic() - started)
            sys.stderr.write(
                f"\r{tool}: chunk {n}/{len(batches)}  ok={counts['ok']:,} "
                f"err={counts['err']:,}  {counts['ok'] / elapsed:.0f} rec/s"
            )
            sys.stderr.flush()
    sys.stderr.write("\n")


# ---------------------------------------------------------------------------
# search — FREE
# ---------------------------------------------------------------------------

SEARCH_COLUMNS = [
    "person_id", "company_id", "name", "position", "company_name", "fqdn",
    "industry", "hq_country", "global_company_size", "linkedin", "linkedin_url",
    "has_email", "has_mobile", "has_dnd_phone", "has_personal_email",
]


def flatten_hit(rec: dict) -> dict:
    slug = rec.get("linkedin") or ""
    row = {c: "" for c in SEARCH_COLUMNS}
    row.update({
        "person_id": _j(rec.get("person_id")),
        "company_id": _j(rec.get("company_id")),
        "name": _j(rec.get("name") or rec.get("pName")),
        "position": _j(rec.get("position")),
        "company_name": _j(rec.get("company_name")),
        "fqdn": _j(rec.get("fqdn")),
        "industry": _j(rec.get("industry")),
        "hq_country": _j(rec.get("hq_country")),
        "global_company_size": _j(rec.get("global_company_size")),
        "linkedin": _j(slug),
        "linkedin_url": f"https://www.linkedin.com/in/{slug}/" if slug and rec.get("person_id") else "",
        "has_email": _j(rec.get("has_email")),
        "has_mobile": _j(rec.get("has_mobile")),
        "has_dnd_phone": _j(rec.get("has_dnd_phone")),
        "has_personal_email": _j(rec.get("has_personal_email")),
    })
    return row


def cmd_search(argv: list[str]) -> None:
    p = argparse.ArgumentParser(
        prog="firmable search",
        description="Discovery across Firmable. FREE — search never costs credits, so page freely.",
    )
    p.add_argument("--query", help="Natural language (uses ai_search)")
    p.add_argument("--filters", help="Structured filters as JSON (uses filter_search)")
    p.add_argument("--category", choices=["company", "people"], default="company")
    p.add_argument("--country", help="ISO code, e.g. AU. Defaults to AU server-side")
    p.add_argument("--size", type=int, default=20, help="Records per page (filter_search only)")
    p.add_argument("--max", type=int, default=100, help="Stop after this many records (default 100)")
    p.add_argument("--all", action="store_true", help="Keep paging until exhausted or --max")
    p.add_argument("--person-per-company", type=int, help="Cap people returned per company")
    p.add_argument("--csv", help="Write a flattened CSV here")
    p.add_argument("--out", help="Write raw JSON here")
    p.add_argument("--rps", type=float, default=DEFAULT_RPS)
    p.add_argument("--verbose", action="store_true")
    args = p.parse_args(argv)

    if not args.query and not args.filters:
        sys.exit("supply --query (natural language) or --filters (JSON).\n"
                 "  Resolve filter option ids first: firmable mcp call get_filter_options "
                 "--arg identifier=industry --arg searchTerm=software")

    import firmable_mcp as m
    session = m.McpSession(verbose=args.verbose)
    session.initialize()
    limiter = _Limiter(args.rps)

    tool = "ai_search" if args.query else "filter_search"
    sys.stderr.write(f"{tool}: free — searching costs no credits\n")

    records: list = []
    offset = 0
    while True:
        limiter.acquire()
        if args.query:
            payload = {"query": args.query, "category": args.category, "from": str(offset)}
            if args.country:
                payload["country"] = args.country
        else:
            payload = {
                "category": args.category,
                "filters": json.loads(args.filters),
                "size": str(args.size),
                "from": str(offset),
            }
            if args.person_per_company:
                payload["person_per_company"] = args.person_per_company
        result = session.call_tool(tool, payload)
        data = result.get("structuredContent") if isinstance(result, dict) else result
        page = (data or {}).get("records") or []
        if not page:
            break
        records.extend(page)
        sys.stderr.write(f"\r{tool}: {len(records):,} records")
        sys.stderr.flush()
        if len(records) >= args.max or not args.all:
            break
        offset += len(page)
    sys.stderr.write("\n")
    records = records[: args.max]

    totals = {k: v for k, v in (data or {}).items() if k.startswith("total")}
    if totals:
        sys.stderr.write(f"{tool}: matched {totals}\n")

    if args.csv:
        write_csv(args.csv, [flatten_hit(r) for r in records if isinstance(r, dict)], SEARCH_COLUMNS)
    text = json.dumps({"count": len(records), **totals, "records": records}, indent=2, ensure_ascii=False)
    if args.out:
        Path(args.out).write_text(text)
        sys.stderr.write(f"wrote {len(text):,} bytes to {args.out}\n")
    elif not args.csv:
        print(text)


# ---------------------------------------------------------------------------
# emails — FREE, 100 per call
# ---------------------------------------------------------------------------

EMAIL_COLUMNS = ["person_id", "name", "work_email", "personal_email",
                 "work_all", "verified", "deliverability", "_status"]


def _emails_of(rec: dict, kind: str) -> list[dict]:
    e = rec.get("emails") or {}
    return [x for x in (e.get(kind) or []) if isinstance(x, dict)]


def cmd_emails(argv: list[str]) -> None:
    p = argparse.ArgumentParser(
        prog="firmable emails",
        description="Work + personal emails for many people. FREE — 100 per call, no credits.",
    )
    p.add_argument("input", nargs="?", help="CSV/text file of person ids (- for stdin)")
    p.add_argument("--ids", help="Comma-separated person ids instead of a file")
    p.add_argument("--column", help="CSV column holding the person id")
    p.add_argument("--csv", help="Write a flattened CSV here")
    p.add_argument("--out", help="JSONL output path")
    p.add_argument("--rps", type=float, default=DEFAULT_RPS)
    p.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    p.add_argument("--force", action="store_true", help="Ignore the resume file")
    p.add_argument("--verbose", action="store_true")
    args = p.parse_args(argv)

    ids = read_ids(args.input, args.column, args.ids)
    _confirm("mcp:bulk_reveal_person_emails", len(ids), yes=True)  # free; never gated

    out_path = Path(args.out) if args.out else Path(
        (args.csv or (args.input if args.input and args.input != "-" else "firmable")) + ".emails.jsonl")
    run_chunked(ids, "bulk_reveal_person_emails", lambda b: {"ids": b},
                out_path, args.rps, args.concurrency, resume=not args.force,
                verbose=args.verbose)

    if args.csv:
        rows = []
        for line in out_path.open(encoding="utf-8"):
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            if not rec.get("_ok"):
                for i in rec.get("_ids", []):
                    rows.append({"person_id": i, "_status": "error"})
                continue
            data = rec.get("data")
            people = data if isinstance(data, list) else (data or {}).get("records") or []
            for person in people:
                if not isinstance(person, dict):
                    continue
                work = _emails_of(person, "work")
                personal = _emails_of(person, "personal")
                rows.append({
                    "person_id": _j(person.get("id")),
                    "name": _j(person.get("name")),
                    "work_email": _j(work[0]["value"]) if work else "",
                    "personal_email": _j(personal[0]["value"]) if personal else "",
                    "work_all": _j([x.get("value") for x in work]),
                    "verified": _j(work[0].get("is_verified")) if work else "",
                    "deliverability": _j(work[0].get("deliverability")) if work else "",
                    "_status": "hit" if work or personal else "no email",
                })
        write_csv(args.csv, rows, EMAIL_COLUMNS)


# ---------------------------------------------------------------------------
# profiles / phones — THESE SPEND CREDITS
# ---------------------------------------------------------------------------

def cmd_profiles(argv: list[str]) -> None:
    p = argparse.ArgumentParser(
        prog="firmable profiles",
        description="Full person profiles — email AND phone AND firmographics. "
                    "COSTS 1 CREDIT PER PERSON. Needs --yes.",
    )
    p.add_argument("input", nargs="?", help="CSV/text file of person ids (- for stdin)")
    p.add_argument("--ids", help="Comma-separated ids instead of a file")
    p.add_argument("--column", help="CSV column holding the id")
    p.add_argument("--companies", action="store_true",
                   help="Treat ids as COMPANY ids (bulk_get_companies)")
    p.add_argument("--csv", help="Write a flattened CSV here")
    p.add_argument("--out", help="JSONL output path")
    p.add_argument("--rps", type=float, default=DEFAULT_RPS)
    p.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    p.add_argument("--limit", type=int, help="Only take the first N ids — bound the spend")
    p.add_argument("--yes", action="store_true", help="Confirm the credit spend")
    p.add_argument("--force", action="store_true", help="Ignore the resume file")
    p.add_argument("--verbose", action="store_true")
    args = p.parse_args(argv)

    ids = read_ids(args.input, args.column, args.ids)
    if args.limit:
        ids = ids[: args.limit]
    tool = "bulk_get_companies" if args.companies else "bulk_get_people"
    _confirm(f"mcp:{tool}", len(ids), args.yes)

    key = "domain" if args.companies else "id"
    out_path = Path(args.out) if args.out else Path(
        (args.csv or (args.input if args.input and args.input != "-" else "firmable")) + ".profiles.jsonl")
    run_chunked(ids, tool, lambda b: {"items": [{key if not i.startswith(("f0", "fp")) else "id": i} for i in b]},
                out_path, args.rps, args.concurrency, resume=not args.force, verbose=args.verbose)
    sys.stderr.write(f"profiles: raw records in {out_path}\n")


PHONE_COLUMNS = ["person_id", "mobile", "mobile_dnd", "is_mobile", "verified", "_status"]


def cmd_phones(argv: list[str]) -> None:
    p = argparse.ArgumentParser(
        prog="firmable phones",
        description="Mobile numbers for many people. COSTS 1 CREDIT PER PERSON, and the "
                    "underlying call is ATOMIC — a chunk larger than your balance is rejected "
                    "whole. Needs --yes. Consider `firmable profiles` instead: same price, but "
                    "it returns the profile and email too.",
    )
    p.add_argument("input", nargs="?", help="CSV/text file of person ids (- for stdin)")
    p.add_argument("--ids", help="Comma-separated ids instead of a file")
    p.add_argument("--column", help="CSV column holding the person id")
    p.add_argument("--csv", help="Write a flattened CSV here")
    p.add_argument("--out", help="JSONL output path")
    p.add_argument("--rps", type=float, default=DEFAULT_RPS)
    p.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    p.add_argument("--limit", type=int, help="Only take the first N ids — bound the spend")
    p.add_argument("--yes", action="store_true", help="Confirm the credit spend")
    p.add_argument("--force", action="store_true", help="Ignore the resume file")
    p.add_argument("--verbose", action="store_true")
    args = p.parse_args(argv)

    ids = read_ids(args.input, args.column, args.ids)
    if args.limit:
        ids = ids[: args.limit]
    _confirm("mcp:bulk_reveal_person_phones", len(ids), args.yes)

    out_path = Path(args.out) if args.out else Path(
        (args.csv or (args.input if args.input and args.input != "-" else "firmable")) + ".phones.jsonl")
    run_chunked(ids, "bulk_reveal_person_phones", lambda b: {"ids": b},
                out_path, args.rps, args.concurrency, resume=not args.force, verbose=args.verbose)

    if args.csv:
        rows = []
        for line in out_path.open(encoding="utf-8"):
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            if not rec.get("_ok"):
                for i in rec.get("_ids", []):
                    rows.append({"person_id": i, "_status": "error"})
                continue
            data = rec.get("data")
            people = data if isinstance(data, list) else (data or {}).get("records") or []
            for person in people:
                if not isinstance(person, dict):
                    continue
                phones = person.get("phones") or []
                clean = [x.get("value") for x in phones if isinstance(x, dict) and not x.get("is_dnd")]
                dnd = [x.get("value") for x in phones if isinstance(x, dict) and x.get("is_dnd")]
                rows.append({
                    "person_id": _j(person.get("id")),
                    "mobile": _j(clean),
                    "mobile_dnd": _j(dnd),
                    "is_mobile": _j([x.get("is_mobile") for x in phones if isinstance(x, dict)]),
                    "verified": _j([x.get("is_verified") for x in phones if isinstance(x, dict)]),
                    "_status": "hit" if phones else "no phone",
                })
        write_csv(args.csv, rows, PHONE_COLUMNS)
