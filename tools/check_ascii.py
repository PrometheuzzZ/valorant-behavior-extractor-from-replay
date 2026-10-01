#!/usr/bin/env python3
"""Reject non-ASCII bytes in tracked Rust source files."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


Violation = tuple[int, int, int]
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def violations(path: Path) -> list[Violation]:
    """Return one-based line/byte-column locations for non-ASCII bytes."""
    found: list[Violation] = []
    for line_no, line in enumerate(path.read_bytes().splitlines(keepends=True), 1):
        for column, byte in enumerate(line, 1):
            if byte > 0x7F:
                found.append((line_no, column, byte))
    return found


def tracked_rust_files() -> list[Path]:
    """Enumerate tracked Rust files from the repository root."""
    result = subprocess.run(
        ["git", "-C", str(REPOSITORY_ROOT), "ls-files", "-z", "--", "*.rs"],
        check=True,
        stdout=subprocess.PIPE,
    )
    return [Path(raw.decode()) for raw in result.stdout.split(b"\0") if raw]


def scan(
    paths: list[Path], *, root: Path | None = None
) -> tuple[list[tuple[Path, Violation]], int]:
    """Scan paths, returning individual violations and affected-line count."""
    found: list[tuple[Path, Violation]] = []
    affected_lines: set[tuple[Path, int]] = set()
    for path in paths:
        source_path = root / path if root is not None else path
        for violation in violations(source_path):
            found.append((path, violation))
            affected_lines.add((path, violation[0]))
    return found, len(affected_lines)


def run_check(paths: list[Path], *, tracked: bool) -> int:
    """Print a stable scanner report and return its process exit status."""
    try:
        root = REPOSITORY_ROOT if tracked else None
        found, affected_line_count = scan(paths, root=root)
    except OSError as exc:
        print(f"ERROR: cannot read Rust source: {exc}", file=sys.stderr)
        return 2

    if found:
        for path, (line, column, byte) in found:
            print(
                f"{path.as_posix()}:{line}:{column}: non-ASCII byte 0x{byte:02X}",
                file=sys.stderr,
            )
        print(
            f"FAILED: {affected_line_count} line(s), {len(found)} byte(s)",
            file=sys.stderr,
        )
        return 1

    scope = "tracked Rust file(s)" if tracked else "Rust file(s)"
    print(f"OK: {len(paths)} {scope}, ASCII only")
    return 0



def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="scan Rust sources")
    parser.add_argument(
        "--path",
        action="append",
        type=Path,
        default=[],
        help="scan exactly PATH (repeatable test hook)",
    )
    args = parser.parse_args()
    if args.path and not args.check:
        parser.error("--path requires --check")
    if not args.check:
        parser.error("--check is required")
    return args


def main() -> int:
    args = parse_args()
    if args.path:
        missing = [path for path in args.path if not path.is_file()]
        if missing:
            for path in missing:
                print(f"ERROR: missing Rust source: {path}", file=sys.stderr)
            return 2
        return run_check(args.path, tracked=False)

    try:
        paths = tracked_rust_files()
    except (OSError, subprocess.CalledProcessError, UnicodeDecodeError) as exc:
        print(f"ERROR: cannot enumerate tracked Rust files: {exc}", file=sys.stderr)
        return 2
    if not paths:
        # `git ls-files` can succeed and print nothing (wrong directory, no
        # commits, a pathspec that stopped matching). This repository always
        # has Rust files, so an empty enumeration is a broken measurement.
        print(
            f"ERROR: no tracked Rust files under {REPOSITORY_ROOT}; "
            f"nothing was scanned",
            file=sys.stderr,
        )
        return 2
    return run_check(paths, tracked=True)


if __name__ == "__main__":
    raise SystemExit(main())
