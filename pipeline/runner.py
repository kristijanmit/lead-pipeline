"""CLI entrypoint — wires config, collectors, and sinks together.

Stages themselves are functions of lists of leads (docs/ARCHITECTURE.md §3);
this module only handles argument parsing, the run directory, and the
manifest.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import yaml

from pipeline.collectors import get_collector
from pipeline.collectors.areas import parse_bbox, resolve_bbox
from pipeline.collectors.maps import RAW_CSV_NAME, grid_cell_count
from pipeline.config import ConfigError, load_config
from pipeline.dedupe import dedupe_leads, is_social_domain, lead_key, normalize_domain
from pipeline.drafting import DraftBudgetError, draft_lead, examples_path, language_skip_reason, load_examples
from pipeline.drafting.drafter import INVALID_PREFIX
from pipeline.enrichers import apply_enrichers, get_enricher
from pipeline.enrichers.audit import apply_audit
from pipeline.market import Market, load_market, market_from_prompts, market_yaml
from pipeline.niche import Niche, check_sources, load_niche_file
from pipeline.reports import build_report, findings, markdown_to_html, skip_reason
from pipeline.reports.audit_report import positives
from pipeline.schema import read_jsonl, write_jsonl
from pipeline.scoring import score
from pipeline.sinks import get_sink
from pipeline.sinks.csv_sink import CsvSink
from pipeline.sinks.notion_sink import (
    NotionAuthError, _email_index, _rich_text, _select_name, _text, cache_key, lead_from_page, load_rejected,
    load_seen, match_cache_key, save_seen,
)

_TIER_ORDER = ("A", "B", "C", "D")

# find a missing website first, then contact data, then the website audit
_ENRICH_STEPS = ("discovery", "contact", "audit")

RUNS_DIR = Path("data/runs")
BATCHES_DIR = Path("data/batches")

# exit code for "out of AI credits / usage limit": drafting stopped, the run's
# collect/enrich/score/sync are done and the undrafted pages wait for draft-notion
EXIT_OUT_OF_CREDITS = 3


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class _DraftMarkets:
    """The market each lead is drafted under (channel rules, language), loaded once
    per id. A lead or page without a market is skipped, never guessed."""

    def __init__(self, config: dict):
        self.config = config
        self.loaded: dict[str, Market | str] = {}

    def resolve(self, lead) -> tuple[Market | None, str]:
        """(market, skip reason). A reason means: don't draft this lead now."""
        if not lead.market:
            return None, "no market (set Market on the page, or score a run collected with one)"
        if lead.market not in self.loaded:
            try:
                self.loaded[lead.market] = load_market(self.config, lead.market)
            except ValueError as e:
                self.loaded[lead.market] = str(e)
        market = self.loaded[lead.market]
        if isinstance(market, str):
            return None, market
        return market, language_skip_reason(market)


class _Examples:
    """Examples file per drafting language (outreach.examples_path with {language}),
    loaded once. A missing file is a warning, not an error: drafts run without examples."""

    def __init__(self, outreach: dict):
        self.outreach = outreach
        self.loaded: dict[str, str] = {}

    def get(self, language: str) -> str:
        if language not in self.loaded:
            path = examples_path(self.outreach, language)
            self.loaded[language] = load_examples(path)
            if not self.loaded[language]:
                print(f"  warning: no examples file {path}; drafting {language} without examples", file=sys.stderr)
        return self.loaded[language]


def _print_market_skips(skips: dict[str, int]) -> None:
    for reason, count in skips.items():
        print(f"  {count} skipped, not drafted: {reason}")


def _has_no_contact_info(lead) -> bool:
    """No email, phone, social link, or website — nothing to outreach through."""
    return not (
        lead.contact_emails or lead.contact_phones or lead.social_links or lead.domain
    )


def _run_market(args: argparse.Namespace, config: dict) -> Market:
    """The run's market: a Market object from `go` / batch, else the --market preset."""
    market = getattr(args, "market_def", None)
    if market is not None:
        return market
    if getattr(args, "market", None):
        return load_market(config, args.market)
    raise ValueError("no market: run go, or pass --market <id> (a preset under markets: in config.yaml)")


def _run_niche(args: argparse.Namespace) -> Niche:
    """The run's niche: a Niche object from `go` / batch, else --niche-file. No default."""
    niche = getattr(args, "niche_def", None)
    if niche is not None:
        return niche
    if getattr(args, "niche_file", None):
        return load_niche_file(args.niche_file)
    raise ValueError("no niche: run go, or pass --niche-file")


def cmd_collect(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    market = _run_market(args, config)
    niche = _run_niche(args)
    sources = [s.strip() for s in args.source.split(",") if s.strip()]
    check_sources(niche, sources)
    params = {
        "location": args.location or config["collect"]["location"],
        "input": args.input,
        "bbox": getattr(args, "bbox", None),
        "grid": not getattr(args, "no_grid", False),
    }

    def source_params(source: str) -> dict:
        # Maps searches free text, OSM needs tags; the manual CSV ignores both.
        # Market-driven settings: Maps -lang, OSM name:en for English markets.
        # raw_dir: where Maps keeps gosom's untouched results CSV.
        terms = niche.osm_tags if source == "osm" else niche.search_terms
        return {
            **params,
            "categories": list(terms),
            "maps_language": market.maps_language,
            "prefer_name_en": market.language == "en",
            "raw_dir": str(RUNS_DIR / run_id),
        }

    # instantiate all collectors up front so a typo'd source name or missing
    # binary fails before any collection effort is spent
    collectors = {source: get_collector(source, config) for source in sources}
    run_id = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    started_at = _now_iso()

    raw_leads: list = []
    raw_by_source: dict[str, int] = {}
    errors_by_source: dict[str, str] = {}
    for source, collector in collectors.items():
        # a single source's operational failure (timeout, dead binary, ...)
        # shouldn't sink sources listed alongside it as a fallback/supplement
        # (e.g. --source osm,maps when Overpass is down) — collect what we
        # can and record the rest; ValueError (misconfiguration) still
        # propagates since no other source can compensate for that
        try:
            collected = list(collector.collect(source_params(source)))
        except RuntimeError as e:
            errors_by_source[source] = str(e)
            raw_by_source[source] = 0
            print(f"  warning: {source} collector failed: {e}", file=sys.stderr)
            continue
        raw_by_source[source] = len(collected)
        raw_leads.extend(collected)
    if not raw_leads and errors_by_source:
        details = "; ".join(f"{s}: {e}" for s, e in errors_by_source.items())
        raise RuntimeError(f"all collectors failed: {details}")
    deduped = dedupe_leads(raw_leads)
    # leads already in Notion would be enriched and drafted for nothing —
    # draft fields are only written when a page is created — so drop them
    # here, by the same cache sync checks (email matches need enrichment, so
    # sync still catches those)
    # leads the L0 gates rejected in an earlier run would only be rejected
    # again; --include-seen keeps both kinds
    already_in_notion = already_rejected = 0
    if not getattr(args, "include_seen", False):
        seen = load_seen(config["notion"]["cache_path"])
        rejected = load_rejected(config["collect"]["rejected_path"])
        fresh = [lead for lead in deduped if cache_key(lead) not in seen]
        already_in_notion = len(deduped) - len(fresh)
        deduped = [lead for lead in fresh if cache_key(lead) not in rejected]
        already_rejected = len(fresh) - len(deduped)
    leads = deduped if args.limit is None else deduped[: args.limit]
    # new leads per category — dedupe keeps the first occurrence, so a category
    # listed later only shows what earlier ones didn't already find
    new_by_category: dict[str, int] = {}
    for lead in leads:
        new_by_category[lead.industry] = new_by_category.get(lead.industry, 0) + 1
    for lead in leads:
        lead.run_id = run_id
        # a collector that set its own location (the manual CSV's optional
        # "location" column) wins; market and niche always come from the run
        if not lead.location:
            lead.location = params["location"]
        lead.country = market.country
        lead.market = market.id
        lead.language = market.language
        lead.industry = lead.niche = niche.name
        lead.niche_label = niche.phrase

    args.run = run_id  # lets `run` (collect+enrich+score+sync chained) find it

    run_dir = RUNS_DIR / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    write_jsonl(run_dir / "collected.jsonl", leads)
    CsvSink(run_dir / "collected.csv").write(leads)

    manifest = {
        "run_id": run_id,
        "stage": "collect",
        "market": market.to_dict(),
        "niche": niche.to_dict(),
        "config_snapshot": {
            "sources": sources,
            "params": {**params, "search_terms": niche.search_terms, "osm_tags": niche.osm_tags},
            "limit": args.limit,
            "overpass": config["overpass"],
        },
        "counts": {
            "raw": len(raw_leads),
            "raw_by_source": raw_by_source,
            "already_in_notion": already_in_notion,
            "already_rejected": already_rejected,
            "new_by_category": new_by_category,
            "after_dedupe": len(deduped),
            "written": len(leads),
        },
        "errors_by_source": errors_by_source,
        "started_at": started_at,
        "finished_at": _now_iso(),
    }
    (run_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    with_domain = sum(1 for lead in leads if lead.domain)
    source_summary = ", ".join(f"{s}: {n}" for s, n in raw_by_source.items())
    print(f"run {run_id}: {len(raw_leads)} raw ({source_summary}) -> {len(leads)} written")
    print(f"  market {market.id} ({market.country}, {market.language}), niche {niche.name!r}")
    if errors_by_source:
        print(f"  {len(errors_by_source)} source(s) failed: {', '.join(errors_by_source)} (see manifest.json)")
    if already_in_notion:
        print(f"  {already_in_notion} already in Notion (cache) — skipped")
    if already_rejected:
        print(f"  {already_rejected} rejected in an earlier run (rejected_domains.json) — skipped")
    if len(new_by_category) > 1:
        breakdown = ", ".join(f"{c} {n}" for c, n in new_by_category.items())
        print(f"  new per search term: {breakdown}")
    print(f"  {with_domain} with a website, {len(leads) - with_domain} without")
    raw_note = f", {RAW_CSV_NAME}" if (run_dir / RAW_CSV_NAME).exists() else ""
    print(f"  {run_dir}/collected.jsonl (+ collected.csv, manifest.json{raw_note})")
    return 0


def _stage_keys(lead) -> set[tuple[str, str]]:
    """Keys a stage's output lead is matched by on re-runs: its own, plus the
    company key — discovery gives a lead a domain after collect, so the
    collected lead (still keyed by company) must find it."""
    return {lead_key(lead), ("company", lead.company.strip().lower())}


def _audit_outcome(before, after) -> str:
    """"unreachable" | "lighthouse_failed" | "" for one audited lead."""
    if after.site_reachable is False:
        return "unreachable"
    new_errors = after.errors[len(before.errors):]
    if any(e.startswith("audit: ") for e in new_errors):
        return "lighthouse_failed"
    return ""


# steps `enrich --only <step> --rescan` can re-run on an enriched run
_RESCAN_STEPS = ("audit",)


def _cmd_rescan(args: argparse.Namespace, config: dict) -> int:
    """Re-run one enrich step (the audit) on leads already in enriched.jsonl —
    contact data is kept, the audit fields are replaced. For runs enriched
    before schema v11, which have no signals. Lighthouse scores are kept when
    Lighthouse fails this time."""
    run_dir = RUNS_DIR / args.run
    enriched_path = run_dir / "enriched.jsonl"
    if not enriched_path.exists():
        raise ValueError(f"no enriched.jsonl in {run_dir} — run enrich first")
    leads = read_jsonl(enriched_path)
    enricher = get_enricher(args.only, config)
    enricher.keep_lighthouse = True
    delay_s = config["enrich"]["delay_s"]
    started_at = _now_iso()

    results = []
    rescanned = unreachable = lighthouse_failed = not_audited = pending = 0
    for lead in leads:
        if not lead.domain or is_social_domain(lead.domain):
            not_audited += 1
            results.append(lead)
            continue
        if args.limit is not None and rescanned >= args.limit:
            pending += 1
            results.append(lead)
            continue
        if rescanned and delay_s:
            time.sleep(delay_s)  # shared courtesy between sites (ARCHITECTURE §11)
        out = enricher.enrich(lead)
        rescanned += 1
        outcome = _audit_outcome(lead, out)
        unreachable += outcome == "unreachable"
        lighthouse_failed += outcome == "lighthouse_failed"
        print(f"  [{rescanned}] {lead.company} ({lead.domain}): {outcome or 'ok'}")
        results.append(out)
        write_jsonl(enriched_path, results + leads[len(results):])  # a crash keeps finished audits

    write_jsonl(enriched_path, results)
    CsvSink(run_dir / "enriched.csv").write(results)

    manifest_path = run_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {"run_id": args.run}
    manifest["enrich_rescan"] = {
        "step": args.only,
        "config_snapshot": {"enrich": config["enrich"], "limit": args.limit},
        "counts": {
            "input": len(leads), "rescanned": rescanned, "site_unreachable": unreachable,
            "lighthouse_failed": lighthouse_failed, "not_audited": not_audited, "pending": pending,
        },
        "started_at": started_at,
        "finished_at": _now_iso(),
    }
    manifest_path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(
        f"run {args.run}: {rescanned} re-audited ({unreachable} unreachable, "
        f"{lighthouse_failed} Lighthouse failed), {not_audited} without a site, {pending} not reached (--limit)"
    )
    print(f"  {enriched_path} (+ enriched.csv, manifest.json)")
    if (run_dir / "scored.jsonl").exists():
        print(f"  scored.jsonl is now older than the audit: run `score --run {args.run}` again")
    return 0


def cmd_enrich(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    only, rescan = getattr(args, "only", None), getattr(args, "rescan", False)
    if only or rescan:
        if not (only and rescan):
            raise ValueError("--only and --rescan go together: `enrich --run <id> --only audit --rescan`")
        if only not in _RESCAN_STEPS:
            raise ValueError(f"--only {only!r}: only {', '.join(_RESCAN_STEPS)} can be re-run")
        return _cmd_rescan(args, config)
    run_dir = RUNS_DIR / args.run
    collected_path = run_dir / "collected.jsonl"
    if not collected_path.exists():
        raise ValueError(f"no collected.jsonl in {run_dir} — run collect first")
    leads = read_jsonl(collected_path)

    # idempotent re-runs: leads already enriched in a previous (partial) run
    # are carried over untouched — only status "enriched" counts as done
    enriched_path = run_dir / "enriched.jsonl"
    previous = {}
    if enriched_path.exists():
        previous = {
            key: lead
            for lead in read_jsonl(enriched_path)
            if lead.status == "enriched"
            for key in _stage_keys(lead)
        }

    # leads with no email/phone/social/website are nothing to outreach
    # through — filtered out entirely, but tracked in filtered.jsonl (not
    # silently dropped, per manifest.json being the only observability
    # here) and, once known, excluded on re-runs same as "enriched" above
    filtered_path = run_dir / "filtered.jsonl"
    previous_filtered = {}
    if filtered_path.exists():
        previous_filtered = {
            key: lead for lead in read_jsonl(filtered_path) for key in _stage_keys(lead)
        }

    enrichers = [(name, get_enricher(name, config)) for name in _ENRICH_STEPS]
    delay_s = config["enrich"]["delay_s"]
    started_at = _now_iso()

    results = []
    newly_filtered = []
    processed = skipped = already_filtered = failed = filtered_count = 0
    unreachable = lighthouse_failed = 0
    for lead in leads:
        key = lead_key(lead)
        company_key = ("company", lead.company.strip().lower())
        done = previous.get(key) or previous.get(company_key)
        if done is not None:
            results.append(done)
            skipped += 1
            continue
        if key in previous_filtered or company_key in previous_filtered:
            already_filtered += 1
            continue
        if args.limit is not None and processed >= args.limit:
            results.append(lead)  # still pending — a re-run picks it up
            continue
        if processed and delay_s:
            time.sleep(delay_s)  # shared courtesy between sites (ARCHITECTURE §11)
        enriched = apply_enrichers(lead, enrichers, retry_delay_s=delay_s)
        processed += 1
        if enriched.status == "enrich_failed":
            failed += 1
        outcome = _audit_outcome(lead, enriched)
        unreachable += outcome == "unreachable"
        lighthouse_failed += outcome == "lighthouse_failed"
        no_contact = _has_no_contact_info(enriched)
        site = lead.domain or "no website"
        if enriched.domain != lead.domain:
            site = f"site found: {enriched.domain}"
        print(
            f"  [{processed}] {lead.company} "
            f"({site}): "
            f"{'filtered (no contact info)' if no_contact else enriched.status}"
        )
        if no_contact:
            newly_filtered.append(enriched)
            filtered_count += 1
        else:
            results.append(enriched)

    write_jsonl(enriched_path, results)
    CsvSink(run_dir / "enriched.csv").write(results)
    write_jsonl(filtered_path, [*previous_filtered.values(), *newly_filtered])

    # collect wrote the manifest flat; enrich adds its own section so the
    # run keeps both stages' snapshots (§12 — the only observability here)
    manifest_path = run_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {"run_id": args.run}
    manifest["stage"] = "enrich"
    manifest["enrich"] = {
        "config_snapshot": {"enrich": config["enrich"], "limit": args.limit},
        "counts": {
            "input": len(leads),
            "already_enriched": skipped,
            "already_filtered": already_filtered,
            "processed": processed,
            "failed": failed,
            "filtered_no_contact": filtered_count,
            "site_unreachable": unreachable,
            "lighthouse_failed": lighthouse_failed,
            "pending": len(leads) - skipped - already_filtered - processed,
        },
        "started_at": started_at,
        "finished_at": _now_iso(),
    }
    manifest_path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    pending = len(leads) - skipped - already_filtered - processed
    print(
        f"run {args.run}: {processed} enriched ({failed} failed, {filtered_count} filtered — no contact info), "
        f"{skipped} already done, {already_filtered} already filtered, {pending} pending"
    )
    if unreachable or lighthouse_failed:
        print(f"  audit: {unreachable} site(s) unreachable, {lighthouse_failed} Lighthouse failure(s) (see errors)")
    print(f"  {run_dir}/enriched.jsonl (+ enriched.csv, manifest.json)")
    if filtered_count or already_filtered:
        print(
            f"  {run_dir}/filtered.jsonl: "
            f"{len(previous_filtered) + filtered_count} lead(s) with no email/phone/social/website"
        )
    return 0


def _read_manifest(run_dir: Path) -> dict:
    path = run_dir / "manifest.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def _score_market(args: argparse.Namespace, config: dict, run_dir: Path) -> Market:
    """The country gate is the run's market country, read from the manifest collect
    wrote. --market is only for runs from before markets existed (no market block)."""
    block = _read_manifest(run_dir).get("market")
    requested = getattr(args, "market", None)
    if block:
        market = Market.from_dict(block.get("id", ""), block)
        if requested and requested.strip().lower() != market.id:
            raise ValueError(
                f"run {args.run} was collected for market {market.id!r}, not {requested!r}"
            )
        return market
    if requested:
        return load_market(config, requested)
    raise ValueError(
        f"run {args.run} has no market in manifest.json (collected before markets existed) — "
        "pass --market <id>"
    )


def _score_niche(run_dir: Path) -> Niche | None:
    """The run's niche from the manifest. Runs from before niches existed have none:
    every lead's niche match is then "unknown" (no effect on the score)."""
    block = _read_manifest(run_dir).get("niche")
    return Niche.from_dict(block) if block else None


def _update_rejected(path: str, results: list, run_id: str) -> tuple[int, int]:
    """Disqualified keys -> rejected_domains.json (collect skips them); keys of this
    run's leads that now pass the gates come off it. Returns (added, removed)."""
    rejected = load_rejected(path)
    added = removed = 0
    for lead in results:
        key = cache_key(lead)
        if lead.status == "disqualified":
            added += key not in rejected
            rejected[key] = {"reason": lead.disqualify_reason, "run_id": run_id, "rejected_at": _now_iso()}
        elif lead.status == "scored" and key in rejected:
            del rejected[key]
            removed += 1
    if added or removed or Path(path).exists():
        save_seen(path, rejected)
    return added, removed


def cmd_score(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    run_dir = RUNS_DIR / args.run
    enriched_path = run_dir / "enriched.jsonl"
    if not enriched_path.exists():
        raise ValueError(f"no enriched.jsonl in {run_dir} — run enrich first")
    leads = read_jsonl(enriched_path)

    # scored.jsonl carries forward previously scored rows for pass-through
    # cases below (not yet enriched, or beyond --limit)
    scored_path = run_dir / "scored.jsonl"
    previous = {}
    if scored_path.exists():
        previous = {lead_key(lead): lead for lead in read_jsonl(scored_path)}

    scoring = config["scoring"]
    market = _score_market(args, config, run_dir)
    niche = _score_niche(run_dir)
    year = datetime.now().year
    started_at = _now_iso()

    results = []
    newly_scored = []
    scored_count = pending = 0
    for lead in leads:
        # a lead enrichment never reached (status still "collected" after a
        # limited enrich run) has a domain but no audit data — scoring it
        # would fabricate a "site didn't load" signal for a site nobody
        # tried. Pass it through unscored; a full enrich run picks it up.
        if lead.status not in ("enriched", "enrich_failed"):
            results.append(previous.get(lead_key(lead), lead))
            pending += 1
            continue
        # --limit dry-runs must not truncate scored.jsonl: rows beyond the
        # limit keep their previous scored values rather than being dropped
        if args.limit is not None and scored_count >= args.limit:
            results.append(previous.get(lead_key(lead), lead))
            continue
        scored = score(lead, scoring, market, niche, year)
        results.append(scored)
        newly_scored.append(scored)
        scored_count += 1

    write_jsonl(scored_path, results)
    CsvSink(run_dir / "scored.csv").write(results)
    rejected_added, rejected_removed = _update_rejected(
        config["collect"]["rejected_path"], newly_scored, args.run
    )

    carried = len(leads) - scored_count - pending
    by_type: dict[str, int] = {}
    tier_counts = {t: 0 for t in ("A", "B", "C", "D")}
    disqualify_reasons: dict[str, int] = {}
    niche_counts: dict[str, int] = {}
    for lead in results:
        if lead.lead_type is not None:
            by_type[lead.lead_type] = by_type.get(lead.lead_type, 0) + 1
        if lead.tier in tier_counts:
            tier_counts[lead.tier] += 1
        if lead.disqualify_reason:
            kind = lead.disqualify_reason.split(":")[0]  # "chain: 4 listings" -> "chain"
            disqualify_reasons[kind] = disqualify_reasons.get(kind, 0) + 1
        if lead.niche_match:
            niche_counts[lead.niche_match] = niche_counts.get(lead.niche_match, 0) + 1
    qualified_count = sum(1 for lead in results if lead.qualified)
    confidence_low = sum(1 for lead in results if lead.score_confidence == "low")

    manifest_path = run_dir / "manifest.json"
    manifest = _read_manifest(run_dir) or {"run_id": args.run}
    manifest["stage"] = "score"
    manifest["score"] = {
        "config_snapshot": {
            "scoring": scoring, "market": market.id, "niche": niche.name if niche else None,
            "year": year, "limit": args.limit,
        },
        "counts": {
            "input": len(leads),
            "scored": scored_count,
            "pending_enrichment": pending,
            "beyond_limit": carried,
            "by_lead_type": by_type,
            "qualified": qualified_count,
            "disqualified": sum(disqualify_reasons.values()),
            "disqualify_reasons": disqualify_reasons,
            "tier_counts": tier_counts,
            "confidence_low": confidence_low,
            "niche_match": niche_counts,
            "rejected_added": rejected_added,
            "rejected_removed": rejected_removed,
        },
        "started_at": started_at,
        "finished_at": _now_iso(),
    }
    manifest_path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    tiers = " ".join(f"{t}:{n}" for t, n in tier_counts.items())
    print(
        f"run {args.run}: {scored_count} scored (v{scoring['version']}, market {market.id}), "
        f"{qualified_count} qualified — tiers {tiers}"
    )
    if disqualify_reasons:
        print("  disqualified: " + ", ".join(f"{k} {n}" for k, n in sorted(disqualify_reasons.items())))
    if confidence_low:
        print(f"  {confidence_low} with low-confidence Need (capped at B, needs a manual look)")
    if pending:
        print(
            f"  warning: {pending} leads still pending enrichment were passed "
            f"through unscored — finish `enrich --run {args.run}` first"
        )
    if carried:
        print(f"  {carried} beyond --limit kept their previous scored values")
    ranked = [lead for lead in results if lead.priority is not None]
    for lead in sorted(ranked, key=lambda l: l.priority, reverse=True)[:5]:
        print(
            f"  {lead.tier} {lead.priority:5.1f}  {lead.company} "
            f"({lead.domain or 'no website'}, {lead.lead_type})"
        )
    print(f"  {run_dir}/scored.jsonl (+ scored.csv, manifest.json)")
    return 0


# an audit older than this gets a warning in audit-report (the report shows its date)
AUDIT_STALE_DAYS = 14


def _audit_date(manifest: dict) -> datetime | None:
    """When the run's stored audit data was taken: the last enrich, or the last
    `enrich --only audit --rescan` when it reached every lead (a --limit rescan
    leaves older audits, so it doesn't count)."""
    stamps = []
    enrich = manifest.get("enrich") or {}
    if enrich.get("finished_at"):
        stamps.append(enrich["finished_at"])
    rescan = manifest.get("enrich_rescan") or {}
    if rescan.get("finished_at") and not (rescan.get("counts") or {}).get("pending"):
        stamps.append(rescan["finished_at"])
    return datetime.fromisoformat(max(stamps)) if stamps else None


def _run_language(config: dict, manifest: dict) -> str:
    """The run's language for leads without one: the manifest's market block, else the
    market the run was scored under (`score --market` for runs collected before markets)."""
    block = manifest.get("market") or {}
    if block.get("language"):
        return block["language"]
    scored_under = ((manifest.get("score") or {}).get("config_snapshot") or {}).get("market")
    if scored_under:
        try:
            return load_market(config, scored_under).language
        except ValueError:
            return ""
    return ""


def cmd_audit_report(args: argparse.Namespace) -> int:
    """One-page audit report per lead (docs/OUTREACH.md §7): Markdown + self-contained
    HTML in data/runs/<id>/reports/, in the lead's market language. Stored audit data by
    default; --rescan re-audits with the shared audit core (report only, JSONL untouched).
    An existing .md is kept (it may hold hand edits) unless --force; the HTML is always
    rendered again from the .md."""
    config = load_config(args.config)
    run_dir = RUNS_DIR / args.run
    scored_path = run_dir / "scored.jsonl"
    if not scored_path.exists():
        raise ValueError(f"no scored.jsonl in {run_dir} — run score first")
    leads = read_jsonl(scored_path)
    manifest = _read_manifest(run_dir)
    run_language = _run_language(config, manifest)
    niche = _score_niche(run_dir)
    need_cfg = config["scoring"]["need"]
    overrides = niche.need_overrides if niche else None
    signature = config["outreach"]["signature"]
    now = datetime.now(timezone.utc)
    started_at = _now_iso()

    if args.domain:
        wanted = normalize_domain(args.domain)
        selected = [lead for lead in leads if lead.domain == wanted]
        if not selected:
            raise ValueError(f"no lead with domain {wanted!r} in {scored_path}")
    else:
        floor = _TIER_ORDER.index(args.min_tier)
        selected = [
            lead for lead in leads
            if lead.qualified and lead.tier in _TIER_ORDER and _TIER_ORDER.index(lead.tier) <= floor
        ]
        selected.sort(key=lambda lead: lead.priority or 0, reverse=True)

    audited_at = _audit_date(manifest)
    if not args.rescan:
        if audited_at is None:
            print("  warning: the run's manifest has no enrich date; the reports show today's date", file=sys.stderr)
        elif (now - audited_at).days > AUDIT_STALE_DAYS:
            print(
                f"  warning: the audit data is {(now - audited_at).days} days old (from {audited_at.date()}); "
                "add --rescan for a fresh audit",
                file=sys.stderr,
            )

    reports_dir = run_dir / "reports"
    enricher = get_enricher("audit", config) if args.rescan else None
    delay_s = config["enrich"]["delay_s"]
    written = kept = rescanned = 0
    skipped: dict[str, int] = {}
    for lead in selected:
        if args.limit is not None and written + kept >= args.limit:
            break
        if not lead.language and run_language:
            lead = replace(lead, language=run_language)  # runs from before step 1 didn't stamp it
        md_path = reports_dir / f"{lead.domain}.md"
        if lead.domain and md_path.exists() and not args.force:
            reports_dir.mkdir(parents=True, exist_ok=True)
            (reports_dir / f"{lead.domain}.html").write_text(
                markdown_to_html(md_path.read_text(encoding="utf-8"), lead.language or "en"), encoding="utf-8"
            )
            kept += 1
            print(f"  kept {md_path.name} (hand edits safe; --force to rebuild), HTML re-rendered")
            continue
        checked = (audited_at or now).date()
        if enricher and lead.domain and not is_social_domain(lead.domain):
            if rescanned and delay_s:
                time.sleep(delay_s)  # shared courtesy between sites (ARCHITECTURE §11)
            lead = apply_audit(lead, enricher.audit(lead.domain))
            rescanned += 1
            checked = now.date()
        reason = skip_reason(lead)
        found = [] if reason else findings(lead, need_cfg, checked.year, overrides)
        if not reason and not found:
            reason = "site looks fine"
        if reason:
            key = reason.split(" (")[0]
            skipped[key] = skipped.get(key, 0) + 1
            if args.domain:
                print(f"  {lead.company} ({lead.domain or 'no website'}): skipped, {reason}")
            continue
        md = build_report(lead, found, positives(lead, checked.year), checked, signature)
        reports_dir.mkdir(parents=True, exist_ok=True)
        md_path.write_text(md, encoding="utf-8")
        (reports_dir / f"{lead.domain}.html").write_text(markdown_to_html(md, lead.language), encoding="utf-8")
        written += 1
        print(f"  {lead.tier or '-'} {lead.company} ({lead.domain}, {lead.language}): {len(found)} findings")

    manifest = _read_manifest(run_dir) or {"run_id": args.run}
    manifest["audit_report"] = {
        "config_snapshot": {
            "domain": args.domain, "min_tier": args.min_tier, "limit": args.limit,
            "rescan": args.rescan, "force": args.force,
            "audit_date": audited_at.isoformat() if audited_at else None,
        },
        "counts": {
            "selected": len(selected), "written": written, "kept": kept, "rescanned": rescanned,
            "skipped": skipped,
        },
        "started_at": started_at,
        "finished_at": _now_iso(),
    }
    (run_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    skips = ", ".join(f"{k} {n}" for k, n in sorted(skipped.items()))
    print(
        f"run {args.run}: {written} reports written, {kept} kept"
        + (f", {rescanned} re-audited" if args.rescan else "")
        + (f"; skipped: {skips}" if skips else "")
    )
    if written or kept:
        print(f"  {reports_dir}/ (<domain>.md + .html; edit the .md, re-run to update the .html)")
    return 0


class _DraftTally:
    """Counts for one drafting pass (auto step or draft-notion)."""

    def __init__(self):
        self.drafted = self.failed = self.errored = self.no_contact = self.undrafted = 0
        self.skips: dict[str, int] = {}
        self.out_of_credits = ""

    def skip(self, reason: str) -> None:
        self.skips[reason] = self.skips.get(reason, 0) + 1

    def counts(self) -> dict:
        return {
            "drafted": self.drafted, "draft_failed": self.failed, "error": self.errored,
            "no_contact_data": self.no_contact, "skipped": sum(self.skips.values()),
            "undrafted_out_of_credits": self.undrafted,
        }

    def summary(self) -> str:
        return (
            f"{self.drafted} drafted, {self.failed} draft failed, {self.errored} errors (retried next time), "
            f"{self.no_contact} without contact data"
        )

    def report(self) -> None:
        _print_market_skips(self.skips)
        if self.out_of_credits:
            print(
                f"  drafting stopped, out of AI credits ({self.out_of_credits}): {self.undrafted} pages "
                "undrafted. Their Draft Message is empty, so `draft-notion` (or a later run) drafts them "
                "once credits are back.",
                file=sys.stderr,
            )


def _draft_page(sink, page: dict, lead, market: Market, ctx: dict, tally: _DraftTally, write: bool):
    """Draft one Notion page from `lead` and (when `write`) update that page. Never
    raises for a per-page problem (script_edits.md §11.4): a message that fails
    validation sets Status "Draft failed" with the reason in Draft Notes; a CLI or
    Notion error goes to Draft Notes / the report and leaves the page eligible.
    DraftBudgetError (out of credits) and NotionAuthError propagate. Returns the
    drafted lead."""
    props = page.get("properties", {})
    out = draft_lead(
        lead, ctx["outreach"], ctx["draft_cfg"], market, variant=tally.drafted + tally.failed,
        examples=ctx["examples"].get(market.language), niche=ctx.get("niche"), need_cfg=ctx["need_cfg"],
    )
    update: dict = {}
    if out.draft_message:
        tally.drafted += 1
        had_draft = bool(_rich_text(props.get("Draft Message")))
        verb = "overwrite" if had_draft else "write"
        print(f"  {verb if write else 'would ' + verb}: {lead.company} -> {out.suggested_channel}")
        print(f"    {out.draft_message}")
        if out.draft_notes:
            print(f"    verify before sending: {out.draft_notes}")
        update = {
            "Suggested Channel": {"select": {"name": out.suggested_channel}},
            "Draft Message": _text(out.draft_message[:2000]),
            # always sent: a redraft must not leave the old draft's note behind
            "Draft Notes": _text(out.draft_notes[:2000]) if out.draft_notes else {"rich_text": []},
        }
        if _select_name(props.get("Status")) in ("Not started", "", "Draft failed"):
            update["Status"] = {"select": {"name": "Queued"}}
    elif out.errors != lead.errors:
        error = out.errors[-1]
        if error.startswith(INVALID_PREFIX):
            tally.failed += 1
            print(f"  draft failed: {lead.company}: {error}")
            update = {
                "Status": {"select": {"name": "Draft failed"}},
                "Draft Notes": _text(f"Draft failed validation: {error[len(INVALID_PREFIX):]}"[:2000]),
            }
        else:
            tally.errored += 1
            print(f"  error: {lead.company}: {error}")
            update = {"Draft Notes": _text(f"Draft error, retried next run: {error}"[:2000])}
    else:
        tally.no_contact += 1
        print(f"  no contact data (add an Instagram link, phone or email): {lead.company}")
    if write and update:
        try:
            sink.update_page(page["id"], update)
        except NotionAuthError:
            raise
        except Exception as e:  # one page failing to save never stops the rest
            tally.errored += 1
            print(f"  error: {lead.company}: Notion update failed: {e}", file=sys.stderr)
    return out


def _draft_context(config: dict, niche: Niche | None = None) -> dict:
    return {
        "outreach": config["outreach"], "draft_cfg": config["draft"], "need_cfg": config["scoring"]["need"],
        "examples": _Examples(config["outreach"]), "niche": niche,
    }


def _with_page_fields(lead, page: dict):
    """The JSONL lead, plus what the operator may have set on the page since sync:
    SM Audit (first praise source) and a hand-picked Suggested Channel."""
    props = page.get("properties", {})
    return replace(
        lead,
        sm_audit=_rich_text(props.get("SM Audit")) or lead.sm_audit,
        suggested_channel=_select_name(props.get("Suggested Channel")) or lead.suggested_channel,
        qualified=True,
    )


def _auto_draft(args: argparse.Namespace) -> int:
    """The draft step at the end of run / batch / go (script_edits.md §11): this
    run's pages in Notion that are still draftable (query_draftable, Tier >=
    outreach.auto_draft_min_tier), drafted from the run's JSONL lead (it carries
    niche_label and profile_facts) and written to the page. No prompt, no approval.
    Per-page failures never stop it; running out of credits stops drafting, leaves
    the rest undrafted (draft-notion or a later run picks them up) and returns
    EXIT_OUT_OF_CREDITS. --limit caps the pages drafted."""
    config = load_config(args.config)
    outreach = config["outreach"]
    if not outreach["auto_draft"]:
        print("drafting: outreach.auto_draft is off — draft later with `draft-notion`")
        return 0
    run_dir = RUNS_DIR / args.run
    scored_path = run_dir / "scored.jsonl"
    leads = read_jsonl(scored_path)
    sink = get_sink(getattr(args, "sink", "notion") or "notion", config)
    seen = load_seen(sink.cache_path)
    index = _email_index(seen)
    by_page: dict[str, int] = {}
    for i, lead in enumerate(leads):
        if lead.status != "synced":
            continue
        key = match_cache_key(lead, seen, index)
        if key and seen[key].get("page_id"):
            by_page.setdefault(seen[key]["page_id"], i)
    started_at = _now_iso()
    tally = _DraftTally()
    manifest = _read_manifest(run_dir) or {"run_id": args.run}
    min_tier = outreach["auto_draft_min_tier"]
    if not by_page:
        print(f"run {args.run}: nothing to draft (no page synced in this run)")
        pages = []
    else:
        try:
            pages = sink.query_draftable(min_tier=min_tier, page_ids=set(by_page))
        except NotionAuthError:
            raise
        except Exception as e:  # Notion down: the pages stay draftable for draft-notion
            print(f"  drafting skipped, Notion query failed: {e} — run `draft-notion` later", file=sys.stderr)
            pages = []
    ctx = _draft_context(config, _score_niche(run_dir))
    markets = _DraftMarkets(config)
    limit = getattr(args, "limit", None)
    for n, page in enumerate(pages):
        if limit is not None and tally.drafted + tally.failed + tally.errored >= limit:
            break
        i = by_page[page["id"]]
        lead = _with_page_fields(leads[i], page)
        market, reason = markets.resolve(lead)
        if reason:
            tally.skip(reason)
            continue
        try:
            out = _draft_page(sink, page, lead, market, ctx, tally, write=True)
        except DraftBudgetError as e:
            tally.out_of_credits = str(e)
            tally.undrafted = len(pages) - n
            break
        leads[i] = replace(out, sm_audit=leads[i].sm_audit, qualified=leads[i].qualified)
    write_jsonl(scored_path, leads)
    CsvSink(run_dir / "scored.csv").write(leads)

    manifest["stage"] = "draft"
    manifest["draft"] = {
        "config_snapshot": {"draft": config["draft"], "min_tier": min_tier, "limit": limit},
        "counts": {"run_pages": len(by_page), "draftable": len(pages), **tally.counts()},
        "market_skip_reasons": tally.skips,
        "stopped_out_of_credits": bool(tally.out_of_credits),
        "started_at": started_at,
        "finished_at": _now_iso(),
    }
    (run_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(f"run {args.run} drafting (Tier {min_tier}+, {len(pages)} draftable pages): {tally.summary()}")
    tally.report()
    return EXIT_OUT_OF_CREDITS if tally.out_of_credits else 0


def _jsonl_by_page(sink) -> tuple[dict[str, object], dict[str, Niche | None]]:
    """page_id -> latest JSONL lead (via the sync cache, like --rescore), and run_id
    -> that run's niche. draft-notion drafts from these when it can, so pages a run
    left undrafted keep their profile_facts, niche_label and niche question."""
    seen = load_seen(sink.cache_path)
    index = _email_index(seen)
    rows: dict[str, object] = {}
    niches: dict[str, Niche | None] = {}
    for path in sorted(RUNS_DIR.glob("*/scored.jsonl")):  # run ids sort by time: later wins
        for lead in read_jsonl(path):
            if lead.status != "synced":
                continue
            key = match_cache_key(lead, seen, index)
            if key and seen[key].get("page_id"):
                rows[seen[key]["page_id"]] = lead
    for lead in rows.values():
        if lead.run_id not in niches:
            niches[lead.run_id] = _score_niche(RUNS_DIR / lead.run_id) if lead.run_id else None
    return rows, niches


def cmd_draft_notion(args: argparse.Namespace) -> int:
    """Catch-up drafting for Notion pages per script_edits.md §11.2: no draft yet,
    Qualified, Status Not started, Verdict not Skip, Tier empty or at least
    outreach.auto_draft_min_tier. SM Audit is optional (the first praise source).
    Pages without Market or Industry are skipped and reported. A page synced by a
    run is drafted from its JSONL row (profile_facts, niche); a hand-made page from
    the page itself. Writes when outreach.auto_draft is on (--dry-run previews);
    with auto_draft off it is a dry run unless --yes. --redraft also rewrites
    existing drafts. Per-page failures never stop it. --limit caps the pages drafted."""
    config = load_config(args.config)
    sink = get_sink("notion", config)
    outreach = config["outreach"]
    write = not getattr(args, "dry_run", False) and (outreach["auto_draft"] or getattr(args, "yes", False))

    redraft = getattr(args, "redraft", False)
    pages = sink.query_draftable(
        redraft=redraft, min_tier=outreach["auto_draft_min_tier"],
        company=getattr(args, "company", "") or "",
    )
    if not pages:
        print("nothing to draft: no page with Status Not started, an empty Draft Message, "
              f"Verdict not Skip and Tier {outreach['auto_draft_min_tier']} or better "
              "(--redraft rewrites existing drafts)")
        return 0
    rows, niches = _jsonl_by_page(sink)
    tally = _DraftTally()
    markets = _DraftMarkets(config)
    contexts: dict[str, dict] = {}
    for n, page in enumerate(pages):
        if args.limit is not None and tally.drafted + tally.failed + tally.errored + tally.no_contact >= args.limit:
            break
        props = page.get("properties", {})
        row = rows.get(page.get("id", ""))
        lead = _with_page_fields(row, page) if row is not None else lead_from_page(page)
        missing = [name for name in ("Market", "Industry") if not _select_name(props.get(name))]
        if missing:
            tally.skip("set Market and Industry on the page")
            print(f"  skipped: {lead.company}: no {' / '.join(missing)}")
            continue
        market, reason = markets.resolve(lead)
        if reason:
            tally.skip(reason)
            print(f"  skipped: {lead.company}: {reason}")
            continue
        run_id = row.run_id if row is not None else ""
        if run_id not in contexts:
            contexts[run_id] = _draft_context(config, niches.get(run_id))
        try:
            _draft_page(sink, page, lead, market, contexts[run_id], tally, write=write)
        except DraftBudgetError as e:
            tally.out_of_credits = str(e)
            tally.undrafted = len(pages) - n
            break
    print(tally.summary())
    tally.report()
    if tally.drafted and not write:
        print("dry run — nothing written" + ("" if outreach["auto_draft"] else " (pass --yes to write)"))
    return EXIT_OUT_OF_CREDITS if tally.out_of_credits else 0


def _rescore_leads(run_id: str | None) -> tuple[list, int]:
    """Leads whose score fields --rescore pushes, plus how many rows were skipped
    for being scored before v4. With a run: that run's scored.jsonl. Without:
    every run, the latest row per cache key (run ids sort by time)."""
    if run_id:
        paths = [RUNS_DIR / run_id / "scored.jsonl"]
        if not paths[0].exists():
            raise ValueError(f"no scored.jsonl in {RUNS_DIR / run_id} — run score first")
    else:
        paths = sorted(RUNS_DIR.glob("*/scored.jsonl"))
    latest: dict[str, object] = {}
    for path in paths:
        for lead in read_jsonl(path):
            if lead.status in ("scored", "disqualified", "synced"):
                latest[cache_key(lead)] = lead  # later runs overwrite earlier ones
    leads, old = [], 0
    for lead in latest.values():
        if lead.scoring_version < 4 or (lead.qualified and lead.tier is None):
            old += 1
        else:
            leads.append(lead)
    return leads, old


def _cmd_rescore(args: argparse.Namespace, config: dict, sink) -> int:
    """sync --rescore: update only the score properties (Tier, Priority, Score
    Reasons, Qualified, Lead Type, Audit Notes) on pages that already exist. Human
    fields are never written. Pages with no JSONL row are reported (all-runs mode)."""
    started_at = _now_iso()
    leads, old = _rescore_leads(args.run)
    report = sink.rescore(leads, limit=args.limit)
    no_source = None
    if not args.run:
        # a cache entry no v4 row points at: hand-made page, or a run not re-scored
        seen = load_seen(sink.cache_path)
        index = _email_index(seen)
        pointed = {match_cache_key(lead, seen, index) for lead in leads}
        no_source = sum(1 for key in seen if key not in pointed)

    counts = {
        "candidates": len(leads),
        "updated": len(report.updated),
        "no_page": len(report.no_page),
        "scored_before_v4": old,
        "beyond_limit": report.pending,
        "failed": len(report.failed),
    }
    if args.run:
        run_dir = RUNS_DIR / args.run
        manifest = _read_manifest(run_dir) or {"run_id": args.run}
        manifest["rescore"] = {
            "config_snapshot": {"sink": args.sink, "limit": args.limit},
            "counts": counts, "started_at": started_at, "finished_at": _now_iso(),
        }
        (run_dir / "manifest.json").write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
    scope = f"run {args.run}" if args.run else "all runs"
    print(
        f"rescore ({scope}): {counts['updated']} pages updated, {counts['no_page']} leads with no "
        f"Notion page, {counts['failed']} failed"
    )
    for lead, error in report.failed:
        print(f"  failed: {lead.company} ({lead.domain or 'no website'}): {error}")
    if old:
        print(f"  {old} rows scored before v4 skipped — run `score --run <id>` on those runs first")
    if no_source:
        print(f"  {no_source} Notion pages have no v4 JSONL row (hand-made, or run not re-scored): not updated")
    if report.pending:
        print(f"  {report.pending} beyond --limit left for the next run")
    return 0


def cmd_sync(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    sink = get_sink(args.sink, config)

    if args.refresh_cache:
        count = sink.refresh_cache()
        print(f"cache rebuilt from Notion: {count} entries -> {sink.cache_path}")
        return 0

    if getattr(args, "rescore", False):
        return _cmd_rescore(args, config, sink)

    if not args.run:
        raise ValueError("--run is required (unless using --refresh-cache or --rescore)")
    run_dir = RUNS_DIR / args.run
    scored_path = run_dir / "scored.jsonl"
    if not scored_path.exists():
        raise ValueError(f"no scored.jsonl in {run_dir} — run score first")
    leads = read_jsonl(scored_path)

    # only scored, qualified leads at or above --min-tier go out: "synced" is already
    # pushed, "disqualified" failed an L0 gate (never synced), a tier below the
    # minimum (or no tier: scored before v4) stays in JSONL, anything else is a
    # pending pass-through row from a limited enrich/score run
    min_tier = getattr(args, "min_tier", None) or "C"
    allowed = set(_TIER_ORDER[: _TIER_ORDER.index(min_tier) + 1])
    scored = [lead for lead in leads if lead.status == "scored"]
    to_sync = [lead for lead in scored if lead.qualified and lead.tier in allowed]
    below_tier = len(scored) - len(to_sync)
    already_synced = sum(1 for lead in leads if lead.status == "synced")
    disqualified = sum(1 for lead in leads if lead.status == "disqualified")
    not_scored = len(leads) - len(scored) - already_synced - disqualified
    started_at = _now_iso()

    report = sink.write(to_sync, limit=args.limit)

    # cache hits are also flipped: the lead is in Notion, however it got there
    done = {lead_key(lead) for lead in report.created + report.cached}
    results = [
        replace(lead, status="synced")
        if lead.status == "scored" and lead_key(lead) in done
        else lead
        for lead in leads
    ]
    write_jsonl(scored_path, results)
    CsvSink(run_dir / "scored.csv").write(results)

    manifest_path = run_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {"run_id": args.run}
    manifest["stage"] = "sync"
    manifest["sync"] = {
        "config_snapshot": {
            "sink": args.sink, "notion": config["notion"], "limit": args.limit, "min_tier": min_tier,
        },
        "counts": {
            "input": len(leads),
            "created": len(report.created),
            "already_in_notion": len(report.cached),
            "already_synced": already_synced,
            "skipped_disqualified": disqualified,
            "skipped_below_min_tier": below_tier,
            "skipped_not_scored": not_scored,
            "beyond_limit": report.pending,
            "failed": len(report.failed),
        },
        "started_at": started_at,
        "finished_at": _now_iso(),
    }
    manifest_path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    print(
        f"run {args.run}: {len(report.created)} created in Notion, "
        f"{len(report.cached)} already there (cache), {already_synced} already synced, "
        f"{len(report.failed)} failed"
        + (f", {disqualified} disqualified (not synced)" if disqualified else "")
        + (f", {below_tier} below tier {min_tier} or unscored by v4 (not synced)" if below_tier else "")
    )
    for lead, error in report.failed:
        print(f"  failed: {lead.company} ({lead.domain or 'no website'}): {error}")
    if not_scored:
        print(
            f"  warning: {not_scored} leads not yet scored were skipped — "
            f"finish `score --run {args.run}` first"
        )
    if report.pending:
        print(f"  {report.pending} beyond --limit left for the next run")
    print(f"  {run_dir}/scored.jsonl (+ scored.csv, manifest.json)")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    """collect -> enrich -> score -> sync -> draft, chained via the shared args
    namespace (collect stamps args.run for the later stages to read). Never
    prompts. Drafting (script_edits.md §11) is automatic: this run's pages at
    Tier >= outreach.auto_draft_min_tier; running out of credits returns
    EXIT_OUT_OF_CREDITS after everything else is done."""
    # build the sink first: a missing NOTION_TOKEN/database id should fail now,
    # not after collect/enrich have spent time
    get_sink(args.sink, load_config(args.config))
    for stage in (cmd_collect, cmd_enrich, cmd_score, cmd_sync):
        code = stage(args)
        if code != 0:
            return code
        print()
    return _auto_draft(args)


_BATCH_KEYS = {"source", "market", "niche", "location", "input", "limit", "sink", "include_seen", "bbox", "grid"}


def _batch_market(entry: dict, config: dict) -> Market:
    """`market:` is a preset id or a full block with an `id`."""
    value = entry["market"]
    if isinstance(value, str):
        return load_market(config, value)
    if isinstance(value, dict):
        return Market.from_dict(str(value.get("id", "")).strip().lower(), value)
    raise ValueError("market must be a preset id or a full market block")


def _load_batch(path: Path, config: dict) -> list[dict]:
    """Entries with their market and niche resolved and validated up front, so a
    typo in entry 5 fails before entry 1 spends any scraping time."""
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    runs = data.get("runs")
    if not isinstance(runs, list) or not runs:
        raise ValueError(f"{path}: expected a non-empty `runs:` list")
    for i, entry in enumerate(runs, 1):
        if not isinstance(entry, dict) or not entry.get("source"):
            raise ValueError(f"{path}: run #{i} needs at least `source`")
        unknown = set(entry) - _BATCH_KEYS
        if unknown:
            raise ValueError(f"{path}: run #{i} has unknown keys {sorted(unknown)}")
        missing = [k for k in ("market", "niche") if not entry.get(k)]
        if missing:
            raise ValueError(
                f"{path}: run #{i} needs {' and '.join(missing)} — no defaults (market: preset id "
                "or full block; niche: full block with name, search_terms, phrase)"
            )
        try:
            market = _batch_market(entry, config)
            niche = Niche.from_dict(entry["niche"])
            check_sources(niche, [s.strip() for s in str(entry["source"]).split(",")])
        except (TypeError, ValueError) as e:
            raise ValueError(f"{path}: run #{i}: {e}") from None
        entry["_market"], entry["_niche"] = market, niche
    return runs


def _run_args(base: argparse.Namespace, entry: dict) -> argparse.Namespace:
    """Same namespace `run` would get from the CLI, filled from one batch entry."""
    return argparse.Namespace(
        config=base.config,
        source=entry["source"],
        market=None,
        market_def=entry.get("_market"),
        niche_file=None,
        niche_def=entry.get("_niche"),
        location=entry.get("location"),
        input=entry.get("input"),
        limit=entry.get("limit"),
        sink=entry.get("sink", "notion"),
        include_seen=bool(entry.get("include_seen", False)),
        bbox=entry.get("bbox"),
        no_grid=entry.get("grid") is False,
        refresh_cache=False,
        run=None,
    )


def _public(entry: dict) -> dict:
    """The entry as written in the YAML (without the resolved _market/_niche)."""
    return {k: v for k, v in entry.items() if not k.startswith("_")}


def _describe(entry: dict) -> str:
    parts = []
    for k, v in _public(entry).items():
        if k == "niche" and isinstance(v, dict):
            v = v.get("name")
        elif k == "market" and isinstance(v, dict):
            v = v.get("id")
        parts.append(f"{k}={v}")
    return ", ".join(parts)


def cmd_batch(args: argparse.Namespace) -> int:
    """Run the entries of a batch file one after another (strictly sequential),
    each as `run` would: collect -> enrich -> score -> sync -> draft. Never prompts.

    One entry failing doesn't stop the rest. Running out of AI credits doesn't
    either: that entry finishes undrafted, later entries still collect, enrich,
    score and sync but skip drafting (every call would fail the same way); their
    pages stay draftable for `draft-notion`. Finished entries are recorded in
    data/batches/<name>.json and skipped on re-runs."""
    batch_path = Path(args.file)
    entries = _load_batch(batch_path, load_config(args.config))
    state_path = BATCHES_DIR / f"{batch_path.stem}.json"
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}

    def key(entry: dict) -> str:
        return json.dumps(_public(entry), sort_keys=True, ensure_ascii=False)

    def save() -> None:
        BATCHES_DIR.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    todo = [e for e in entries if state.get(key(e), {}).get("status") != "done"]
    print(f"batch {batch_path.name}: {len(todo)} to run, {len(entries) - len(todo)} already done")
    if args.dry_run:
        for i, entry in enumerate(todo, 1):
            print(f"  [{i}] {_describe(entry)}")
        return 0

    summary: list[tuple[str, str]] = []
    problems = 0
    out_of_credits = False
    for i, entry in enumerate(todo, 1):
        run_args = _run_args(args, entry)
        print(f"[{i}/{len(todo)}] {_describe(entry)}")
        note = ""
        try:
            code = 0
            for stage in (cmd_collect, cmd_enrich, cmd_score, cmd_sync):
                code = stage(run_args)
                if code != 0:
                    break
                print()
            if code == 0 and out_of_credits:
                note = ", not drafted (out of AI credits earlier in this batch)"
                print(f"drafting skipped{note[len(', not drafted'):]}: run `draft-notion` once credits are back")
            elif code == 0:
                if _auto_draft(run_args) == EXIT_OUT_OF_CREDITS:
                    out_of_credits = True
                    note = ", drafting stopped (out of AI credits)"
        except (ConfigError, ValueError, RuntimeError) as e:
            print(f"  error: {e}", file=sys.stderr)
            state[key(entry)] = {"status": "failed", "run_id": run_args.run, "error": str(e)}
            save()
            summary.append((_describe(entry), f"FAILED: {e}"))
            problems += 1
            continue
        status = "done" if code == 0 else "failed"
        state[key(entry)] = {"status": status, "run_id": run_args.run}
        save()
        summary.append((_describe(entry), f"run {run_args.run}: {status}{note}"))
        problems += status == "failed"
        if i < len(todo) and args.pause_s:
            time.sleep(args.pause_s)  # courtesy between runs (Maps / Notion rate limits)

    print("\nbatch summary:")
    for desc, result in summary:
        print(f"  {desc} -> {result}")
    if out_of_credits:
        print("out of AI credits: undrafted pages stay draftable — run `draft-notion` once credits are back",
              file=sys.stderr)
        return EXIT_OUT_OF_CREDITS if not problems else 1
    return 1 if problems else 0


def _ask(question: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    while True:
        answer = input(f"{question}{suffix}: ").strip()
        if answer or default:
            return answer or default
        print("  required — please enter a value")


_GO_MODES = ("Lead Gen", "Draft Outreach")
_DRAFT_SCOPES = (
    "Pages with no draft yet",
    "Redraft pages that already have a draft",
)


def _ask_choice(question: str, options: tuple[str, ...], default: str) -> str:
    """Numbered menu; accepts the number or the (prefix of the) option name."""
    print(f"{question}:")
    for i, option in enumerate(options, 1):
        print(f"  {i}. {option}")
    answer = _ask("Choose", default).strip().lower()
    if answer.isdigit() and 1 <= int(answer) <= len(options):
        return options[int(answer) - 1]
    matches = [o for o in options if o.lower().startswith(answer)]
    if len(matches) == 1:
        return matches[0]
    raise ValueError(f"unknown choice {answer!r} — pick one of: {', '.join(options)}")


def _go_draft_outreach(args: argparse.Namespace) -> int:
    """draft-notion with the scope and limit asked here. Writes straight away when
    outreach.auto_draft is on (no approval step, script_edits.md §11); with it off,
    a dry run first and a yes/no before writing (unless go --yes)."""
    scope = _ask_choice("Which Notion pages?", _DRAFT_SCOPES, "1")
    raw = _ask("How many at most (a number, or 'all')", str(args.limit) if args.limit else "5")
    limit = None if raw.lower() == "all" else int(raw)
    company = ""
    if limit == 1:
        company = _ask("Which lead (company name, or auto for the next one)", "auto")
        company = "" if company.lower() == "auto" else company
    base = dict(
        config=args.config, limit=limit, yes=bool(getattr(args, "yes", False)), dry_run=False,
        company=company, redraft=scope == _DRAFT_SCOPES[1],
    )
    if load_config(args.config)["outreach"]["auto_draft"] or base["yes"]:
        return cmd_draft_notion(argparse.Namespace(**base))
    code = cmd_draft_notion(argparse.Namespace(**base))
    if code != 0 or _ask("Write these drafts to Notion? (y/n)", "n").lower() not in ("y", "yes"):
        return code
    return cmd_draft_notion(argparse.Namespace(**{**base, "yes": True}))


def _ask_optional(question: str) -> str:
    """Free answer, empty allowed."""
    return input(f"{question} (optional, Enter to skip): ").strip()


def _split_list(text: str) -> list[str]:
    return [x.strip() for x in text.split(",") if x.strip()]


_NEW_MARKET = "New market"
_NEW_NICHE = "New niche"


def _go_market(config: dict) -> Market:
    presets = tuple(config["markets"])
    options = tuple(f"{m} ({config['markets'][m]['country']})" for m in presets) + (_NEW_MARKET,)
    choice = _ask_choice("Market", options, "1")
    if choice != _NEW_MARKET:
        return load_market(config, presets[options.index(choice)])
    while True:
        try:
            market = market_from_prompts(_ask)
            break
        except ValueError as e:
            print(f"  {e} — let's try again")
    print("\n  new market, used for this run only. To keep it, paste this into config.yaml:\n")
    print("    " + market_yaml(market).replace("\n", "\n    ").rstrip())
    return market


def _earlier_niches() -> list[tuple[str, Niche]]:
    """Niches from earlier runs' manifests, newest first, one per name."""
    found: dict[str, tuple[str, Niche]] = {}
    for manifest_path in sorted(RUNS_DIR.glob("*/manifest.json"), reverse=True):
        try:
            data = json.loads(manifest_path.read_text(encoding="utf-8")).get("niche")
            niche = Niche.from_dict(data) if data else None
        except (ValueError, TypeError, json.JSONDecodeError):
            continue
        if niche and niche.name.lower() not in found:
            found[niche.name.lower()] = (manifest_path.parent.name, niche)
    return list(found.values())


def _go_niche() -> Niche:
    """Always typed or explicitly copied — nothing is preselected."""
    earlier = _earlier_niches()[:9]
    if earlier:
        options = (_NEW_NICHE,) + tuple(f"Copy {n.name!r} from run {run_id}" for run_id, n in earlier)
        choice = _ask_choice("Niche", options, "")
        if choice != _NEW_NICHE:
            return earlier[options.index(choice) - 1][1]
    while True:
        name = _ask("Niche name (e.g. dental clinic)")
        terms = _split_list(_ask("Google Maps search terms, comma-separated", name))
        phrase = _ask("Draft phrase, plural, in the market language (e.g. stomatološke ordinacije)")
        accepted = _split_list(_ask_optional("Accepted Google categories, comma-separated"))
        question = _ask_optional("Question for this niche")
        landing = _ask_optional("Landing URL")
        try:
            return Niche(name=name, search_terms=terms, phrase=phrase, accepted_categories=accepted,
                         question=question, landing_url=landing).validated()
        except ValueError as e:
            print(f"  {e} — let's try again")


def _go_osm_tags(niche: Niche) -> Niche:
    """OSM needs tags, asked only when the source includes osm and the niche has none."""
    if niche.osm_tags:
        return niche
    while True:
        tags = _split_list(_ask("OSM tags, comma-separated (raw key=value like amenity=dentist, or known keys)"))
        try:
            return Niche(**{**niche.to_dict(), "osm_tags": tags}).validated()
        except ValueError as e:
            print(f"  {e}")


def cmd_go(args: argparse.Namespace) -> int:
    """Interactive entry point: pick what to run, answer the prompts, run it.
    Lead Gen = collect -> enrich -> score -> sync -> draft (automatic, like `run`);
    Draft Outreach = draft-notion. Lead Gen asks for the market (preset or new),
    creates the niche (or copies one from an earlier run — never preselected),
    then source and location, and runs without another prompt after the summary
    confirm (--yes skips that too). --source maps/osm/notion skips the menu."""
    config = load_config(args.config)
    source = (args.source or "").lower()
    if source == "notion":
        mode = "Draft Outreach"
    elif source in ("maps", "osm"):
        mode = "Lead Gen"
    elif source:
        raise ValueError(f"unknown source {source!r} — choose maps, osm or notion")
    else:
        mode = _ask_choice("What to run?", _GO_MODES, "Lead Gen")

    if mode == "Draft Outreach":
        return _go_draft_outreach(args)

    market = _go_market(config)
    niche = _go_niche()
    if not source:
        source = _ask("Source (maps / osm)", "maps").lower()
    if source not in ("maps", "osm"):
        raise ValueError(f"unknown source {source!r} — choose maps or osm")
    if source == "osm":
        niche = _go_osm_tags(niche)
    location = _ask("Scrape location", config["collect"]["location"])
    terms = niche.osm_tags if source == "osm" else niche.search_terms
    print(
        f"\n  mode: {mode}\n  market: {market.id} ({market.country}, drafts in {market.language})"
        f"\n  niche: {niche.name} — \"{niche.phrase}\"\n  source: {source} ({', '.join(terms)})"
        f"\n  location: {location}"
    )
    outreach = config["outreach"]
    if outreach["auto_draft"]:
        print(f"  drafts: automatic after sync, Tier {outreach['auto_draft_min_tier']} and better")
    bbox = resolve_bbox({"location": location}, config["maps"]["areas"])
    if bbox and source == "maps":
        cells = grid_cell_count(parse_bbox(bbox), config["maps"]["grid_cell_km"])
        print(f"  grid: ~{cells} cells x {len(terms)} search terms (longer run, many more leads)")
    elif bbox:
        print(f"  area: box {bbox} from maps.areas (not an area-name lookup)")
    if not getattr(args, "yes", False) and _ask("Start? (y/n)", "y").lower() not in ("y", "yes"):
        print("cancelled")
        return 0
    print()
    return cmd_run(
        argparse.Namespace(
            config=args.config,
            source=source,
            market=None,
            market_def=market,
            niche_file=None,
            niche_def=niche,
            location=location,
            input=None,
            limit=args.limit,
            sink="notion",
            include_seen=False,
            bbox=None,
            no_grid=False,
            refresh_cache=False,
        )
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m pipeline.runner")
    parser.add_argument("--config", default="config.yaml", help="path to config.yaml")
    sub = parser.add_subparsers(dest="command", required=True)

    collect = sub.add_parser("collect", help="collect leads from one or more sources")
    collect.add_argument(
        "--source",
        required=True,
        help='comma-separated: "osm", "maps", "manual" — e.g. "osm,manual"',
    )
    collect.add_argument(
        "--market", help="market preset id from config.yaml markets:, e.g. serbia / uae (required)"
    )
    collect.add_argument(
        "--niche-file", help="YAML niche definition: name, search_terms, phrase, optional osm_tags, ... (required)"
    )
    collect.add_argument("--location", help='city or area, e.g. "Novi Sad"')
    collect.add_argument(
        "--input", help="manual collector: path to the hand-gathered leads CSV"
    )
    collect.add_argument(
        "--limit", type=int, help="keep only the first N leads — dry-run a batch"
    )
    collect.add_argument(
        "--include-seen",
        action="store_true",
        help="keep leads already in the Notion cache (default: skip them after collect)",
    )
    collect.add_argument(
        "--bbox",
        help='maps grid area "minLat,minLon,maxLat,maxLon" (default: maps.areas entry for --location)',
    )
    collect.add_argument(
        "--no-grid", action="store_true", help="maps: one plain query, no grid (about 20 leads per category)"
    )
    collect.set_defaults(func=cmd_collect)

    enrich = sub.add_parser(
        "enrich", help="contact enrichment, then website audit, on a collected run"
    )
    enrich.add_argument("--run", required=True, help="run id under data/runs/")
    enrich.add_argument(
        "--limit", type=int, help="process only the first N pending leads — dry-run"
    )
    enrich.add_argument(
        "--only", choices=_RESCAN_STEPS, help="with --rescan: the one step to re-run (audit)"
    )
    enrich.add_argument(
        "--rescan", action="store_true",
        help="with --only: re-run that step on already-enriched leads (e.g. old runs without audit signals)",
    )
    enrich.set_defaults(func=cmd_enrich)

    score_cmd = sub.add_parser(
        "score", help="apply scoring weights from config.yaml to an enriched run"
    )
    score_cmd.add_argument("--run", required=True, help="run id under data/runs/")
    score_cmd.add_argument(
        "--market", help="only for runs collected before markets existed: preset id for the country gate"
    )
    score_cmd.add_argument(
        "--limit", type=int, help="score only the first N leads — dry-run"
    )
    score_cmd.set_defaults(func=cmd_score)

    ar_cmd = sub.add_parser(
        "audit-report", help="one-page audit report (Markdown + HTML) per lead, in the market's language"
    )
    ar_cmd.add_argument("--run", required=True, help="run id under data/runs/")
    ar_cmd.add_argument("--domain", help="only the lead with this domain (any tier or status)")
    ar_cmd.add_argument(
        "--min-tier", choices=("A", "B", "C", "D"), default="C",
        help="qualified leads at this tier or better (default C), best priority first",
    )
    ar_cmd.add_argument("--limit", type=int, help="at most N reports")
    ar_cmd.add_argument(
        "--rescan", action="store_true",
        help="re-audit each site now (probe + Lighthouse) instead of the stored data; JSONL is not changed",
    )
    ar_cmd.add_argument(
        "--force", action="store_true", help="rebuild existing .md reports (overwrites hand edits)"
    )
    ar_cmd.set_defaults(func=cmd_audit_report)

    dn_cmd = sub.add_parser(
        "draft-notion",
        help="catch-up drafting for Notion pages with no draft yet (Tier >= outreach.auto_draft_min_tier, "
        "Verdict not Skip); writes when outreach.auto_draft is on",
    )
    dn_cmd.add_argument(
        "--company", help="draft only pages whose Company contains this text"
    )
    dn_cmd.add_argument(
        "--redraft", action="store_true", help="also rewrite pages that already have a draft"
    )
    dn_cmd.add_argument("--limit", type=int, help="draft at most N pages")
    dn_cmd.add_argument("--dry-run", action="store_true", help="print the drafts, write nothing")
    dn_cmd.add_argument(
        "--yes", action="store_true", help="write even when outreach.auto_draft is off (default then: dry run)"
    )
    dn_cmd.set_defaults(func=cmd_draft_notion)

    sync_cmd = sub.add_parser(
        "sync", help="push scored leads to a sink, deduped via the local cache"
    )
    sync_cmd.add_argument("--run", help="run id under data/runs/")
    sync_cmd.add_argument("--sink", required=True, help='sink name, e.g. "notion"')
    sync_cmd.add_argument(
        "--limit", type=int, help="create at most N pages — dry-run"
    )
    sync_cmd.add_argument(
        "--min-tier", choices=("A", "B", "C"), default="C",
        help="sync only qualified leads at this tier or better (default C; D stays in JSONL)",
    )
    sync_cmd.add_argument(
        "--rescore", action="store_true",
        help="update only the score properties on pages already in Notion "
        "(this --run, or the latest v4 row of every run)",
    )
    sync_cmd.add_argument(
        "--refresh-cache",
        action="store_true",
        help="rebuild seen_domains.json from Notion, then exit",
    )
    sync_cmd.set_defaults(func=cmd_sync)

    run_cmd = sub.add_parser(
        "run", help="collect, enrich, score, sync and draft in one go (never prompts)"
    )
    run_cmd.add_argument(
        "--source",
        required=True,
        help='comma-separated: "osm", "maps", "manual" — e.g. "osm,manual"',
    )
    run_cmd.add_argument(
        "--market", help="market preset id from config.yaml markets:, e.g. serbia / uae (required)"
    )
    run_cmd.add_argument(
        "--niche-file", help="YAML niche definition: name, search_terms, phrase, optional osm_tags, ... (required)"
    )
    run_cmd.add_argument("--location", help='city or area, e.g. "Novi Sad"')
    run_cmd.add_argument(
        "--input", help="manual collector: path to the hand-gathered leads CSV"
    )
    run_cmd.add_argument(
        "--limit",
        type=int,
        help="cap leads collected, and dry-run enrich/score/sync to the same count",
    )
    run_cmd.add_argument(
        "--sink", default="notion", help='sink name, e.g. "notion" (default)'
    )
    run_cmd.add_argument(
        "--include-seen",
        action="store_true",
        help="keep leads already in the Notion cache (default: skip them after collect)",
    )
    run_cmd.add_argument(
        "--bbox",
        help='maps grid area "minLat,minLon,maxLat,maxLon" (default: maps.areas entry for --location)',
    )
    run_cmd.add_argument(
        "--no-grid", action="store_true", help="maps: one plain query, no grid (about 20 leads per category)"
    )
    run_cmd.set_defaults(func=cmd_run, refresh_cache=False)

    go_cmd = sub.add_parser(
        "go", help="interactive run: prompts for market, niche, source and location"
    )
    go_cmd.add_argument("--source", help="skip the menu: maps / osm (Lead Gen) or notion (Draft Outreach)")
    go_cmd.add_argument("--limit", type=int, help="dry-run: cap leads at N")
    go_cmd.add_argument("--yes", action="store_true", help="skip the final Start? confirmation")
    go_cmd.set_defaults(func=cmd_go)

    batch_cmd = sub.add_parser(
        "batch", help="run the entries of a batches/<name>.yaml file one after another"
    )
    batch_cmd.add_argument("--file", required=True, help="batch YAML with a `runs:` list")
    batch_cmd.add_argument(
        "--pause-s", type=int, default=30, help="seconds to wait between runs (default 30)"
    )
    batch_cmd.add_argument(
        "--dry-run", action="store_true", help="print the planned runs without executing"
    )
    batch_cmd.set_defaults(func=cmd_batch)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (ConfigError, ValueError, RuntimeError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
