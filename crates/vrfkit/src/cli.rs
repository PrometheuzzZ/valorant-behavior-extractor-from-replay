//! Argument parsing -- hand-rolled, no external dependencies.

use crate::error::CliError;
use crate::inspect;
use crate::oracle;

const USAGE: &str = "\
vrfkit -- VALORANT replay (.vrf) toolkit

USAGE:
    vrfkit inspect  <file.vrf> [--redact-identifiers]
    vrfkit validate <file.vrf> [--diagnostics]
    vrfkit diag     <file.vrf> [--json <path>] [--include-payloads]
    vrfkit export   <file.vrf> --out <dir> [--checkpoints]
    vrfkit sens     <file.vrf> [--offline] [--behavior]
    vrfkit [file.vrf ...]   (or drag replays onto the exe / double-click
                             for the newest replay in VALORANT\\Saved\\Demos)

SUBCOMMANDS:
    inspect   Print replay info, header, branch, and chunk summary
              --redact-identifiers  Suppress the replay's friendly name
    validate  Run the RepLayout grammar oracle on every ReplayData content
              block. Exits 0 on a pass, 1 on any counted failure, and 2
              when the file carried no content blocks to check.
              --diagnostics  Print full context for every malformed/skipped event
    diag      Walk ReplayData and every Checkpoint chunk and aggregate every
              stream failure (kind, cause, group, function count, handle)
              into one bounded JSON document. Writes no table.
              --json  Write the aggregate to a file instead of stdout
              --include-payloads  Include bounded raw payload samples
    export    Write six Parquet tables (fields, movement, actors,
              net_guids, events, partials) + manifest.json into --out,
              which must be new, empty or hold only export output:
              anything else in it is refused, never deleted
              --checkpoints  Also parse Checkpoint chunks into
                             checkpoint_fields, checkpoint_actors,
                             checkpoint_net_guids, checkpoint_blocks,
                             checkpoint_guid_entries, checkpoint_export_groups and
                             checkpoint_export_fields
                             Parquet tables. Off by default: the
                             snapshots are ~10% of the file and a separate
                             read. fields, movement, actors, net_guids and
                             events are unaffected either way; checkpoint
                             partial rejections, if any, are added to
                             partials.
";

/// Dispatch one command line and return its exit code: 0, except that
/// `validate` is an oracle and returns its own, so `Ok` does not mean "clean".
/// See [`oracle::Verdict`].
pub fn run(args: &[String]) -> Result<u8, CliError> {
    // Double-click (no args) or replay files dropped onto the exe: sens mode.
    #[cfg(feature = "export")]
    if args.len() < 2 || args[1..].iter().all(|a| a.to_lowercase().ends_with(".vrf")) {
        return Ok(sens_interactive(&args[1..]));
    }
    if args.len() < 2 {
        return Err(CliError::Usage(USAGE.to_string()));
    }

    match args[1].as_str() {
        "inspect" => {
            let (file, [redact_identifiers], []) = parse(args, ["--redact-identifiers"], [])?;
            inspect::run(file, redact_identifiers).map(|()| 0)
        }
        "validate" => {
            let (file, [diagnostics], []) = parse(args, ["--diagnostics"], [])?;
            oracle::run(file, diagnostics).map(oracle::Verdict::exit_code)
        }
        "diag" => {
            let (file, [include_payloads], [json]) = parse(
                args,
                ["--include-payloads"],
                [("--json", "--json requires a file path")],
            )?;
            crate::diagnose::run(file, json, include_payloads).map(|()| 0)
        }
        "export" => export(args).map(|()| 0),
        #[cfg(feature = "export")]
        "sens" => {
            let (file, [offline, behavior], []) = parse(args, ["--offline", "--behavior"], [])?;
            crate::sens::run(file, offline, false, behavior).map(|()| 0)
        }
        "help" | "--help" | "-h" => {
            println!("{USAGE}");
            Ok(0)
        }
        other => Err(CliError::Usage(format!(
            "unknown subcommand: {other}\n{USAGE}"
        ))),
    }
}

/// Interactive sensitivity mode: given files, or the newest local replay.
#[cfg(feature = "export")]
fn sens_interactive(files: &[String]) -> u8 {
    let mut files: Vec<String> = files.to_vec();
    if files.is_empty() {
        match newest_local_replay() {
            Some(f) => files.push(f),
            None => println!(
                "No replays found. Drag a .vrf file onto the program.\n\
                 (Looked in %LOCALAPPDATA%\\VALORANT\\Saved\\Demos)"
            ),
        }
    }
    let mut code = 0;
    for f in &files {
        if let Err(e) = crate::sens::run(f, false, true, false) {
            println!("Error: {e}");
            code = 1;
        }
        println!();
    }
    println!("Press Enter to close...");
    let _ = std::io::stdin().read_line(&mut String::new());
    code
}

#[cfg(feature = "export")]
fn newest_local_replay() -> Option<String> {
    let base = std::env::var_os("LOCALAPPDATA")?;
    let dir = std::path::Path::new(&base).join("VALORANT").join("Saved").join("Demos");
    std::fs::read_dir(dir)
        .ok()?
        .flatten()
        .filter(|e| e.path().extension().is_some_and(|x| x.eq_ignore_ascii_case("vrf")))
        .max_by_key(|e| e.metadata().and_then(|m| m.modified()).ok())
        .map(|e| e.path().to_string_lossy().into_owned())
}

/// The input path, whether each flag was given, and each valued option's value.
type Parsed<'a, const F: usize, const V: usize> = (&'a str, [bool; F], [Option<&'a str>; V]);

/// Split `<subcommand> <file.vrf> [options]`. Each of `flags` may appear
/// once. Each of `valued` may appear once and takes the next argument as its
/// value, whatever it looks like; the pair's second element is the message
/// when there is none. Anything else is refused.
fn parse<'a, const F: usize, const V: usize>(
    args: &'a [String],
    flags: [&str; F],
    valued: [(&str, &str); V],
) -> Result<Parsed<'a, F, V>, CliError> {
    let file = args
        .get(2)
        .ok_or_else(|| CliError::Usage(format!("{} requires <file.vrf>", args[1])))?;
    let (mut set, mut values) = ([false; F], [None; V]);
    let mut rest = args[3..].iter();
    while let Some(arg) = rest.next() {
        let duplicate = || CliError::Usage(format!("duplicate option: {arg}"));
        if let Some(i) = flags.iter().position(|flag| arg == flag) {
            if set[i] {
                return Err(duplicate());
            }
            set[i] = true;
        } else if let Some(i) = valued.iter().position(|(option, _)| arg == option) {
            if values[i].is_some() {
                return Err(duplicate());
            }
            let value = rest
                .next()
                .ok_or_else(|| CliError::Usage(valued[i].1.to_string()))?;
            values[i] = Some(value.as_str());
        } else {
            return Err(CliError::Usage(format!(
                "unknown {} option or surplus argument: {arg}",
                args[1]
            )));
        }
    }
    Ok((file, set, values))
}

#[cfg(feature = "export")]
fn export(args: &[String]) -> Result<(), CliError> {
    let (file, [with_checkpoints], [out_dir]) = parse(
        args,
        ["--checkpoints"],
        [("--out", "--out requires a directory path")],
    )?;
    let out_dir =
        out_dir.ok_or_else(|| CliError::Usage("export requires --out <dir>".to_string()))?;
    crate::driver::run(file, out_dir, with_checkpoints)
}

/// Refusal, not silence: without the `export` feature there are no writers,
/// and printing nothing with exit 0 would look like the files were written.
#[cfg(not(feature = "export"))]
fn export(_args: &[String]) -> Result<(), CliError> {
    Err(CliError::Usage(
        "export is not available: this binary was built without the `export` feature".to_string(),
    ))
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Refusals are usage errors raised before the file is opened; an
    /// accepted command line gets as far as opening the missing file.
    #[test]
    fn unknown_surplus_and_duplicate_arguments_are_refused_before_the_file() {
        const REDACT: &str = "--redact-identifiers";
        const CP: &str = "--checkpoints";
        let mut cases: Vec<(&[&str], bool)> = vec![
            (&["inspect", "m.vrf", "extra"], true),
            (&["inspect", "m.vrf", REDACT], false),
            (&["inspect", "m.vrf", REDACT, REDACT], true),
            (&["validate", "m.vrf", "--unknown"], true),
            (&["validate", "m.vrf", "other.vrf"], true),
        ];
        if cfg!(feature = "export") {
            cases.push((&["export", "m.vrf", "--out", "a", "--out", "b"], true));
            cases.push((&["export", "m.vrf", "--out", "o", CP, CP], true));
        }
        for (args, refused) in cases {
            let argv: Vec<String> = ["vrfkit"]
                .iter()
                .chain(args)
                .map(|a| a.to_string())
                .collect();
            match run(&argv) {
                Err(CliError::Usage(_)) if refused => {}
                Err(CliError::Io(_)) if !refused => {}
                other => panic!("{args:?}: {other:?}"),
            }
        }
    }
}
