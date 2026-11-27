"""Scorer v3 vs v4 side by side on one run — read-only, writes nothing under data/.

Scores the run's enriched.jsonl with both formulas and prints each top N, with
the lead's rank under the other formula. v3 (scorer + weights) is loaded from
git at V3_COMMIT, the last commit before scorer v4, so no copy of the old code
lives in the repo. v4 uses the current code and config.yaml. The run's niche
comes from its manifest when it has one (runs before markets/niches have none:
niche match is then "unknown").

Run `enrich --only audit --rescan --run <id>` first on runs enriched before
schema v11, or v4 scores audited leads without the probe signals.

Usage:
    .venv/bin/python scripts/compare_scorers.py <run_id> <market> [top_n]
    .venv/bin/python scripts/compare_scorers.py 2026-10-20T00-08-14 serbia 20
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import types
from collections import Counter
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pipeline.config import load_config  # noqa: E402
from pipeline.market import load_market  # noqa: E402
from pipeline.niche import Niche  # noqa: E402
from pipeline.schema import read_jsonl  # noqa: E402
from pipeline.scoring import score as score_v4  # noqa: E402

V3_COMMIT = "827ec5c"  # last commit with scorer v3 (merge of v2 step 4)


def _module_from_git(name: str, path: str):
    """Import a file's content at V3_COMMIT as a throwaway module (never written to disk)."""
    source = subprocess.run(
        ["git", "show", f"{V3_COMMIT}:{path}"], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout
    module = types.ModuleType(name)
    module.__file__ = f"<git {V3_COMMIT}:{path}>"
    sys.modules[name] = module  # dataclass/annotation lookups need it registered
    exec(compile(source, module.__file__, "exec"), module.__dict__)
    return module


def _ranks(leads, key) -> dict[str, int]:
    ordered = sorted((l for l in leads if key(l) is not None), key=lambda l: -key(l))
    return {_id(l): i for i, l in enumerate(ordered, 1)}


def _id(lead) -> str:
    return lead.domain or f"company:{lead.company.strip().lower()}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("run", help="run id under data/runs/")
    parser.add_argument("market", help="market preset id from config.yaml, e.g. serbia")
    parser.add_argument("top", nargs="?", type=int, default=20, help="how many to list (default 20)")
    args = parser.parse_args(argv)

    run_dir = ROOT / "data" / "runs" / args.run
    enriched_path = run_dir / "enriched.jsonl"
    if not enriched_path.exists():
        print(f"no enriched.jsonl in {run_dir}", file=sys.stderr)
        return 1
    config = load_config(ROOT / "config.yaml")
    market = load_market(config, args.market)
    manifest_path = run_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}
    niche = Niche.from_dict(manifest["niche"]) if manifest.get("niche") else None

    v3_scorer = _module_from_git("scorer_v3", "pipeline/scoring/scorer.py")
    v3_weights = _module_from_git("config_v3", "pipeline/config.py").DEFAULTS["scoring"]

    leads = [l for l in read_jsonl(enriched_path) if l.status in ("enriched", "enrich_failed")]
    v3 = [v3_scorer.score(l, v3_weights, {"target_countries": [market.country]}) for l in leads]
    v4 = [score_v4(l, config["scoring"], market, niche, datetime.now().year) for l in leads]
    rank3 = _ranks(v3, lambda l: l.total_score)
    rank4 = _ranks(v4, lambda l: l.priority)

    tiers = Counter(l.tier for l in v4 if l.tier)
    gated = Counter(l.disqualify_reason for l in v4 if l.disqualify_reason)
    signals = sum(l.site_reachable is not None for l in leads if l.domain)
    print(
        f"{args.run} · market {market.id} · niche {niche.name if niche else 'none'} · {len(leads)} leads\n"
        f"v4 tiers {' '.join(f'{t}:{tiers.get(t, 0)}' for t in 'ABCD')} · "
        f"disqualified {sum(gated.values())} {dict(gated) if gated else ''} · "
        f"low confidence {sum(l.score_confidence == 'low' for l in v4)} · "
        f"leads with a domain and audit signals: {signals}/{sum(bool(l.domain) for l in leads)}"
    )

    print(f"\n== v3 top {args.top} (total_score) ==")
    for l in sorted((l for l in v3 if l.total_score is not None), key=lambda l: -l.total_score)[: args.top]:
        print(
            f"{rank3[_id(l)]:3}. {l.total_score:5.1f}  {l.company[:40]:40} {l.lead_type:9}  "
            f"v4 #{rank4.get(_id(l), '-')}"
        )

    print(f"\n== v4 top {args.top} (priority) ==")
    for l in sorted((l for l in v4 if l.priority is not None), key=lambda l: -l.priority)[: args.top]:
        print(
            f"{rank4[_id(l)]:3}. {l.tier} {l.priority:5.1f}  N{l.need_score:3.0f} V{l.viability_score:3.0f} "
            f"R{l.reach_score:3.0f}  {l.company[:36]:36} v3 #{rank3.get(_id(l), '-')}\n"
            f"       {' · '.join(l.score_reasons)}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
