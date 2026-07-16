"""CLI entrypoint — wires config, collectors, and sinks together.

Stages themselves are functions of lists of leads (ARCHITECTURE.md §3);
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

from pipeline.collectors import get_collector
from pipeline.config import ConfigError, load_config
from pipeline.dedupe import dedupe_leads, lead_key
from pipeline.enrichers import apply_enrichers, get_enricher
from pipeline.schema import read_jsonl, write_jsonl
from pipeline.scoring import score
from pipeline.sinks import get_sink
from pipeline.sinks.csv_sink import CsvSink

# contact data first, then the website audit — ROADMAP Phase 2 ordering
_ENRICH_STEPS = ("contact", "audit")

RUNS_DIR = Path("data/runs")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def cmd_collect(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    params = {
        "location": args.location or config["collect"]["location"],
        "categories": (
            [c.strip() for c in args.categories.split(",") if c.strip()]
            if args.categories
            else config["collect"]["categories"]
        ),
        "input": args.input,
    }
    sources = [s.strip() for s in args.source.split(",") if s.strip()]

    # instantiate all collectors up front so a typo'd source name or missing
    # binary fails before any collection effort is spent
    collectors = {source: get_collector(source, config) for source in sources}
    run_id = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    started_at = _now_iso()

    raw_leads: list = []
    raw_by_source: dict[str, int] = {}
    for source, collector in collectors.items():
        collected = list(collector.collect(params))
        raw_by_source[source] = len(collected)
        raw_leads.extend(collected)
    deduped = dedupe_leads(raw_leads)
    leads = deduped if args.limit is None else deduped[: args.limit]
    for lead in leads:
        lead.run_id = run_id
        # stamp the market the run targeted; a collector that set its own
        # (the manual CSV's optional "location" column) wins
        if not lead.location:
            lead.location = params["location"]

    run_dir = RUNS_DIR / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    write_jsonl(run_dir / "collected.jsonl", leads)
    CsvSink(run_dir / "collected.csv").write(leads)

    manifest = {
        "run_id": run_id,
        "stage": "collect",
        "config_snapshot": {
            "sources": sources,
            "params": params,
            "limit": args.limit,
            "overpass": config["overpass"],
        },
        "counts": {
            "raw": len(raw_leads),
            "raw_by_source": raw_by_source,
            "after_dedupe": len(deduped),
            "written": len(leads),
        },
        "started_at": started_at,
        "finished_at": _now_iso(),
    }
    (run_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    with_domain = sum(1 for lead in leads if lead.domain)
    source_summary = ", ".join(f"{s}: {n}" for s, n in raw_by_source.items())
    print(f"run {run_id}: {len(raw_leads)} raw ({source_summary}) -> {len(leads)} written")
    print(f"  {with_domain} with a website, {len(leads) - with_domain} without")
    print(f"  {run_dir}/collected.jsonl (+ collected.csv, manifest.json)")
    return 0


def cmd_enrich(args: argparse.Namespace) -> int:
    config = load_config(args.config)
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
            lead_key(lead): lead
            for lead in read_jsonl(enriched_path)
            if lead.status == "enriched"
        }

    enrichers = [(name, get_enricher(name, config)) for name in _ENRICH_STEPS]
    delay_s = config["enrich"]["delay_s"]
    started_at = _now_iso()

    results = []
    processed = skipped = failed = 0
    for lead in leads:
        done = previous.get(lead_key(lead))
        if done is not None:
            results.append(done)
            skipped += 1
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
        print(
            f"  [{processed}] {lead.company} "
            f"({lead.domain or 'no website'}): {enriched.status}"
        )
        results.append(enriched)

    write_jsonl(enriched_path, results)
    CsvSink(run_dir / "enriched.csv").write(results)

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
            "processed": processed,
            "failed": failed,
            "pending": len(leads) - skipped - processed,
        },
        "started_at": started_at,
        "finished_at": _now_iso(),
    }
    manifest_path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    pending = len(leads) - skipped - processed
    print(
        f"run {args.run}: {processed} enriched ({failed} failed), "
        f"{skipped} already done, {pending} pending"
    )
    print(f"  {run_dir}/enriched.jsonl (+ enriched.csv, manifest.json)")
    return 0


def cmd_score(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    run_dir = RUNS_DIR / args.run
    enriched_path = run_dir / "enriched.jsonl"
    if not enriched_path.exists():
        raise ValueError(f"no enriched.jsonl in {run_dir} — run enrich first")
    leads = read_jsonl(enriched_path)

    # scored.jsonl may hold hand-edited intent_score values (the manual
    # workflow in SCORING.md §5) — the only hand-entered data in the
    # pipeline, so a re-score must never drop or reset those rows
    scored_path = run_dir / "scored.jsonl"
    previous = {}
    if scored_path.exists():
        previous = {lead_key(lead): lead for lead in read_jsonl(scored_path)}

    weights = config["scoring"]
    started_at = _now_iso()

    results = []
    scored_count = pending = 0
    for lead in leads:
        # a lead enrichment never reached (status still "collected" after a
        # limited enrich run) has a domain but no audit data — scoring it
        # would fabricate an "unreachable domain" signal for a site nobody
        # tried. Pass it through unscored; a full enrich run picks it up.
        if lead.status not in ("enriched", "enrich_failed"):
            results.append(previous.get(lead_key(lead), lead))
            pending += 1
            continue
        # --limit dry-runs must not truncate scored.jsonl: rows beyond the
        # limit keep their previous scored values (including intent) rather
        # than being dropped from the file
        if args.limit is not None and scored_count >= args.limit:
            results.append(previous.get(lead_key(lead), lead))
            continue
        prev = previous.get(lead_key(lead))
        if prev is not None and prev.intent_score:
            lead = replace(lead, intent_score=prev.intent_score)
        results.append(score(lead, weights))
        scored_count += 1

    write_jsonl(scored_path, results)
    CsvSink(run_dir / "scored.csv").write(results)

    carried = len(leads) - scored_count - pending
    by_type: dict[str, int] = {}
    for lead in results:
        if lead.lead_type is not None:
            by_type[lead.lead_type] = by_type.get(lead.lead_type, 0) + 1

    manifest_path = run_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {"run_id": args.run}
    manifest["stage"] = "score"
    manifest["score"] = {
        "config_snapshot": {"scoring": weights, "limit": args.limit},
        "counts": {
            "input": len(leads),
            "scored": scored_count,
            "pending_enrichment": pending,
            "beyond_limit": carried,
            "by_lead_type": by_type,
            "with_intent": sum(1 for lead in results if lead.intent_score),
        },
        "started_at": started_at,
        "finished_at": _now_iso(),
    }
    manifest_path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    type_summary = ", ".join(f"{t}: {n}" for t, n in sorted(by_type.items()))
    print(f"run {args.run}: {scored_count} scored (v{weights['version']}) — {type_summary}")
    if pending:
        print(
            f"  warning: {pending} leads still pending enrichment were passed "
            f"through unscored — finish `enrich --run {args.run}` first"
        )
    if carried:
        print(f"  {carried} beyond --limit kept their previous scored values")
    ranked = [lead for lead in results if lead.total_score is not None]
    for lead in sorted(ranked, key=lambda l: l.total_score, reverse=True)[:5]:
        print(
            f"  {lead.total_score:5.1f}  {lead.company} "
            f"({lead.domain or 'no website'}, {lead.lead_type})"
        )
    print(f"  {run_dir}/scored.jsonl (+ scored.csv, manifest.json)")
    return 0


def cmd_sync(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    sink = get_sink(args.sink, config)

    if args.refresh_cache:
        count = sink.refresh_cache()
        print(f"cache rebuilt from Notion: {count} entries -> {sink.cache_path}")
        return 0

    if not args.run:
        raise ValueError("--run is required (unless using --refresh-cache)")
    run_dir = RUNS_DIR / args.run
    scored_path = run_dir / "scored.jsonl"
    if not scored_path.exists():
        raise ValueError(f"no scored.jsonl in {run_dir} — run score first")
    leads = read_jsonl(scored_path)

    # only scored leads go out: "synced" is already pushed, anything else is
    # a pending pass-through row from a limited enrich/score run
    to_sync = [lead for lead in leads if lead.status == "scored"]
    already_synced = sum(1 for lead in leads if lead.status == "synced")
    not_scored = len(leads) - len(to_sync) - already_synced
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
        "config_snapshot": {"sink": args.sink, "notion": config["notion"], "limit": args.limit},
        "counts": {
            "input": len(leads),
            "created": len(report.created),
            "already_in_notion": len(report.cached),
            "already_synced": already_synced,
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
    collect.add_argument("--location", help='OSM area name, e.g. "Berlin"')
    collect.add_argument(
        "--input", help="manual collector: path to the hand-gathered leads CSV"
    )
    collect.add_argument(
        "--categories",
        help='comma-separated, e.g. "restaurant,hairdresser" or raw "craft=roofer"',
    )
    collect.add_argument(
        "--limit", type=int, help="keep only the first N leads — dry-run a batch"
    )
    collect.set_defaults(func=cmd_collect)

    enrich = sub.add_parser(
        "enrich", help="contact enrichment, then website audit, on a collected run"
    )
    enrich.add_argument("--run", required=True, help="run id under data/runs/")
    enrich.add_argument(
        "--limit", type=int, help="process only the first N pending leads — dry-run"
    )
    enrich.set_defaults(func=cmd_enrich)

    score_cmd = sub.add_parser(
        "score", help="apply scoring weights from config.yaml to an enriched run"
    )
    score_cmd.add_argument("--run", required=True, help="run id under data/runs/")
    score_cmd.add_argument(
        "--limit", type=int, help="score only the first N leads — dry-run"
    )
    score_cmd.set_defaults(func=cmd_score)

    sync_cmd = sub.add_parser(
        "sync", help="push scored leads to a sink, deduped via the local cache"
    )
    sync_cmd.add_argument("--run", help="run id under data/runs/")
    sync_cmd.add_argument("--sink", required=True, help='sink name, e.g. "notion"')
    sync_cmd.add_argument(
        "--limit", type=int, help="create at most N pages — dry-run"
    )
    sync_cmd.add_argument(
        "--refresh-cache",
        action="store_true",
        help="rebuild seen_domains.json from Notion, then exit",
    )
    sync_cmd.set_defaults(func=cmd_sync)

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
