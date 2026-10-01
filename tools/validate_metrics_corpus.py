#!/usr/bin/env python3
"""Reproduce metrics.json for every replay that has a reference bundle.

Runs check_metrics_baseline.py's pipeline (vrfkit export, the valplay adapter,
compute_metrics.py) on each replay with BOTH a source .vrf and a reference
metrics.json under VRFKIT_VALPLAY_DIR, then diffs them section by section: a
section EXACT on one replay and different on ten is not EXACT, it is lucky.
Nothing under valplay/ is written; outputs go to out/xval/<id>/ and
out/xval_bundle/<id>/, and the sections exact on every replay to
out/xval_summary.json, which `--expect-exact` reads back to fail a run where
one of them no longer is.

Usage:
    python tools/validate_metrics_corpus.py [--limit N] [--only <id>]
                                            [--jobs N] [--expect-exact FILE]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import check_metrics_baseline as cmb
from atomic_io import atomic_write_text, remove_tree, require_descendant

REPO = Path(__file__).resolve().parent.parent
VALPLAY = Path(os.environ.get("VRFKIT_VALPLAY_DIR", ""))
EXPORTS = VALPLAY / "pipeline" / "exports"
VRF_DIR = VALPLAY / "data" / "raw" / "vrf"
VRFKIT = REPO / "target" / "release" / "vrfkit.exe"

# Present in metrics.json but not a metric: provenance that necessarily differs
# because the two bundles live at different paths.
NON_METRIC_KEYS = {"source"}
REPLAY_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")


def discover():
    """Replay ids that have both a reference metrics.json and a source .vrf."""
    if not EXPORTS.is_dir():
        print(f"set VRFKIT_VALPLAY_DIR to the valplay checkout root; "
              f"exports dir not found at {EXPORTS}", file=sys.stderr)
        return []
    if not VRF_DIR.is_dir():
        print(f"set VRFKIT_VALPLAY_DIR to the valplay checkout root; "
              f"corpus dir not found at {VRF_DIR}", file=sys.stderr)
        return []
    have_vrf = {p.stem for p in VRF_DIR.glob("*.vrf")}
    out = []
    for d in sorted(EXPORTS.iterdir()):
        if d.is_dir() and (d / "metrics.json").exists() and d.name in have_vrf:
            out.append(d.name)
    return out


def fresh_dir(path: Path, root: Path | None = None) -> Path:
    """Delete `path` and recreate it empty: these directories persist between
    runs, and a stale metrics.json read after a compute_metrics that wrote
    nothing would compare EXACT against its own reference."""
    root = root or path.parent
    remove_tree(path, root)  # refuses a path outside `root` before deleting
    path.mkdir(parents=True, exist_ok=True)
    return path


def failures(results: list[dict]) -> list[str]:
    """Replays that did not complete the pipeline, as readable lines; any one
    fails the run, however many others finished."""
    return [f"{r['id']}: failed at {r['stage']} -- {str(r.get('error', ''))[:160]}"
            for r in results if r["stage"] != "ok"]


def process(replay_id: str) -> dict:
    """Export, adapt and compute metrics for one replay. Returns a result dict."""
    t0 = time.time()
    if not isinstance(replay_id, str) or not REPLAY_ID_RE.fullmatch(replay_id):
        return {"id": str(replay_id), "stage": "input", "error": "invalid replay id"}

    source = VRF_DIR / f"{replay_id}.vrf"
    reference = EXPORTS / replay_id / "metrics.json"
    if not source.is_file():
        return {"id": replay_id, "stage": "input", "error": f"missing replay: {source}"}
    if not reference.is_file():
        return {"id": replay_id, "stage": "input", "error": f"missing reference: {reference}"}
    try:
        ref = json.loads(reference.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return {"id": replay_id, "stage": "input", "error": f"invalid reference: {exc}"}
    if not isinstance(ref, dict):
        return {"id": replay_id, "stage": "input", "error": "reference is not an object"}

    export_root = REPO / "out" / "xval"
    bundle_parent = REPO / "out" / "xval_bundle"
    export_dir = export_root / replay_id
    bundle_root = bundle_parent / replay_id
    try:
        require_descendant(export_dir, export_root)
        require_descendant(bundle_root, bundle_parent)
        fresh_dir(export_dir, export_root)
        fresh_dir(bundle_root, bundle_parent)
    except (OSError, ValueError) as exc:
        return {"id": replay_id, "stage": "input", "error": str(exc)}

    ours, stage, error = cmb.run_pipeline(VRFKIT, source, export_dir, bundle_root)
    if ours is None:
        return {"id": replay_id, "stage": stage, "error": error}
    sections = sorted((set(ours) | set(ref)) - NON_METRIC_KEYS)
    status = {s: ("EXACT" if ours.get(s) == ref.get(s) else "differs") for s in sections}
    return {
        "id": replay_id,
        "stage": "ok",
        "elapsed_s": round(time.time() - t0, 1),
        "sections": status,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--only", action="append", default=None)
    ap.add_argument("--jobs", type=int, default=3,
                    help="parallel replays; each uses ~2 GB, so keep it small")
    ap.add_argument("--expect-exact", type=Path, default=None,
                    help="a JSON with an always_exact list (a pinned xval_summary.json): "
                         "fail when one of its sections is not EXACT on every replay")
    args = ap.parse_args()

    expected = []
    if args.expect_exact:
        try:
            expected = json.loads(args.expect_exact.read_text(encoding="utf-8"))["always_exact"]
            if not isinstance(expected, list):
                raise TypeError("always_exact is not a list")
        except (OSError, ValueError, KeyError, TypeError) as exc:
            print(f"--expect-exact {args.expect_exact}: {exc!r}", file=sys.stderr)
            return 2
    if cmb.sc.no_exe(VRFKIT):
        return 2

    ids = args.only or discover()
    if args.limit:
        ids = ids[: args.limit]
    if not ids:
        print("no replays with both a reference bundle and a source .vrf",
              file=sys.stderr)
        return 2

    print(f"cross-validating {len(ids)} replays with {args.jobs} workers")
    results = []
    with ProcessPoolExecutor(max_workers=args.jobs) as pool:
        futures = {pool.submit(process, r): r for r in ids}
        for fut in as_completed(futures):
            res = fut.result()
            results.append(res)
            if res["stage"] != "ok":
                print(f"  {res['id']}  FAILED at {res['stage']}: {res['error'][:160]}")
            else:
                n_exact = sum(1 for v in res["sections"].values() if v == "EXACT")
                print(f"  {res['id']}  {n_exact}/{len(res['sections'])} exact"
                      f"  ({res['elapsed_s']}s)")

    ok = [r for r in results if r["stage"] == "ok"]
    if not ok:
        print("\nno replay completed", file=sys.stderr)
        return 1

    all_sections = sorted({s for r in ok for s in r["sections"]})
    order = sorted(ok, key=lambda r: r["id"])

    print()
    print(f"{'section':18s} " + " ".join(r["id"][:8] for r in order) + "   all-exact")
    print("-" * (19 + 9 * len(order) + 12))
    always = []
    for s in all_sections:
        marks = []
        for r in order:
            v = r["sections"].get(s)
            marks.append("   ok    " if v == "EXACT" else ("   --    " if v else "   ?     "))
        every = all(r["sections"].get(s) == "EXACT" for r in order)
        if every:
            always.append(s)
        print(f"{s:18s} " + "".join(marks) + ("   YES" if every else "   no"))

    print()
    print(f"replays compared        : {len(ok)} of {len(ids)}")
    print(f"sections exact on ALL {len(ok):>2}: {len(always)} / {len(all_sections)}")
    print(f"  {', '.join(always) if always else '(none)'}")
    if len(ok) < 2:
        # "EXACT on all" over one replay is the claim this tool exists to
        # test, not evidence for it.
        print("  NOTE: 'ALL' is one replay here; that is the single-replay "
              "claim this run was supposed to generalise.")

    summary = REPO / "out" / "xval_summary.json"
    atomic_write_text(summary, json.dumps(
        {"replays": order, "always_exact": always}, indent=1))
    print(f"\nwrote {summary}")

    lost = sorted(set(expected) - set(always))
    if args.expect_exact:
        print(f"expected exact on every replay: {len(expected)}, not exact now: {len(lost)}")
    dead = failures(results)
    if dead:
        print(f"\nFAILED: {len(dead)} of {len(ids)} replay(s) did not complete "
              f"the pipeline", file=sys.stderr)
        for line in dead[:15]:
            print(f"    {line}", file=sys.stderr)
    if lost:
        print(f"\nFAILED: expected EXACT on every replay, and not: {', '.join(lost)}",
              file=sys.stderr)
    return 1 if dead or lost else 0


if __name__ == "__main__":
    raise SystemExit(main())
