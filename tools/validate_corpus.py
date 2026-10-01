"""Run the grammar oracle across a replay corpus and summarise.

No file may crash, and every file must land at essentially the same oracle
pass rate: a file that drops means a mis-detected build or an unseen stream
shape. Lists every replay that still skips bits, most first.

One vrfkit process per replay, VRFKIT_JOBS at a time (default cores - 2, at
most 16); within a replay content blocks are order-dependent. Discovery is
corpus_scan.py's: top level unless `--recursive`.

Usage:
    python tools/validate_corpus.py <vrfkit.exe> <dir-with-vrf-files> [limit]
    python tools/validate_corpus.py <vrfkit.exe> <dir-with-vrf-files> --recursive
"""

from __future__ import annotations

import argparse
import collections
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import corpus_scan
import summary_counters as sc

PATTERNS = {
    "branch": re.compile(r"Branch:\s+(\S+)"),
    "blocks": re.compile(r"Total content blocks:\s+(\d+)"),
    "malformed": re.compile(r"Malformed framing:\s+(\d+)"),
    "skipped": re.compile(r"Skipped bits:\s+(\d+)"),
    "rate": re.compile(r"ORACLE PASS RATE:\s+([\d.]+)%"),
    "fields": re.compile(r"Fields emitted:\s+(\d+)"),
    "rpcs": re.compile(r"RPCs emitted:\s+(\d+)"),
}


def parse_oracle_output(output: str):
    """Parse one oracle summary, requiring identity and pass-rate lines."""
    matches = {key: pattern.search(output) for key, pattern in PATTERNS.items()}
    if matches["branch"] is None:
        return None, "the oracle did not print Branch"
    if matches["rate"] is None:
        return None, "the oracle did not print ORACLE PASS RATE"
    return matches, None


def problems(failures, missing) -> list[str]:
    """What makes this sweep a failure: a replay the oracle could not
    validate, or a counter it stopped printing (never read as 0). Pass rates
    are not gated; check_corpus_baseline.py pins each replay's."""
    out = [f"{name}: {why}" for name, why in failures]
    out += [f"the oracle did not print '{key}' on {count} replay(s), so the "
            f"corpus total for it is summed over the rest"
            for key, count in sorted(missing.items())]
    return out


def _run_one(exe: Path, path: Path) -> tuple[str | None, str]:
    """Validate one replay: `(error or None, stdout + stderr)`."""
    try:
        code, out = sc.vrfkit(exe, "validate", path, timeout=300)
    except subprocess.TimeoutExpired:
        return "timeout", ""
    except OSError as exc:
        return f"could not start oracle: {exc}", ""
    if code != 0:
        return f"exit {code}: {sc.tail(out, 3, 160)}", out
    return None, out


def parse_args(argv: list[str]) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("exe", type=Path)
    ap.add_argument("corpus", type=Path)
    ap.add_argument("limit", type=int, nargs="?", default=None,
                    help="only validate the first N discovered replays")
    ap.add_argument("--recursive", action="store_true",
                    help="also walk subdirectories of <corpus> -- see "
                         "corpus_scan.py for why this is opt-in")
    ap.add_argument("--redact-identifiers", action="store_true",
                    help="replace corpus paths and replay filenames in output "
                         "with private, run-local labels")
    return ap.parse_args(argv[1:])


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    exe, root = args.exe, args.corpus
    # Leave two cores for the OS; each worker is a whole vrfkit process.
    jobs = max(1, min(int(os.environ.get("VRFKIT_JOBS", "0")) or (os.cpu_count() or 2) - 2, 16))

    scan = corpus_scan.discover(root, args.recursive)
    print(corpus_scan.scope_line(scan, args.redact_identifiers))
    files = scan.files
    if args.limit is not None:
        files = files[: args.limit]
        print(f"limited to the first {len(files)} of {len(scan.files)} discovered")
    if not files:
        where = "<private corpus>" if args.redact_identifiers else str(root)
        raise SystemExit(f"no .vrf under {where}")

    executable = "<vrfkit>" if args.redact_identifiers else str(exe)
    print(f"\nvalidating {len(files)} replays with {executable} ({jobs} workers)\n")
    ok = 0
    failures: list[tuple[str, str]] = []
    branches: collections.Counter[str] = collections.Counter()
    rates: list[tuple[float, str]] = []
    skipping: list[tuple[int, object, float, str]] = []
    totals = collections.Counter()
    missing: collections.Counter[str] = collections.Counter()
    started = time.time()

    with ThreadPoolExecutor(max_workers=jobs) as pool:
        results = pool.map(lambda f: (f, _run_one(exe, f)), files)

        for i, (f, outcome) in enumerate(results, 1):
            label = corpus_scan.replay_label(f, i, args.redact_identifiers)
            err, out = outcome
            if err is not None:
                failures.append((label, corpus_scan.diagnostic(
                    err, args.redact_identifiers)))
                continue
            got, parse_error = parse_oracle_output(out)
            if parse_error is not None:
                failures.append((label, corpus_scan.diagnostic(
                    f"{parse_error}: {sc.tail(out, 3, 160)}", args.redact_identifiers)))
                continue
            ok += 1
            branches[got["branch"].group(1)] += 1
            rate = float(got["rate"].group(1))
            rates.append((rate, label))
            values = {}
            for key in ("blocks", "malformed", "skipped", "fields", "rpcs"):
                if got[key]:
                    values[key] = int(got[key].group(1))
                    totals[key] += values[key]
                else:
                    missing[key] += 1  # never 0: `problems` fails the run on it
            if values.get("skipped"):
                skipping.append((values["skipped"], values.get("malformed", "?"), rate, label))
            if i % 25 == 0 or i == len(files):
                print(f"  [{i}/{len(files)}] ok={ok} failed={len(failures)}")

    elapsed = time.time() - started
    print(f"\nelapsed {elapsed:.1f}s ({elapsed / max(len(files), 1):.2f}s per replay)")
    print(f"succeeded: {ok}/{len(files)}")
    print(f"failed   : {len(failures)}")
    for name, why in failures[:15]:
        print(f"    {name}: {why}")

    if missing:
        print("\nWARNING: counters the oracle did not print (NOT counted as 0):")
        for key, count in missing.most_common():
            print(f"  {key}: absent on {count} replay(s)")

    print("\nbranches seen:")
    for b, c in branches.most_common():
        print(f"  {c:>4}  {b}")

    if rates:
        rates.sort()
        print("\noracle pass rate:")
        print(f"  min    {rates[0][0]:.6f}%  ({rates[0][1]})")
        print(f"  median {rates[len(rates) // 2][0]:.6f}%")
        print(f"  max    {rates[-1][0]:.6f}%")
        below = [(r, n) for r, n in rates if r < 99.99]
        print(f"  below 99.99%: {len(below)}")
        for r, n in below[:10]:
            print(f"    {r:.6f}%  {n}")

    print("\ncorpus totals:")
    for key in ("blocks", "fields", "rpcs", "malformed", "skipped"):
        print(f"  {key:<10} {totals[key]:>14,}")
    print(f"\nreplays that skip bits: {len(skipping)}")
    for skipped, malformed, rate, label in sorted(skipping, key=lambda item: -item[0]):
        print(f"  {skipped:>12,} bits  malformed={malformed}  rate={rate:.6f}%  {label}")

    found = problems(failures, missing)
    if found:
        print(f"\nFAILED: {len(found)} problem(s) across {len(files)} replays",
              file=sys.stderr)
        for line in found[:20]:
            print(f"    {line}", file=sys.stderr)
        return 1
    print(f"\nOK: {ok}/{len(files)} replays validated, every counter printed on "
          f"every one. Pass rates are reported above, not gated -- "
          f"check_corpus_baseline.py pins them per replay.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
