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
from pipeline.collectors.maps import grid_cell_count, parse_bbox, resolve_bbox
from pipeline.config import ConfigError, load_config
from pipeline.dedupe import dedupe_leads, lead_key
from pipeline.drafting import DraftBudgetError, draft_lead, load_examples
from pipeline.enrichers import apply_enrichers, get_enricher
from pipeline.schema import read_jsonl, write_jsonl
from pipeline.scoring import score
from pipeline.sinks import get_sink
from pipeline.sinks.csv_sink import CsvSink
from pipeline.sinks.notion_sink import _rich_text, _select_name, _text, cache_key, lead_from_page, load_seen

# find a missing website first, then contact data, then the website audit
_ENRICH_STEPS = ("discovery", "contact", "audit")

RUNS_DIR = Path("data/runs")
BATCHES_DIR = Path("data/batches")

# exit code for "out of AI credits / usage limit" — `run` stops before sync (draft
# fields are only written when a Notion page is created) and `batch` pauses
EXIT_OUT_OF_CREDITS = 3


def _out_of_credits_message(detail: str) -> str:
    return (
        "drafting stopped: the claude CLI reports no credits / usage limit left "
        f"({detail}). Finished drafts are saved. Once credits are back, resume with "
        "`draft --run <run_id>` (skips leads that already have a draft), then `sync`."
    )


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _has_no_contact_info(lead) -> bool:
    """No email, phone, social link, or website — nothing to outreach through."""
    return not (
        lead.contact_emails or lead.contact_phones or lead.social_links or lead.domain
    )


def cmd_collect(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    params = {
        "location": args.location or config["collect"]["location"],
        "country": args.country or config["collect"]["country"],
        "categories": (
            [c.strip() for c in args.categories.split(",") if c.strip()]
            if args.categories
            else config["collect"]["categories"]
        ),
        "input": args.input,
        "bbox": getattr(args, "bbox", None),
        "grid": not getattr(args, "no_grid", False),
    }
    sources = [s.strip() for s in args.source.split(",") if s.strip()]

    # industry becomes a Notion select option and picks the Serbian niche phrase
    # in drafts — warn (not fail: raw OSM tags are legitimate) on unmapped ones
    known = config["outreach"]["industry_labels"]
    for category in params["categories"]:
        if category not in known:
            print(
                f"warning: category {category!r} has no outreach.industry_labels entry — "
                "drafts will use the raw key; add it to config.yaml"
            )

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
            collected = list(collector.collect(params))
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
    already_in_notion = 0
    if not getattr(args, "include_seen", False):
        seen = load_seen(config["notion"]["cache_path"])
        fresh = [lead for lead in deduped if cache_key(lead) not in seen]
        already_in_notion = len(deduped) - len(fresh)
        deduped = fresh
    leads = deduped if args.limit is None else deduped[: args.limit]
    # new leads per category — dedupe keeps the first occurrence, so a category
    # listed later only shows what earlier ones didn't already find
    new_by_category: dict[str, int] = {}
    for lead in leads:
        new_by_category[lead.industry] = new_by_category.get(lead.industry, 0) + 1
    for lead in leads:
        lead.run_id = run_id
        # stamp the market/country the run targeted; a collector that set
        # its own (the manual CSV's optional "location" column) wins
        if not lead.location:
            lead.location = params["location"]
        if not lead.country:
            lead.country = params["country"]

    args.run = run_id  # lets `run` (collect+enrich+score+sync chained) find it

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
            "already_in_notion": already_in_notion,
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
    if errors_by_source:
        print(f"  {len(errors_by_source)} source(s) failed: {', '.join(errors_by_source)} (see manifest.json)")
    if already_in_notion:
        print(f"  {already_in_notion} already in Notion (cache) — skipped")
    if len(new_by_category) > 1:
        breakdown = ", ".join(f"{c} {n}" for c, n in new_by_category.items())
        print(f"  new per category: {breakdown}")
    print(f"  {with_domain} with a website, {len(leads) - with_domain} without")
    print(f"  {run_dir}/collected.jsonl (+ collected.csv, manifest.json)")
    return 0


def _stage_keys(lead) -> set[tuple[str, str]]:
    """Keys a stage's output lead is matched by on re-runs: its own, plus the
    company key — discovery gives a lead a domain after collect, so the
    collected lead (still keyed by company) must find it."""
    return {lead_key(lead), ("company", lead.company.strip().lower())}


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
    print(f"  {run_dir}/enriched.jsonl (+ enriched.csv, manifest.json)")
    if filtered_count or already_filtered:
        print(
            f"  {run_dir}/filtered.jsonl: "
            f"{len(previous_filtered) + filtered_count} lead(s) with no email/phone/social/website"
        )
    return 0


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

    weights = config["scoring"]
    qualification = config["qualification"]
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
        # limit keep their previous scored values rather than being dropped
        if args.limit is not None and scored_count >= args.limit:
            results.append(previous.get(lead_key(lead), lead))
            continue
        results.append(score(lead, weights, qualification))
        scored_count += 1

    write_jsonl(scored_path, results)
    CsvSink(run_dir / "scored.csv").write(results)

    carried = len(leads) - scored_count - pending
    by_type: dict[str, int] = {}
    for lead in results:
        if lead.lead_type is not None:
            by_type[lead.lead_type] = by_type.get(lead.lead_type, 0) + 1
    qualified_count = sum(1 for lead in results if lead.qualified)

    manifest_path = run_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {"run_id": args.run}
    manifest["stage"] = "score"
    manifest["score"] = {
        "config_snapshot": {"scoring": weights, "qualification": qualification, "limit": args.limit},
        "counts": {
            "input": len(leads),
            "scored": scored_count,
            "pending_enrichment": pending,
            "beyond_limit": carried,
            "by_lead_type": by_type,
            "qualified": qualified_count,
        },
        "started_at": started_at,
        "finished_at": _now_iso(),
    }
    manifest_path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    type_summary = ", ".join(f"{t}: {n}" for t, n in sorted(by_type.items()))
    print(
        f"run {args.run}: {scored_count} scored (v{weights['version']}), "
        f"{qualified_count} qualified — {type_summary}"
    )
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


def cmd_draft(args: argparse.Namespace) -> int:
    """Suggest a channel and draft ONE first message per qualified, scored lead
    that has none yet (docs/OUTREACH.md). Drafts are for human review — nothing
    is sent. Re-runs skip leads that already have a draft, so a failed lead is
    retried and the rest are not."""
    config = load_config(args.config)
    run_dir = RUNS_DIR / args.run
    scored_path = run_dir / "scored.jsonl"
    if not scored_path.exists():
        raise ValueError(f"no scored.jsonl in {run_dir} — run score first")
    leads = read_jsonl(scored_path)
    started_at = _now_iso()

    outreach, draft_cfg = config["outreach"], config["draft"]
    examples = load_examples(outreach["examples_path"])
    drafted = failed = skipped = no_contact = 0
    results = []
    out_of_credits = ""
    for lead in leads:
        statuses = ("scored", "synced") if args.include_synced else ("scored",)
        eligible = lead.status in statuses and lead.qualified and not lead.draft_message
        if not eligible or (args.limit is not None and drafted + failed >= args.limit):
            skipped += 1
            results.append(lead)
            continue
        try:
            out = draft_lead(lead, outreach, draft_cfg, variant=drafted + failed, examples=examples)
        except DraftBudgetError as e:
            out_of_credits = str(e)
            break
        if out.draft_message:
            drafted += 1
        elif out.errors != lead.errors:
            failed += 1
        else:
            no_contact += 1
        results.append(out)
        write_jsonl(scored_path, results + leads[len(results):])  # incremental: a crash keeps finished drafts

    results = results + leads[len(results):]  # untouched leads after an early stop
    write_jsonl(scored_path, results)
    CsvSink(run_dir / "scored.csv").write(results)

    manifest_path = run_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {"run_id": args.run}
    manifest["stage"] = "draft"
    manifest["draft"] = {
        "config_snapshot": {"draft": draft_cfg, "limit": args.limit},
        "counts": {
            "input": len(leads), "drafted": drafted, "failed": failed,
            "no_contact_data": no_contact, "skipped": skipped,
        },
        "stopped_out_of_credits": bool(out_of_credits),
        "started_at": started_at,
        "finished_at": _now_iso(),
    }
    manifest_path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    print(
        f"run {args.run}: {drafted} drafted, {failed} failed, "
        f"{no_contact} without contact data, {skipped} skipped"
    )
    for lead in results:
        if lead.errors and lead.errors[-1].startswith("draft:") and not lead.draft_message:
            print(f"  failed: {lead.company}: {lead.errors[-1]}")
        elif lead.draft_notes:
            print(f"  verify before sending: {lead.company}: {lead.draft_notes}")
    print(f"  {scored_path} (+ scored.csv, manifest.json)")
    if out_of_credits:
        print(_out_of_credits_message(out_of_credits), file=sys.stderr)
        return EXIT_OUT_OF_CREDITS
    return 0


def cmd_draft_notion(args: argparse.Namespace) -> int:
    """Draft for Notion pages (hand-made or synced) that have an SM Audit note and
    no draft yet: the page is the input, so no run dir. Dry run unless --yes.
    --redraft also rewrites existing drafts, --all also drafts pages without a
    note. Status -> Queued only from "Not started"."""
    config = load_config(args.config)
    sink = get_sink("notion", config)
    outreach, draft_cfg = config["outreach"], config["draft"]
    examples = load_examples(outreach["examples_path"])
    country = config["collect"].get("country", "")

    redraft = getattr(args, "redraft", False)
    pages = sink.query_draftable(
        args.limit, redraft=redraft, need_note=not getattr(args, "all", False),
        company=getattr(args, "company", "") or "",
    )
    if not pages:
        print("nothing to draft: add an SM Audit note to a page with Status Not started / Queued "
              "(--all drafts pages without a note, --redraft rewrites existing drafts)")
        return 0
    drafted = failed = no_contact = 0
    for page in pages:
        lead = lead_from_page(page, country=country)
        out = draft_lead(lead, outreach, draft_cfg, variant=drafted + failed, examples=examples)
        if not out.draft_message:
            if out.errors != lead.errors:
                failed += 1
                print(f"  failed: {lead.company}: {out.errors[-1]}")
            else:
                no_contact += 1
                print(f"  no contact data (add an Instagram link, phone or email): {lead.company}")
            continue
        drafted += 1
        had_draft = bool(_rich_text(page["properties"].get("Draft Message")))
        verb = "overwrite" if had_draft else "write"
        print(f"  {verb if args.yes else 'would ' + verb}: {lead.company} -> {out.suggested_channel}")
        print(f"    {out.draft_message}")
        if out.draft_notes:
            print(f"    verify before sending: {out.draft_notes}")
        if args.yes:
            update = {
                "Suggested Channel": {"select": {"name": out.suggested_channel}},
                "Draft Message": _text(out.draft_message[:2000]),
            }
            if _select_name(page["properties"].get("Status")) in ("Not started", ""):
                update["Status"] = {"select": {"name": "Queued"}}
            sink.update_page(page["id"], update)
    print(f"{drafted} drafted, {failed} failed, {no_contact} without contact data")
    if drafted and not args.yes:
        print("dry run — pass --yes to write to Notion")
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


def cmd_run(args: argparse.Namespace) -> int:
    """collect -> enrich -> score -> sync, chained via the shared args namespace
    (collect stamps args.run for the later stages to read). Drafting is opt-in
    (--draft): the usual flow syncs first, you add SM Audit notes in Notion, then
    `draft-notion` writes the drafts."""
    args.include_synced = False
    # build the sink first: a missing NOTION_TOKEN/database id should fail now,
    # not after collect/enrich/draft have spent time and AI credits
    get_sink(args.sink, load_config(args.config))
    stages = (cmd_collect, cmd_enrich, cmd_score) + (
        (cmd_draft,) if getattr(args, "draft", False) else ()
    ) + (cmd_sync,)
    for stage in stages:
        code = stage(args)
        if code != 0:
            return code
        print()
    return 0


_BATCH_KEYS = {"source", "location", "country", "input", "categories", "limit", "sink", "include_seen", "no_draft", "draft", "bbox", "grid"}


def _load_batch(path: Path) -> list[dict]:
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
    return runs


def _run_args(base: argparse.Namespace, entry: dict) -> argparse.Namespace:
    """Same namespace `run` would get from the CLI, filled from one batch entry."""
    return argparse.Namespace(
        config=base.config,
        source=entry["source"],
        location=entry.get("location"),
        country=entry.get("country"),
        input=entry.get("input"),
        categories=entry.get("categories"),
        limit=entry.get("limit"),
        sink=entry.get("sink", "notion"),
        include_seen=bool(entry.get("include_seen", False)),
        draft=bool(entry.get("draft", False)),
        bbox=entry.get("bbox"),
        no_grid=entry.get("grid") is False,
        refresh_cache=False,
        include_synced=False,
        run=None,
    )


def _describe(entry: dict) -> str:
    return ", ".join(f"{k}={v}" for k, v in entry.items())


def cmd_batch(args: argparse.Namespace) -> int:
    """Run the entries of a batch file one after another (strictly sequential).

    One entry failing doesn't stop the rest. Running out of AI credits does:
    every later draft would fail the same way, so the batch pauses there and
    the next invocation resumes that entry at draft (its collect/enrich/score
    are already done), then carries on. Finished entries are recorded in
    data/batches/<name>.json and skipped on re-runs."""
    batch_path = Path(args.file)
    entries = _load_batch(batch_path)
    state_path = BATCHES_DIR / f"{batch_path.stem}.json"
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}

    def key(entry: dict) -> str:
        return json.dumps(entry, sort_keys=True, ensure_ascii=False)

    def save() -> None:
        BATCHES_DIR.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    todo = [e for e in entries if state.get(key(e), {}).get("status") != "done"]
    print(f"batch {batch_path.name}: {len(todo)} to run, {len(entries) - len(todo)} already done")
    if args.dry_run:
        for i, entry in enumerate(todo, 1):
            paused = state.get(key(entry), {}).get("status") == "paused"
            print(f"  [{i}] {_describe(entry)}" + ("  (resumes at draft)" if paused else ""))
        return 0

    summary: list[tuple[str, str]] = []
    problems = 0
    for i, entry in enumerate(todo, 1):
        entry_state = state.get(key(entry), {})
        run_args = _run_args(args, entry)
        resuming = entry_state.get("status") == "paused" and entry_state.get("run_id")
        print(f"[{i}/{len(todo)}] {_describe(entry)}" + (f"  (resuming run {entry_state['run_id']})" if resuming else ""))
        try:
            if resuming:
                run_args.run = entry_state["run_id"]
                stages = (cmd_draft, cmd_sync)
            else:
                stages = (cmd_collect, cmd_enrich, cmd_score) + (
                    () if entry.get("no_draft")
                    else (cmd_draft, cmd_sync) if entry.get("draft") else (cmd_sync,)
                )
            code = 0
            for stage in stages:
                code = stage(run_args)
                if code != 0:
                    break
                print()
        except (ConfigError, ValueError, RuntimeError) as e:
            print(f"  error: {e}", file=sys.stderr)
            state[key(entry)] = {"status": "failed", "run_id": run_args.run, "error": str(e)}
            save()
            summary.append((_describe(entry), f"FAILED: {e}"))
            problems += 1
            continue
        if code == EXIT_OUT_OF_CREDITS:
            state[key(entry)] = {"status": "paused", "run_id": run_args.run}
            save()
            summary.append((_describe(entry), "PAUSED: out of AI credits"))
            problems += 1
            print(
                f"batch paused at run {run_args.run}: out of AI credits. Nothing after this entry "
                f"was started. Re-run `batch --file {args.file}` once credits are back — it resumes "
                "this entry at draft, then continues.",
                file=sys.stderr,
            )
            break
        status = "done" if code == 0 else "failed"
        state[key(entry)] = {"status": status, "run_id": run_args.run}
        save()
        summary.append((_describe(entry), f"run {run_args.run}: {status}"))
        problems += status == "failed"
        if i < len(todo) and args.pause_s:
            time.sleep(args.pause_s)  # courtesy between runs (Maps / Notion rate limits)

    print("\nbatch summary:")
    for desc, result in summary:
        print(f"  {desc} -> {result}")
    return 1 if problems else 0


def _ask(question: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    while True:
        answer = input(f"{question}{suffix}: ").strip()
        if answer or default:
            return answer or default
        print("  required — please enter a value")


_GO_MODES = ("Lead Gen", "Draft Outreach", "Both")
_DRAFT_SCOPES = (
    "Only pages with an SM Audit note",
    "Also pages without a note (generic praise)",
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
    scope = _ask_choice("Which Notion pages?", _DRAFT_SCOPES, "1")
    raw = _ask("How many at most (a number, or 'all')", str(args.limit) if args.limit else "5")
    limit = None if raw.lower() == "all" else int(raw)
    company = ""
    if limit == 1:
        company = _ask("Which lead (company name, or auto for the next one)", "auto")
        company = "" if company.lower() == "auto" else company
    # dry run first; writing needs an explicit yes, same as `draft-notion`
    base = dict(
        config=args.config, limit=limit, yes=False, company=company,
        all=scope == _DRAFT_SCOPES[1], redraft=scope == _DRAFT_SCOPES[2],
    )
    code = cmd_draft_notion(argparse.Namespace(**base))
    if code != 0 or _ask("Write these drafts to Notion? (y/n)", "n").lower() not in ("y", "yes"):
        return code
    return cmd_draft_notion(argparse.Namespace(**{**base, "yes": True}))


def cmd_go(args: argparse.Namespace) -> int:
    """Interactive entry point: pick what to run, answer the prompts, run it.
    Lead Gen = collect -> enrich -> score -> sync; Draft Outreach = draft-notion;
    Both = Lead Gen with the draft stage before sync (no notes, no pauses).
    Defaults come from config.yaml. --source maps/osm/notion skips the menu."""
    config = load_config(args.config)
    collect_cfg = config["collect"]
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

    if not source:
        source = _ask("Source (maps / osm)", "maps").lower()
    if source not in ("maps", "osm"):
        raise ValueError(f"unknown source {source!r} — choose maps or osm")
    location = _ask("Scrape location", collect_cfg["location"])
    raw = _ask("Category/categories (comma-separated)", ",".join(collect_cfg["categories"]))
    categories = ",".join(c.strip() for c in raw.split(",") if c.strip())
    print(f"\n  mode: {mode}\n  source: {source}\n  location: {location}\n  categories: {categories}")
    if mode == "Both":
        print("  drafts are made right away, without SM Audit notes (generic praise);"
              " add notes later and redraft with Draft Outreach")
    bbox = resolve_bbox({"location": location}, config["maps"]["areas"]) if source == "maps" else None
    if bbox:
        cells = grid_cell_count(parse_bbox(bbox), config["maps"]["grid_cell_km"])
        n = len([c for c in categories.split(",") if c])
        print(f"  grid: ~{cells} cells x {n} categories (longer run, many more leads)")
    if _ask("Start? (y/n)", "y").lower() not in ("y", "yes"):
        print("cancelled")
        return 0
    print()
    return cmd_run(
        argparse.Namespace(
            config=args.config,
            source=source,
            location=location,
            country=None,
            input=None,
            categories=categories,
            limit=args.limit,
            sink="notion",
            include_seen=False,
            draft=mode == "Both",
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
    collect.add_argument("--location", help='OSM area name, e.g. "Berlin"')
    collect.add_argument(
        "--country", help='qualification-filter country, e.g. "Serbia" (default from config.yaml)'
    )
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
    enrich.set_defaults(func=cmd_enrich)

    score_cmd = sub.add_parser(
        "score", help="apply scoring weights from config.yaml to an enriched run"
    )
    score_cmd.add_argument("--run", required=True, help="run id under data/runs/")
    score_cmd.add_argument(
        "--limit", type=int, help="score only the first N leads — dry-run"
    )
    score_cmd.set_defaults(func=cmd_score)

    draft_cmd = sub.add_parser(
        "draft", help="draft outreach copy for qualified scored leads (via the claude CLI)"
    )
    draft_cmd.add_argument("--run", required=True, help="run id under data/runs/")
    draft_cmd.add_argument(
        "--limit", type=int, help="draft at most N leads — dry-run"
    )
    draft_cmd.add_argument(
        "--include-synced",
        action="store_true",
        help="also draft leads already pushed to Notion (to backfill existing pages)",
    )
    draft_cmd.set_defaults(func=cmd_draft)

    dn_cmd = sub.add_parser(
        "draft-notion",
        help="draft for Notion pages with an SM Audit note and no draft yet",
    )
    dn_cmd.add_argument(
        "--company", help="draft only pages whose Company contains this text"
    )
    dn_cmd.add_argument(
        "--redraft", action="store_true", help="also rewrite pages that already have a draft"
    )
    dn_cmd.add_argument(
        "--all", action="store_true", help="also draft pages without an SM Audit note"
    )
    dn_cmd.add_argument("--limit", type=int, help="draft at most N pages")
    dn_cmd.add_argument("--yes", action="store_true", help="write to Notion (default: dry run)")
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
        "--refresh-cache",
        action="store_true",
        help="rebuild seen_domains.json from Notion, then exit",
    )
    sync_cmd.set_defaults(func=cmd_sync)

    run_cmd = sub.add_parser(
        "run", help="collect, enrich, score, and sync in one go"
    )
    run_cmd.add_argument(
        "--source",
        required=True,
        help='comma-separated: "osm", "maps", "manual" — e.g. "osm,manual"',
    )
    run_cmd.add_argument("--location", help='OSM area name, e.g. "Berlin"')
    run_cmd.add_argument(
        "--country", help='qualification-filter country, e.g. "Serbia" (default from config.yaml)'
    )
    run_cmd.add_argument(
        "--input", help="manual collector: path to the hand-gathered leads CSV"
    )
    run_cmd.add_argument(
        "--categories",
        help='comma-separated, e.g. "restaurant,hairdresser" or raw "craft=roofer"',
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
        "--draft",
        action="store_true",
        help="also draft messages before sync (default: sync only, draft later with draft-notion)",
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
        "go", help="interactive run: prompts for location and categories"
    )
    go_cmd.add_argument("--source", help="skip the menu: maps / osm (Lead Gen) or notion (Draft Outreach)")
    go_cmd.add_argument("--limit", type=int, help="dry-run: cap leads at N")
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
    except DraftBudgetError as e:  # draft-notion; cmd_draft handles its own
        print(_out_of_credits_message(str(e)), file=sys.stderr)
        return EXIT_OUT_OF_CREDITS
    except (ConfigError, ValueError, RuntimeError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
