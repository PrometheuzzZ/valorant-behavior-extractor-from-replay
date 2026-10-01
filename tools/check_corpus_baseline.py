#!/usr/bin/env python3
"""Pin a corpus's oracle numbers and fail when they drift.

validate_corpus.py prints what a corpus does; this pins its per-file and total
figures in a JSON baseline and exits 1 on any difference. A relative corpus
path resolves against VRFKIT_CORPUS_DIR; a missing corpus is SKIPPED unless
--require-input or VRFKIT_REQUIRE_CORPUS is set.

Usage:
    python tools/check_corpus_baseline.py --baseline tools/baselines/build_1302.json
    python tools/check_corpus_baseline.py --baseline <path> --update
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from validate_corpus import _run_one, parse_oracle_output  # noqa: E402
from corpus_scan import find_replays  # noqa: E402
import summary_counters as sc  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
DEFAULT_EXE = REPO / "target" / "release" / "vrfkit.exe"


def measure(exe: Path, root: Path) -> dict:
    """Run the oracle over every .vrf under root and collect the numbers."""
    files = find_replays(root, recursive=True)
    per_file = {}
    totals = {"blocks": 0, "fields": 0, "rpcs": 0, "malformed": 0, "skipped": 0}
    branches = {}

    for f in files:
        name = f.relative_to(root).as_posix()
        error, out = _run_one(exe, f)
        if error is None:
            got, error = parse_oracle_output(out)
        if error is not None:
            per_file[name] = {"error": error}
            continue
        branch = got["branch"].group(1)
        branches[branch] = branches.get(branch, 0) + 1
        entry = {"branch": branch, "rate": got["rate"].group(1)}
        for key in totals:
            match = got.get(key)
            if match is None:
                entry[key] = None  # never 0: a total would hide it
                continue
            value = int(match.group(1))
            entry[key] = value
            totals[key] += value
        per_file[name] = entry

    return {"branches": branches, "totals": totals, "per_file": per_file}


def unpinnable(current: dict) -> list[str]:
    """Why this run must not become a baseline: a replay the oracle could not
    validate, or a counter it did not print."""
    reasons = []
    if not current["per_file"]:
        return ["no replay produced any numbers"]
    for name in sorted(current["per_file"]):
        entry = current["per_file"][name]
        if "error" in entry:
            reasons.append(f"{name}: the oracle failed ({entry['error']})")
            continue
        for key in sorted(k for k, v in entry.items() if v is None):
            reasons.append(f"{name}: the oracle did not print {key}")
    return reasons


def diff(baseline: dict, current: dict) -> list[str]:
    """Every way the two disagree, as human-readable lines."""
    out = []
    for key, want in baseline["totals"].items():
        got = current["totals"].get(key)
        if got != want:
            out.append(f"total {key}: {got} (baseline {want})")
    if baseline["branches"] != current["branches"]:
        out.append(f"branches: {current['branches']} (baseline {baseline['branches']})")

    b_files, c_files = set(baseline["per_file"]), set(current["per_file"])
    for name in sorted(b_files - c_files):
        out.append(f"missing replay: {name}")
    for name in sorted(c_files - b_files):
        out.append(f"replay not in baseline: {name}")
    for name in sorted(b_files & c_files):
        want, got = baseline["per_file"][name], current["per_file"][name]
        for key in sorted(set(want) | set(got)):
            if want.get(key) != got.get(key):
                out.append(f"{name} {key}: {got.get(key)} (baseline {want.get(key)})")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--baseline", type=Path, required=True)
    ap.add_argument("--exe", type=Path, default=DEFAULT_EXE)
    ap.add_argument("--corpus", type=Path, default=None,
                    help="overrides the corpus path stored in the baseline")
    ap.add_argument("--update", action="store_true",
                    help="rewrite the baseline from the current numbers")
    ap.add_argument("--require-input", action="store_true",
                    help="fail instead of skipping when the corpus is absent/empty")
    args = ap.parse_args()

    if sc.no_exe(args.exe):
        return 2
    stored = sc.load_baseline(args.baseline)
    refusal = args.update and sc.machine_path(args.corpus, stored.get("corpus"), "corpus",
                                              args.baseline)
    if refusal:
        print(refusal, file=sys.stderr)
        return 2
    corpus = sc.baseline_input(args.corpus, stored.get("corpus", ""))
    if corpus is None or not corpus.exists():
        return sc.missing_input("no corpus named (pass --corpus or store one in the baseline)"
                                if corpus is None else f"corpus not present ({corpus})",
                                args.require_input)
    current = measure(args.exe, corpus)
    if not current["per_file"]:
        return sc.missing_input(f"no .vrf under {corpus}", args.require_input)

    pinned = {"corpus": stored.get("corpus") or str(args.corpus), **current}
    verdict = sc.pin_or_diff(args.baseline, stored, pinned, args.update,
                             unpinnable(current), diff)
    if verdict is not None:
        return verdict
    print(f"OK: {len(current['per_file'])} replays match the baseline "
          f"(branches {current['branches']}, malformed {current['totals']['malformed']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
