"""Assert that each BUILD's preserved replay still produces sane MATCH METRICS.

Framing counters cannot see a decoder that stops producing values: 13.02
moved `RoundResults` from handle 93 to 81, the match score stopped being
written, and every framing guard stayed green. This runs the layer where rows
become a scoreboard:

    vrfkit export -> tools/to_valplay_bundle.py -> valplay compute_metrics.py

**Invariants** (`invariants`) hold for any replay of any build and need no
baseline; they survive legitimate changes that move counts. **Pinned values**
catch drift in everything else, all builds in ONE file, so a build leaving the
set is itself a failure.

`kills == deaths` is NOT an invariant: a resurrected player who dies again in
the same round is two `bDied` reports but one DidKill per (round, subject).
Across five Swiftplay replays the gap was exactly 1 in each of the two with a
resurrection, 0 otherwise.

The slowest check here: five full 44-65 MB matches plus three sub-MB public
fixtures. Run it after a non-trivial change, not in a fast sweep.

Usage:
    python tools/check_metrics_baseline.py
    python tools/check_metrics_baseline.py --update
    python tools/check_metrics_baseline.py --only 13.02 --jobs 1
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import summary_counters as sc
from atomic_io import atomic_write_text

REPO = Path(__file__).resolve().parent.parent
DEFAULT_EXE = REPO / "target" / "release" / "vrfkit.exe"
BUNDLE_TOOL = REPO / "tools" / "to_valplay_bundle.py"
DEFAULT_BASELINE = REPO / "tools" / "baselines" / "metrics_builds.json"

#: valplay is never modified, only invoked; VRFKIT_VALPLAY_DIR is its checkout.
COMPUTE_METRICS = Path(
    os.environ.get("VRFKIT_VALPLAY_DIR", "")
) / "pipeline" / "metrics" / "compute_metrics.py"

#: One replay per build, as the baseline pins it (check_baseline_schemas.py
#: checks each against its build's corpus). Never the game's own Saved\Demos:
#: the game rotates it.
REPLAYS = json.loads(DEFAULT_BASELINE.read_text(encoding="utf-8"))["replays"]


def run_pipeline(exe: Path, replay: Path, export_dir: Path, bundle_dir: Path,
                 metrics_out: Path | None = None) -> tuple[dict | None, str, str]:
    """export -> bundle -> compute_metrics: `(metrics.json, "ok", "")`, or
    `(None, stage, why)` for the stage that failed."""
    metrics = metrics_out or bundle_dir / "metrics.json"
    for stage, cmd in (
            ("export", [exe, "export", replay, "--out", export_dir]),
            ("bundle", [sys.executable, BUNDLE_TOOL, export_dir, "-o", bundle_dir]),
            ("metrics", [sys.executable, COMPUTE_METRICS, bundle_dir,
                         *(("-o", metrics_out) if metrics_out else ())])):
        try:
            code, text = sc.run(cmd, timeout=1800)
        except subprocess.TimeoutExpired:
            return None, stage, "timeout after 1800 seconds"
        except OSError as exc:
            return None, stage, f"could not start process: {exc}"
        if code != 0:
            return None, stage, f"rc={code}: {sc.tail(text, 3, 300) or 'no output'}"
    try:
        return json.loads(metrics.read_text(encoding="utf-8")), "ok", ""
    except (OSError, ValueError) as exc:
        return None, "metrics", f"metrics.json: {exc}"


def _sum(d: dict, field: str) -> int:
    """One per-player counter summed over `d`'s players. A player without the
    key, or with a value that is not a count, raises: a counter valplay
    renamed must not read as a plausible 0."""
    total = 0
    for player, values in d.items():
        if field not in values:
            raise ValueError(f"per-player counter {field!r} is missing for player {player}")
        value = values[field]
        if type(value) is not int:
            raise ValueError(f"per-player counter {field!r} of player {player} is "
                             f"{value!r}, not a count")
        total += value
    return total


def extract(m: dict) -> dict:
    """The pinned figures, all of them semantic rather than framing."""
    combat = m["combat"]["per_player"]
    tac = m["tactical"]["per_player"]
    return {
        "rounds_rpc": m["rounds"]["round_count"],
        "rounds_objective": m["objective"]["round_count"],
        "client_round_starts": m["rounds"]["client_round_start_events"],
        "team_score": m["objective"]["team_score"],
        "plants": m["objective_detail"]["plant_count"],
        "defuses": m["objective_detail"]["defuse_count"],
        "players": len(m["players"]),
        "combat_players": len(combat),
        "kills": _sum(combat, "kills"),
        "deaths": _sum(combat, "deaths"),
        "assists": _sum(combat, "assists"),
        "headshots": _sum(combat, "headshots"),
        "damage_dealt": round(sum(p["damage_dealt"] for p in combat.values()), 2),
        "first_bloods": _sum(tac, "first_bloods"),
        "trade_kills": _sum(tac, "trade_kills"),
        "kast_rounds": _sum(m["kast"]["per_player"], "kast_rounds"),
        "ultimate_casts": m["ultimate"]["total_casts"],
        "distinct_weapons": m["weapons"]["distinct_weapons"],
        "shots": sum(m["weapons"]["shots_by_weapon"].values()),
        "shot_rays": m["shot_rays"]["ray_count"],
        "ability_spawns": m["ability_usage"]["ability_spawn_count"],
        "movement_samples": m["movement_summary"]["movement_samples"],
        "economy_rounds": m["economy_detail"]["rounds"],
    }


def invariants(v: dict) -> list[str]:
    """Checks that need no baseline, one message per check that fails. R1 and
    R2 catch the 13.02 RoundResults shift (objective 0 vs rpc 21); R3, a
    round with no recorded winner, cannot."""
    bad = []
    if v["rounds_objective"] <= 0:
        bad.append(
            f"R1 objective.round_count is {v['rounds_objective']}: the "
            f"BombGameState round results produced nothing. This is the exact "
            f"shape of the 13.02 RoundResults handle shift."
        )
    if v["rounds_rpc"] != v["rounds_objective"]:
        bad.append(
            f"R2 round count disagrees between its two independent sources: "
            f"ClientRoundStart RPCs say {v['rounds_rpc']}, BombGameState "
            f"RoundResults say {v['rounds_objective']}."
        )
    score_total = sum(v["team_score"].values())
    if score_total != v["rounds_objective"]:
        bad.append(
            f"R3 team_score sums to {score_total} but there are "
            f"{v['rounds_objective']} rounds: every round has exactly one "
            f"winner, so these cannot differ."
        )
    if v["players"] <= 0:
        bad.append("R4 no players were identified at all.")
    if v["kills"] > 0 and v["damage_dealt"] <= 0:
        bad.append(
            f"R5 {v['kills']} kills but zero damage: the combat report "
            f"stopped decoding while the kill timeline kept working."
        )
    return bad


def invariant_count() -> int:
    """How many checks `invariants()` runs: the distinct `R<n>` labels in its
    source, so the pass message cannot state a stale literal."""
    import inspect
    return len(set(re.findall(r"\bR\d+\b", inspect.getsource(invariants))))


def run_one(build: str, replay: Path, exe: Path) -> tuple[str, dict | None, str]:
    """`(build, extract(metrics), error)` from the pipeline, run in sibling
    scratch directories (the adapter's input and output never overlap) that
    are removed afterwards."""
    if not replay.is_file():
        return build, None, f"replay not found: {replay}"
    out = Path(tempfile.mkdtemp(prefix=f"vrfkit-metrics-{build.replace('.', '_')}-"))
    try:
        metrics, stage, why = run_pipeline(exe, replay, out / "export", out / "bundle",
                                           out / "metrics.json")
        if metrics is None:
            return build, None, f"{stage} failed: {why}"
        return build, extract(metrics), ""
    except ValueError as exc:
        return build, None, f"metrics.json: {exc}"
    finally:
        shutil.rmtree(out, ignore_errors=True)


def merged_metrics(stored: dict, fresh: dict, only) -> dict:
    """The metrics `--update` writes. A scoped run (`--only 13.02`) keeps the
    builds it did not look at; only an unscoped one may retire a build, or a
    build that left REPLAYS stays pinned and every later run fails on it."""
    return dict(fresh) if only is None else {**stored, **fresh}


def compare(build: str, got: dict, want: dict) -> list[str]:
    drift = []
    for key in sorted(set(got) | set(want)):
        a, b = got.get(key, "<absent>"), want.get(key, "<absent>")
        if a != b:
            drift.append(f"{build} {key}: got {a}, baseline {b}")
    return drift


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    ap.add_argument("--exe", type=Path, default=DEFAULT_EXE)
    ap.add_argument("--jobs", type=int, default=3)
    ap.add_argument("--only", action="append", default=None,
                    help="limit to these builds (repeatable)")
    ap.add_argument("--update", action="store_true",
                    help="rewrite the baseline from this run")
    args = ap.parse_args()

    if not args.exe.is_file():
        print(f"executable not found: {args.exe}\n"
              f"build it with: cargo build --release -p vrfkit", file=sys.stderr)
        return 2
    if not COMPUTE_METRICS.is_file():
        print(f"valplay compute_metrics not found: {COMPUTE_METRICS}\n"
              f"set VRFKIT_VALPLAY_DIR to the valplay checkout root",
              file=sys.stderr)
        return 2

    builds = {k: v for k, v in REPLAYS.items()
              if args.only is None or k in args.only}
    if not builds:
        print(f"no builds selected; known: {sorted(REPLAYS)}", file=sys.stderr)
        return 2

    print(f"running export -> bundle -> compute_metrics on {len(builds)} build(s), "
          f"{args.jobs}-wide")
    started = time.time()
    results, failures = {}, []
    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        for build, values, err in pool.map(
            lambda kv: run_one(kv[0], sc.baseline_input(None, kv[1]), args.exe),
            sorted(builds.items()),
        ):
            if values is None:
                failures.append(f"{build}: {err}")
                print(f"  {build}  PIPELINE FAILED  {err}")
            else:
                results[build] = values
                print(f"  {build}  rounds={values['rounds_objective']:>3} "
                      f"score={values['team_score']} "
                      f"players={values['players']:>2} "
                      f"kills={values['kills']:>4}")
    elapsed = time.time() - started
    print(f"elapsed {elapsed:.1f}s")

    # Invariants first: they are the point, and they apply even with --update.
    broken = []
    for build, values in sorted(results.items()):
        for msg in invariants(values):
            broken.append(f"{build}: {msg}")

    if broken:
        print(f"\nFAILED: {len(broken)} invariant violation(s)", file=sys.stderr)
        for msg in broken:
            print(f"    {msg}", file=sys.stderr)
        if args.update:
            print("  baseline NOT updated -- refusing to pin a broken run.",
                  file=sys.stderr)
        return 1
    if failures:
        print(f"\nFAILED: {len(failures)} build(s) did not complete the pipeline",
              file=sys.stderr)
        for msg in failures:
            print(f"    {msg}", file=sys.stderr)
        return 1

    if args.update:
        stored = json.loads(args.baseline.read_text(encoding="utf-8")) \
            if args.baseline.is_file() else {}
        metrics = merged_metrics(stored.get("metrics", {}), results, args.only)
        payload = {
            "note": "Semantic metrics per build. See the module docstring: "
                    "framing counters cannot see a decoder that stops "
                    "producing values.",
            "replays": REPLAYS,
            "metrics": metrics,
        }
        atomic_write_text(
            args.baseline, json.dumps(payload, indent=1, sort_keys=True) + "\n")
        kept = sorted(set(metrics) - set(results))
        print(f"\nwrote {args.baseline}: re-pinned {len(results)} build(s)"
              + (f", kept {len(kept)} not looked at ({', '.join(kept)})"
                 if kept else ""))
        return 0

    if not args.baseline.is_file():
        print(f"\nbaseline not found: {args.baseline}\n"
              f"create it with --update", file=sys.stderr)
        return 2
    baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
    want = baseline.get("metrics", {})

    drift = []
    for build in sorted(results):
        if build not in want:
            drift.append(f"{build}: present in this run, absent from the baseline")
        else:
            drift.extend(compare(build, results[build], want[build]))
    for build in sorted(want):
        if build not in results and (args.only is None or build in args.only):
            drift.append(f"{build}: in the baseline, MISSING from this run")

    if drift:
        print(f"\nFAILED: {len(drift)} metric(s) drifted from "
              f"{args.baseline.name}", file=sys.stderr)
        for msg in drift:
            print(f"    {msg}", file=sys.stderr)
        print("  If the change is intended, re-pin with --update.", file=sys.stderr)
        return 1

    n_inv = len(results) * invariant_count()
    print(f"\nOK: {len(results)} build(s) pass {n_inv} invariant checks and match "
          f"{sum(len(v) for v in results.values())} pinned metric values")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
