"""vrf-decode's overlay tables, parsed, and `overlay::resolve_entry`.

The one copy check_checksum_types.py and check_entry_survival.py share. The
parsers return each `FieldType`'s source text; each tool keeps its own
spelling, since both appear in their reports. A table must parse whole -- the
declared length, the literals and the parsed entries agree and no key repeats
-- or `ParseError` names it: an entry the pattern missed would go unchecked.
"""

from __future__ import annotations

import re

#: A Rust string literal's body. `\\.` also takes a backslash-newline
#: continuation, which `unescape` then removes.
STR = r'"((?:[^"\\]|\\.)*)"'
FIELD_TYPE = r"(FieldType::\w+(?:\s*\{[^}]*\})?)"


class ParseError(ValueError):
    """A Rust table did not parse completely: fail rather than check fewer."""


def unescape(raw: str) -> str:
    """The value of a Rust string literal body.

    A backslash before a newline drops the newline and the next line's leading
    whitespace -- `GROUP_ALIASES` splits its paths that way.
    """
    raw = re.sub(r"\\\r?\n\s*", "", raw)
    return raw.replace('\\"', '"').replace("\\\\", "\\")


def array_body(src: str, header: str, what: str) -> tuple[int, str]:
    """`(declared length, body)` of a `[T; N] = [ ... ];` array literal,
    rustfmt's one-line `= [];` included."""
    m = re.search(header, src)
    if m is None:
        raise ParseError(f"{what}: array header not found")
    if src.startswith("];", m.end()):
        return int(m.group(1)), ""
    end = src.find("\n];", m.end())
    if end < 0:
        raise ParseError(f"{what}: array end not found")
    return int(m.group(1)), src[m.end():end]


def check_count(what: str, declared: int, literal: int, parsed: int) -> None:
    if not declared == literal == parsed:
        raise ParseError(
            f"{what}: the source declares {declared} entries and holds {literal} "
            f"literals, but {parsed} parsed -- the rest would go unchecked")


def _unique(what: str, keys) -> None:
    seen = set()
    for key in keys:
        if key in seen:
            raise ParseError(f"{what}: {key!r} is declared twice")
        seen.add(key)


def overlay_entries(src: str) -> list[tuple[str, str, str]]:
    """`(group, field name, FieldType text)` for every `OVERLAY_TABLE` entry."""
    declared, body = array_body(
        src, r"pub static OVERLAY_TABLE: \[OverlayEntry; (\d+)\] = \[", "OVERLAY_TABLE")
    pattern = re.compile(
        r"OverlayEntry \{\s*group_path: " + STR + r",\s*field_name: " + STR
        + r",\s*field_type: " + FIELD_TYPE + r",?\s*\}", re.S)
    entries = [(unescape(g), unescape(n), t) for g, n, t in pattern.findall(body)]
    check_count("OVERLAY_TABLE", declared, body.count("OverlayEntry {"), len(entries))
    _unique("OVERLAY_TABLE", ((g, n) for g, n, _ in entries))
    return entries


def handle_entries(src: str) -> list[tuple[str, int, str]]:
    """`(group, handle, field name)` for every `OVERLAY_HANDLE_TABLE` entry."""
    declared, body = array_body(
        src, r"pub static OVERLAY_HANDLE_TABLE: \[OverlayHandleEntry; (\d+)\] = \[",
        "OVERLAY_HANDLE_TABLE")
    pattern = re.compile(
        r"OverlayHandleEntry \{\s*group_path: " + STR + r",\s*handle: (\d+),\s*"
        r"field_name: " + STR + r",?\s*\}", re.S)
    entries = [(unescape(g), int(h), unescape(n)) for g, h, n in pattern.findall(body)]
    check_count("OVERLAY_HANDLE_TABLE", declared, body.count("OverlayHandleEntry {"),
                len(entries))
    _unique("OVERLAY_HANDLE_TABLE", ((g, h) for g, h, _ in entries))
    return entries


def scoped_entries(src: str) -> list[tuple[str, str, int, str]]:
    """`(field name, group, checksum, FieldType text)` for every `SCOPED_TYPES` entry."""
    declared, body = array_body(
        src, r"static SCOPED_TYPES: \[\(&str, &str, u32, FieldType\); (\d+)\] = \[",
        "SCOPED_TYPES")
    pattern = re.compile(
        r"\(\s*" + STR + r",\s*" + STR + r",\s*(\d+),\s*" + FIELD_TYPE + r",?\s*\)", re.S)
    entries = [(unescape(n), unescape(g), int(c), t) for n, g, c, t in pattern.findall(body)]
    check_count("SCOPED_TYPES", declared, body.count("FieldType::"), len(entries))
    _unique("SCOPED_TYPES", (e[:3] for e in entries))
    return entries


def checksum_entries(src: str) -> list[tuple[int, str]]:
    """`(checksum, FieldType text)` for every `CHECKSUM_TYPES` entry."""
    declared, body = array_body(
        src, r"pub static CHECKSUM_TYPES: \[\(u32, FieldType\); (\d+)\] = \[",
        "CHECKSUM_TYPES")
    pattern = re.compile(r"\(\s*(\d+),\s*" + FIELD_TYPE + r",?\s*\)", re.S)
    entries = [(int(c), t) for c, t in pattern.findall(body)]
    check_count("CHECKSUM_TYPES", declared, body.count("FieldType::"), len(entries))
    _unique("CHECKSUM_TYPES", (c for c, _ in entries))
    return entries


def group_aliases(src: str) -> list[tuple[str, str]]:
    """`(source group, canonical group)` for every `GROUP_ALIASES` pair."""
    start = src.find("const GROUP_ALIASES: &[(&str, &str)] = &[")
    if start < 0:
        raise ParseError("GROUP_ALIASES: not found")
    end = src.find("\n];", start)
    body = src[start:end]
    body = body[body.index("= &[") + 4:]
    pairs = [(unescape(a), unescape(b)) for a, b in re.findall(
        r"\(\s*" + STR + r",\s*" + STR + r",?\s*\)", body, re.S)]
    literals = len(re.findall(STR, body, re.S))
    check_count("GROUP_ALIASES", len(pairs), literals // 2 if literals % 2 == 0 else -1,
                len(pairs))
    if not pairs:
        raise ParseError("GROUP_ALIASES: parsed no pair")
    _unique("GROUP_ALIASES", (a for a, _ in pairs))
    return pairs


def engine_object_refs(src: str) -> list[str]:
    """The field names `ENGINE_OBJECT_REFS` types as object references."""
    m = re.search(r"const ENGINE_OBJECT_REFS: \[&str; (\d+)\] = \[([^\]]*)\];", src)
    if m is None:
        raise ParseError("ENGINE_OBJECT_REFS: not found")
    names = [unescape(n) for n in re.findall(STR, m.group(2))]
    check_count("ENGINE_OBJECT_REFS", int(m.group(1)), len(names), len(names))
    return names


def is_fname_index(name: str | None) -> bool:
    """`overlay::is_unresolved_fname_index`: a bare decimal names nothing."""
    return bool(name) and name.isascii() and name.isdigit()


class Resolver:
    """`overlay::resolve_entry` over parsed tables, whatever each key maps to.

    `entries` {(group, name): value}, `handles` {(group, handle): field
    name}, `scoped` {(name, group, checksum): value},
    `checksums` {checksum: value}, `aliases` {group: canonical group},
    `engine_refs` the names typed as engine object references, which resolve
    to `engine_value`.
    """

    engine_value = None

    def __init__(self, entries, handles, scoped, checksums, aliases, engine_refs):
        self.entries, self.handles = entries, handles
        self.scoped, self.checksums = scoped, checksums
        self.aliases, self.engine_refs = aliases, engine_refs

    def in_group(self, group, name, handle):
        """`(value, step)` in one group: the name, then `b` + name, then the
        handle, which is refused when the wire declares another real name
        there. `(None, None)` when none resolves."""
        if name is not None:
            for probe, step in ((name, "name"), ("b" + name, "b-prefix")):
                value = self.entries.get((group, probe))
                if value is not None:
                    return value, step
        if handle is None:
            return None, None
        named = self.handles.get((group, handle))
        if named is None:
            return None, None
        if name is not None and name != named and not is_fname_index(name):
            return None, None  # refused: the wire declares something else here
        value = self.entries.get((group, named))
        return (value, "handle") if value is not None else (None, None)

    def resolve(self, group, name, handle, checksum):
        """`(value, step)` as vrfkit resolves the declaration, or `(None, None)`.

        `step` is `name`, `b-prefix` or `handle` -- prefixed `alias ` when found
        in the group `GROUP_ALIASES` names -- `scoped`, `engine reference` or
        `checksum table`.
        """
        value, step = self.in_group(group, name, handle)
        if value is not None:
            return value, step
        aliased = self.aliases.get(group)
        if aliased is not None:
            value, step = self.in_group(aliased, name, handle)
            if value is not None:
                return value, "alias " + step
        if name is None:
            return None, None
        if checksum is not None:
            value = self.scoped.get((name, group, checksum))
            if value is not None:
                return value, "scoped"
        if name in self.engine_refs:
            return self.engine_value, "engine reference"
        if checksum is not None and checksum in self.checksums:
            return self.checksums[checksum], "checksum table"
        return None, None
