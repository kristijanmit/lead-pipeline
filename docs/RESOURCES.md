# AGENCY lead pipeline — resources

Open-source projects discussed for this build, organized by whether they're
actually part of the roadmap or just evaluated and set aside. Give Claude Code
this file alongside ARCHITECTURE.md and ROADMAP.md so it knows what to reach
for instead of writing something from scratch that already exists.

## Adopted — part of the current roadmap

### GoogleChrome/lighthouse

https://github.com/GoogleChrome/lighthouse
Official open-source engine behind the PageSpeed Insights API — install via
`npm install -g lighthouse`, run locally against any URL, no API key, no
quota. Returns performance, accessibility, SEO, and best-practices scores in
one JSON output.
**Used in**: Phase 2 (`pipeline/enrichers/audit.py`) — this is the whole
website-audit step, not a supplement to it.

### gosom/google-maps-scraper

https://github.com/gosom/google-maps-scraper
Go-based, self-hosted (Docker) scraper for Google Maps business listings —
name, address, phone, website, rating, reviews. CLI, API, and web UI modes.
**Used in**: Phase 1 (`pipeline/collectors/maps.py`) — supplemental to the
OSM collector for areas/categories where OpenStreetMap data is thin.
**Caveat**: scraping Google Maps sits in a ToS gray area (noted in
ROADMAP.md's risks section) — use sparingly, not as the primary source.

## Considered, not currently adopted

### reacherhq/check-if-email-exists

https://github.com/reacherhq/check-if-email-exists
Rust-based SMTP-level email verifier — checks MX records and opens a real
SMTP handshake to ask "would you accept mail for this address" without
sending anything. Self-hostable via Docker.
**Why it's not in the roadmap**: needs outbound port 25 open, which most
home/ISP connections block — would require a small VPS (~$5/mo), the one
paid dependency this project was built to avoid. Worth revisiting if AGENCY's
budget changes, or if Claude Code's own environment turns out to allow
outbound SMTP.

### ozhehkovski/geoleadscraper

https://github.com/ozhehkovski/geoleadscraper
MIT-licensed Chrome extension that scrapes whatever Google Maps page you're
currently looking at — no code, no CLI.
**Why it's not in the roadmap**: this project is being built as a scripted,
runnable pipeline, not a manual browser tool. Worth keeping in mind if lead
sourcing is ever handed to someone non-technical, since it needs no setup.

### n8n

https://github.com/n8n-io/n8n
Open-source, self-hostable workflow automation platform with native
Notion/Google Sheets/HTTP nodes, visual builder, scheduling, and retry logic
built in.
**Why it's not in the roadmap**: the current architecture (ARCHITECTURE.md)
deliberately stays at "scripts plus JSONL" — the right amount of structure
for one operator. n8n is the documented graduation path (ARCHITECTURE.md
§14) if this pipeline ever needs unattended scheduling or gets too large for
one person to run by hand — not a fit today.

## Not a repo, but worth knowing about

### OpenStreetMap Overpass API

https://overpass-api.de (public API, no repo to clone)
Free, no-key, no-signup query API against OpenStreetMap's business/POI data.
**Used in**: Phase 1 (`pipeline/collectors/osm.py`) — the default, ToS-clean
collection source. Not something to adopt code from; just a shared public
service to query respectfully (see ROADMAP.md's rate-limit note).

### Notion API

https://developers.notion.com (public API, free internal integrations)
**Used in**: Phase 4 (`pipeline/sinks/notion_sink.py`). One-time setup:
create an internal integration at notion.so/my-integrations, connect it to
the leads database (`•••` → Connections on the database page), and put the
token in the gitignored `.env` as `NOTION_TOKEN`. Free tier rate limit is
~3 requests/second — `notion.delay_s` in `config.yaml` stays under it.
