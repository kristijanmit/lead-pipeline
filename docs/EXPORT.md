# AGENCY lead export

## 1. Overview

Two sinks, both satisfying the `Sink` protocol (ARCHITECTURE.md §7):

```python
class Sink(Protocol):
    def write(self, leads: list[Lead]) -> None: ...
```

| Sink | Module | Registered as | Used via |
| --- | --- | --- | --- |
| `CsvSink` | `pipeline/sinks/csv_sink.py` | not registered — called directly | always, every `score` run |
| `NotionSink` | `pipeline/sinks/notion_sink.py` | `@register("notion")` | `runner.py sync --sink notion` |

## 2. CsvSink — the always-on human view

`runner.py` instantiates `CsvSink` directly (not through the registry —
it isn't selected by `--sink`, it just always runs) at the end of the
`score` stage, writing `scored.csv` alongside `scored.jsonl`. Fixed
column set (company, domain, industry, location, country, source,
contact info joined with `; `, lead_type, qualified, the three scores,
status, errors). It exists for pasting into Sheets or eyeballing a batch
— **it is a view of the data, never the internal format**; nothing reads
it back in. If a scoring or schema field needs to show up here, add a
column in `_COLUMNS` and the matching row value — that's the entire
surface area.

## 3. NotionSink — the outreach tracker sink

`runner.py sync --run <run_id> --sink notion` reads `scored.jsonl` and
pushes qualified, unsynced leads into the AGENCY leads Notion database.

**Field mapping**: the full `Lead` → Notion property table, including
which fields are flattened, joined, or omitted, lives in one place —
`docs/ARCHITECTURE.md` §6.1 — and the code enforces the "one place" part:
every property comes out of a single function, `to_notion_properties(lead)
-> dict`. This doc doesn't repeat that table; it covers everything
*around* that function instead.

One convention worth calling out here since §6.1's table doesn't state it
explicitly: fields are **omitted, not sent blank**, whenever a value is
`None` or empty — `HTTPS`/`Mobile Friendly` only appear once the audit
enricher actually determined them, `Reachability Score`/`Opportunity
Score` only appear once scoring computed them. A Notion checkbox can't
represent "unknown," so leaving the property out entirely is how "we
don't know yet" is distinguished from "false."

### 3.1 Idempotency: `seen_domains.json`

Never queried live per lead (ARCHITECTURE.md §9) — a local cache file
(path from `notion.cache_path` in `config.yaml`) tracks what's already in
Notion. Two lookup paths, checked in order for each lead:

1. **Cache key match** — `cache_key(lead)` is the string form of
   `dedupe.lead_key`: `"domain:example.com"` or, when there's no domain,
   `"company:acme dental"`. A direct hit means this exact identity is
   already synced.
2. **Email index match** — `_email_index(seen)` builds a lowercased
   `email -> cache_key` map from every cache entry's stored snapshot. This
   catches the case a domain-only key would miss: the same business
   collected once *before* its domain was known (via `manual` or an early
   OSM pass with no `website` tag) and again later once enrichment found
   it — same email, different cache key, still one business, still one
   Notion page.

A match either way is treated as **cached**, not created: no new page.
But if the matched lead carries information the existing page's snapshot
doesn't have — a new email, a new phone, a new social link, computed by
`_new_info` — that gets posted as a **Notion comment** on the existing
page rather than silently dropped, and the snapshot is updated to include
it. This is what keeps a second sync pass useful instead of a no-op once
a lead is already in the tracker.

The cache entry (`page_id`, `synced_at`, and the `snapshot` — emails,
phones, socials, domain, company) is written **after each successful
create or update**, not batched at the end — a crash mid-run leaves the
cache accurate for everything that did land, so a re-run only touches
what's left.

### 3.2 `refresh_cache()`

`runner.py sync --sink notion --refresh-cache` fully replaces
`seen_domains.json` by paging through the live Notion database
(`_snapshot_from_properties` is the reverse of `to_notion_properties` for
the fields the cache needs) rather than trusting the local file. Run it
when the two might have drifted — e.g. after manually editing or merging
pages in Notion directly. It is not run automatically on every sync,
because querying the full database per invocation is exactly the
per-lead-live-query cost the cache exists to avoid (ARCHITECTURE.md §9);
it's a deliberate, occasional reconciliation step instead.

### 3.3 Failure handling

- Per-lead API failures (page creation, or a comment post on a cache hit)
  are collected into `SyncReport.failed` as `(lead, reason)` pairs — the
  batch keeps going.
- A `401`/`403` response raises `NotionAuthError` immediately and halts
  the whole batch — a bad token or a disconnected integration means *no*
  lead in the batch can possibly succeed, so there's nothing to gain by
  continuing to try.
- `429` or `5xx` responses get exactly one retry, honoring the `Retry-After`
  header if present (`_post`).
- `--limit N` caps **attempts**, not successes (`report.pending` counts
  what was skipped as a result) — this matters for a dry run against a
  half-broken setup: it must fail fast on the first couple of leads, not
  silently burn through the entire batch one failure at a time before the
  operator notices something's wrong.

### 3.4 One-time setup

Covered briefly in `README.md`'s Notion setup section — not repeated here.
Both the integration token and the database id are secrets and live in the
gitignored `.env` (`NOTION_TOKEN`, `NOTION_DATABASE_ID`); `config.yaml`'s
`notion.database_id` stays empty and is only a fallback if you'd rather
not use `.env` for the id specifically — `NotionSink.__init__` checks
`config.yaml` first, then falls back to the env var.

## 4. Adding a new sink

1. Create `pipeline/sinks/<name>.py`.
2. Write a class whose `write(self, leads: list[Lead]) -> None` (or, if it
   needs to report per-lead outcomes like `NotionSink` does, a similar
   `write(self, leads, limit=None) -> SomeReport` shape) satisfies the
   `Sink` protocol, and decorate it `@register("<name>")`.
3. Wire it into `runner.py`'s `--sink` handling so `cmd_sync` can select
   it via `get_sink("<name>", config)` — `CsvSink` is the one exception
   that bypasses the registry because it always runs regardless of
   `--sink`.
4. Add a stage-level test in the shape of `tests/test_sync_stage.py` /
   `tests/test_notion_sink.py`: no live API calls — stub the HTTP session
   the sink is given (`NotionSink.__init__` already accepts an injected
   `session` for exactly this reason) and assert on the request payloads
   and the resulting `SyncReport`.
