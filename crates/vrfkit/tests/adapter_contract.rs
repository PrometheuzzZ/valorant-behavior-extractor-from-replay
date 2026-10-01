//! Cross-language pins for the constants `tools/to_valplay_bundle.py` shares
//! with this workspace: the adapter must agree with Rust on values, and Rust
//! tests use the symbols, so a value drift leaves the Rust suite green. Two
//! constants classify every row: a drifted
//! `UNRESOLVED_CLASS_NET_CACHE_PAYLOAD_FIELD_NAME` publishes preserved
//! ClassNetCache blobs as decoded fields, and a drifted
//! `CLASS_NET_CACHE_SUFFIX` reclassifies every RPC as a replicated property:
//! a complete-looking bundle with no kills, damage or abilities.
//!
//! Constants are found by assignment name, not substring, because the values
//! also occur in prose (e.g. the docstring); the Event allowlists are compared
//! whole against `vrf_container::KNOWN_EVENT_GROUPS`, so a group missing on
//! either side fails.

use std::fs;
use std::path::PathBuf;

/// The adapter source from this test's workspace. A missing file fails, never
/// skips: "could not be checked" must not read as "holds".
fn adapter_source() -> String {
    let path = adapter_path();
    fs::read_to_string(&path).unwrap_or_else(|err| {
        panic!(
            "cannot read the valplay adapter at {}: {err}",
            path.display()
        )
    })
}

fn adapter_path() -> PathBuf {
    // Two up from crates/vrfkit, fixed at compile time: the checkout under
    // test, not a working directory `cargo test` does not promise.
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("..")
        .join("..")
        .join("tools")
        .join("to_valplay_bundle.py")
}

/// The index of the line holding the top-level `name = ...` assignment.
fn assignment_line(source: &str, name: &str) -> Option<usize> {
    source.lines().position(|line| {
        (line.strip_prefix(name)).is_some_and(|rest| rest.trim_start().starts_with('='))
    })
}

/// Every string literal in the top-level `name = ...` assignment, joined, as
/// Python joins adjacent literals. `None` when there is no such assignment,
/// which the call site treats as a failure.
fn python_constant(source: &str, name: &str) -> Option<String> {
    let start = assignment_line(source, name)?;

    // The first line, plus continuation lines while brackets remain open.
    let mut statement = String::new();
    let mut depth: i32 = 0;
    for line in source.lines().skip(start) {
        statement.push_str(line);
        statement.push('\n');
        for c in line.chars() {
            match c {
                '(' | '[' | '{' => depth += 1,
                ')' | ']' | '}' => depth -= 1,
                _ => {}
            }
        }
        if depth <= 0 {
            break;
        }
    }

    // Escapes are not modelled: the constants carry none, and one that grew
    // one would stop matching, the safe direction to fail in.
    let mut value = String::new();
    let mut quote: Option<char> = None;
    for c in statement.chars() {
        match quote {
            None => {
                if c == '"' || c == '\'' {
                    quote = Some(c);
                }
            }
            Some(open) => {
                if c == open {
                    quote = None;
                } else {
                    value.push(c);
                }
            }
        }
    }
    Some(value)
}

/// Parse one top-level numeric assignment used by both languages.
fn python_f64_constant(source: &str, name: &str) -> Option<f64> {
    let line = source.lines().nth(assignment_line(source, name)?)?;
    line[name.len()..].trim_start()[1..].trim().parse().ok()
}

/// The Event allowlist dictionaries, which the adapter keeps one entry per
/// line; any other syntax fails closed rather than being parsed as Python.
fn python_string_dict(source: &str, name: &str) -> Option<Vec<(String, String)>> {
    let start = assignment_line(source, name)?;
    let mut rows = Vec::new();
    for line in source.lines().skip(start + 1) {
        let line = line.trim();
        if line == "}" {
            rows.sort();
            return Some(rows);
        }
        let entry = line.strip_suffix(',')?;
        let (key, value) = entry.split_once(':')?;
        let key = key.trim().strip_prefix('"')?.strip_suffix('"')?;
        rows.push((key.to_string(), value.trim().to_string()));
    }
    None
}

const WORD_COUNTS: &str = "_SERVER_TIMELINE_WORD_COUNTS";

/// The adapter's Event allowlist `dictionary` against the parser's table,
/// read through the accessor the parser calls and rendered as the dictionary
/// literal spells it.
fn assert_event_contract(source: &str, dictionary: &str) {
    use vrf_container::{
        known_event_payload_name, known_event_payload_tag, known_event_word_count,
    };
    let render: fn(&str) -> Option<String> = match dictionary {
        WORD_COUNTS => |group| known_event_word_count(group).map(|n| n.to_string()),
        "_SERVER_TIMELINE_PAYLOAD_TAGS" => {
            |group| known_event_payload_tag(group).map(|t| t.to_string())
        }
        "_SERVER_TIMELINE_PAYLOAD_NAMES" => {
            |group| known_event_payload_name(group).map(|n| format!("\"{n}\""))
        }
        other => panic!("no parser accessor for {other}"),
    };
    let mut rust = vrf_container::KNOWN_EVENT_GROUPS
        .iter()
        .map(|known| {
            let value = render(known.group).unwrap_or_else(|| {
                panic!(
                    "{} is in KNOWN_EVENT_GROUPS but its accessor returns None",
                    known.group
                )
            });
            (known.group.to_string(), value)
        })
        .collect::<Vec<_>>();
    rust.sort();
    let python = python_string_dict(source, dictionary)
        .unwrap_or_else(|| panic!("the adapter must assign a simple {dictionary} dictionary"));
    assert_eq!(python, rust, "the adapter's {dictionary} allowlist drifted");
}

/// `_SERVER_TIMELINE_WORD_COUNTS` from the parser's table, minus `skip` and
/// plus `extra`: differing from the parser by at most one group.
fn word_count_dictionary(skip: Option<&str>, extra: Option<(&str, usize)>) -> String {
    let mut source = format!("{WORD_COUNTS} = {{\n");
    for known in vrf_container::KNOWN_EVENT_GROUPS {
        if Some(known.group) != skip {
            source.push_str(&format!("    \"{}\": {},\n", known.group, known.word_count));
        }
    }
    if let Some((group, count)) = extra {
        source.push_str(&format!("    \"{group}\": {count},\n"));
    }
    source.push_str("}\n");
    source
}

#[test]
fn adapter_pins_the_unresolved_class_net_cache_field_name() {
    let source = adapter_source();
    let python = python_constant(&source, "UNRESOLVED_CLASS_NET_CACHE_PAYLOAD_FIELD_NAME")
        .expect("the adapter must assign UNRESOLVED_CLASS_NET_CACHE_PAYLOAD_FIELD_NAME");
    assert_eq!(
        python,
        vrf_export::UNRESOLVED_CLASS_NET_CACHE_PAYLOAD_FIELD_NAME,
        "the adapter's reserved field name no longer matches vrf-export's; \
         preserved ClassNetCache blocks would be published as decoded fields"
    );
}

#[test]
fn adapter_pins_the_class_net_cache_suffix() {
    let source = adapter_source();
    let python = python_constant(&source, "CLASS_NET_CACHE_SUFFIX")
        .expect("the adapter must assign CLASS_NET_CACHE_SUFFIX");
    assert_eq!(
        python,
        vrf_schema::CLASS_NET_CACHE_SUFFIX,
        "the adapter's RPC discriminator no longer matches vrf-schema's; \
         every RPC in the bundle would be classified as a replicated property"
    );
}

#[test]
fn adapter_pins_the_event_payload_time_tolerance() {
    let source = adapter_source();
    let python = python_f64_constant(&source, "EVENT_PAYLOAD_TIME_TOLERANCE_MS")
        .expect("the adapter must assign EVENT_PAYLOAD_TIME_TOLERANCE_MS");
    assert_eq!(
        python,
        vrf_container::EVENT_PAYLOAD_TIME_TOLERANCE_MS,
        "the adapter and parser no longer agree on when payload_seconds is structurally valid"
    );
}

#[test]
fn adapter_pins_the_event_payload_word_counts() {
    assert_event_contract(&adapter_source(), WORD_COUNTS);
}

/// A one-value drift must reach the assertion. Built from the parser's table,
/// so it stays complete as the table grows and fails on the value alone.
#[test]
#[should_panic(expected = "the adapter's _SERVER_TIMELINE_WORD_COUNTS allowlist drifted")]
fn event_word_count_contract_rejects_one_drifted_value() {
    let complete = word_count_dictionary(None, None);
    let drifted = complete.replacen("\"characterDeath\": 2,", "\"characterDeath\": 1,", 1);
    assert_ne!(
        drifted, complete,
        "the fixture must change exactly one value"
    );
    assert_event_contract(&drifted, WORD_COUNTS);
}

/// The parser's own table passes, so the two tests below fail for the one
/// group they change, not for how the dictionary is written.
#[test]
fn event_word_count_contract_accepts_the_parsers_own_table() {
    assert_event_contract(&word_count_dictionary(None, None), WORD_COUNTS);
}

/// A group the parser knows and the adapter would publish without its words.
#[test]
#[should_panic(expected = "the adapter's _SERVER_TIMELINE_WORD_COUNTS allowlist drifted")]
fn event_word_count_contract_rejects_a_group_the_adapter_lacks() {
    let first = vrf_container::KNOWN_EVENT_GROUPS[0].group;
    assert_event_contract(&word_count_dictionary(Some(first), None), WORD_COUNTS);
}

/// The adapter assigns words to a group the parser never decodes.
#[test]
#[should_panic(expected = "the adapter's _SERVER_TIMELINE_WORD_COUNTS allowlist drifted")]
fn event_word_count_contract_rejects_a_group_the_parser_lacks() {
    let extra = word_count_dictionary(None, Some(("spikeDropped", 0)));
    assert_event_contract(&extra, WORD_COUNTS);
}

#[test]
fn adapter_pins_the_event_payload_tags() {
    assert_event_contract(&adapter_source(), "_SERVER_TIMELINE_PAYLOAD_TAGS");
}

#[test]
fn adapter_pins_the_event_payload_names() {
    assert_event_contract(&adapter_source(), "_SERVER_TIMELINE_PAYLOAD_NAMES");
}

/// A scanner that returned the Rust value, or the first quoted text in the
/// file, would keep the constant tests green forever.
#[test]
fn the_constant_scanner_reads_the_assignment_and_not_the_prose() {
    let source = concat!(
        "\"\"\"A docstring mentioning WIDGET = \"decoy\" in prose.\"\"\"\n",
        "\n",
        "OTHER = \"not this one\"\n",
        "WIDGET = (\n",
        "    \"real\"\n",
        "    \"_value\"\n",
        ")\n",
        "TRAILING = \"after\"\n",
    );
    assert_eq!(
        python_constant(source, "WIDGET").as_deref(),
        Some("real_value")
    );
    assert_eq!(
        python_constant(source, "OTHER").as_deref(),
        Some("not this one")
    );
    assert_eq!(
        python_constant(source, "ABSENT"),
        None,
        "a missing assignment must be reported, not silently treated as empty"
    );
    assert_eq!(python_f64_constant("LIMIT = 1.001\n", "LIMIT"), Some(1.001));
    assert_eq!(python_f64_constant("LIMIT = nope\n", "LIMIT"), None);
    assert_eq!(
        python_string_dict(
            "VALUES = {\n    \"two\": \"second\",\n    \"one\": 1,\n}\n",
            "VALUES",
        ),
        Some(vec![
            ("one".to_string(), "1".to_string()),
            ("two".to_string(), "\"second\"".to_string()),
        ])
    );
}
