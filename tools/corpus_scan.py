"""Shared `.vrf` discovery for validate_corpus.py and
check_decode_errors_corpus.py, so the two cannot scan different sets as "the
corpus" without saying so.

Non-recursive by default: a subdirectory need not hold more of the same
corpus (the live client's `Demos/old` holds replays it is about to rotate out,
possibly across a build boundary). `--recursive` opts in on both tools.
`discover()` always counts the `.vrf` files a non-recursive scan leaves out,
and callers print that count unconditionally, zero included. A caller's
`limit` applies after discovery, so `excluded` is only ever the recursion
setting's doing.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class CorpusScan:
    """What one discovery call found, and how it looked."""

    files: list[Path]
    scanned_root: Path
    recursive: bool
    #: `.vrf` files under `scanned_root`, in subdirectories, NOT included in
    #: `files` because `recursive` was False. Always 0 when `recursive` is True.
    excluded: int


def find_replays(root: Path, recursive: bool) -> list[Path]:
    """Replay files by suffix, case-insensitively on every host
    (`Path.glob("*.vrf")` follows the filesystem's case rules); a directory
    named `x.vrf` is not one."""
    candidates = root.rglob("*") if recursive else root.glob("*")
    return sorted(
        path for path in candidates
        if path.is_file() and path.suffix.casefold() == ".vrf"
    )


def discover(root: Path, recursive: bool) -> CorpusScan:
    """The `.vrf` files that make up the corpus at `root`; `excluded` is
    counted with one extra rglob, never guessed."""
    top = find_replays(root, recursive=False)
    if recursive:
        everything = find_replays(root, recursive=True)
        return CorpusScan(files=everything, scanned_root=root, recursive=True,
                          excluded=0)
    everything = find_replays(root, recursive=True)
    excluded = len(everything) - len(top)
    return CorpusScan(files=top, scanned_root=root, recursive=False,
                      excluded=excluded)


def replay_label(path: Path, index: int, redact_identifiers: bool) -> str:
    """A diagnostic label: under redaction `replay-NNNN`, stable within one
    sorted sweep, since filenames are often account- or session-derived."""
    return f"replay-{index:04d}" if redact_identifiers else path.name


def diagnostic(detail: str, redact_identifiers: bool) -> str:
    """Strip subprocess tails that may echo private input metadata."""
    if not redact_identifiers:
        return detail
    return detail.partition(":")[0]


def scope_line(scan: CorpusScan, redact_identifiers: bool = False) -> str:
    """One line stating the corpus scope, `excluded` included at 0."""
    mode = "recursive" if scan.recursive else "top-level only"
    root = "<private corpus>" if redact_identifiers else str(scan.scanned_root)
    line = (f"corpus scope: {len(scan.files)} .vrf file(s) under "
            f"{root} ({mode}); {scan.excluded} more in "
            f"subdirectories excluded")
    if not scan.recursive:
        line += " (pass --recursive to include)"
    return line
