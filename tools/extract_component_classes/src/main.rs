//! List every component template's instance name, owning package and class
//! from an installed game's IoStore containers: a replay names a Blueprint
//! component only by its instance name (`ZoomStateMachine`), and only the cooked
//! game says its class. The source of `KNOWN_SUBOBJECT_CLASS_PATHS` in
//! `crates/vrfkit/src/sink/paths.rs` (procedure: docs/DATA.md, "Reading
//! component classes out of the game").
//!
//! Exit status: 0 when every package was read and every self-check held; 1 when
//! anything could not be read or a check failed (readable rows are still
//! written, and the summary says what is missing); 2 for a usage or setup error.

#![forbid(unsafe_code)]

mod cityhash;
mod container;
mod dirindex;
mod names;
mod reader;
mod scan;
mod script;
mod toc;
mod zen;

use std::collections::{BTreeMap, BTreeSet, HashMap};
use std::fmt::Write as _;
use std::io::Write as _;
use std::path::{Path, PathBuf};
use std::sync::Mutex;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::time::UNIX_EPOCH;

use container::Container;
use scan::{CLASS_KINDS, ClassTable, Job, PackageScan, Resolved, resolve, scan_package};
use script::{ScriptObjects, parse_script_objects};
use toc::{CHUNK_EXPORT_BUNDLE_DATA, CHUNK_SCRIPT_OBJECTS};

const USAGE: &str = "usage: extract-component-classes <PAKS_DIR> [--format tsv|json] \
[--kind all|gen_variable|cdo_subobject] [--name NAME]... [--jobs N] [--out FILE]";

#[derive(Debug, Clone, Copy)]
enum Format {
    Tsv,
    Json,
}

#[derive(Debug)]
struct Args {
    paks: PathBuf,
    format: Format,
    kind: Option<String>,
    names: BTreeSet<String>,
    jobs: usize,
    out: Option<PathBuf>,
}

fn parse_args() -> Result<Args, String> {
    let mut it = std::env::args().skip(1);
    let mut paks = None;
    let mut format = Format::Tsv;
    let mut kind = None;
    let mut names = BTreeSet::new();
    let mut jobs = std::thread::available_parallelism()
        .map_or(4, |n| n.get())
        .min(16);
    let mut out = None;
    while let Some(arg) = it.next() {
        let mut value = |flag: &str| it.next().ok_or_else(|| format!("{flag} needs a value"));
        match arg.as_str() {
            "--format" => {
                format = match value("--format")?.as_str() {
                    "tsv" => Format::Tsv,
                    "json" => Format::Json,
                    other => return Err(format!("unknown --format {other}")),
                }
            }
            "--kind" => {
                let k = value("--kind")?;
                match k.as_str() {
                    "all" => kind = None,
                    "gen_variable" | "cdo_subobject" => kind = Some(k),
                    other => return Err(format!("unknown --kind {other}")),
                }
            }
            "--name" => {
                names.insert(value("--name")?);
            }
            "--jobs" => {
                jobs = value("--jobs")?
                    .parse()
                    .map_err(|_| "--jobs needs a positive number".to_owned())?;
                if jobs == 0 {
                    return Err("--jobs needs a positive number".to_owned());
                }
            }
            "--out" => out = Some(PathBuf::from(value("--out")?)),
            "-h" | "--help" => return Err(String::new()),
            flag if flag.starts_with("--") => return Err(format!("unknown option {flag}")),
            path => {
                if paks.replace(PathBuf::from(path)).is_some() {
                    return Err("more than one PAKS_DIR given".to_owned());
                }
            }
        }
    }
    let paks = paks.ok_or_else(|| "PAKS_DIR is required".to_owned())?;
    Ok(Args {
        paks,
        format,
        kind,
        names,
        jobs,
        out,
    })
}

#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord)]
struct Row {
    instance: String,
    kind: &'static str,
    asset: String,
    export: String,
    class: String,
    class_kind: &'static str,
    native_class: String,
    outer: String,
    class_ref: String,
    container: String,
}

/// `Counts` with one `usize` per name, and `Counts::pairs`, every counter in
/// this order and then each `class_kind.*`: all printed, zeros included.
macro_rules! counters {
    ($($name:ident),* $(,)?) => {
        #[derive(Debug, Default)]
        struct Counts {
            $($name: usize,)*
            class_kinds: BTreeMap<&'static str, usize>,
        }

        impl Counts {
            fn pairs(&self) -> Vec<(String, usize)> {
                let mut v = vec![$((stringify!($name).to_owned(), self.$name)),*];
                // A kind missing from `CLASS_KINDS` still prints, after the rest.
                let stray = self.class_kinds.keys().filter(|k| !CLASS_KINDS.contains(*k));
                for kind in CLASS_KINDS.iter().chain(stray) {
                    let n = self.class_kinds.get(kind).copied().unwrap_or(0);
                    v.push((format!("class_kind.{kind}"), n));
                }
                v
            }
        }
    };
}

counters! {
    containers,
    legacy_paks_not_read,
    toc_entries,
    indexed_files,
    // Directory-index files naming a TOC entry past the end of the TOC, or one
    // a later file also names: neither can be attached to a chunk.
    indexed_files_dropped,
    package_chunks,
    package_chunks_unindexed,
    package_files_not_package_chunks,
    packages_read,
    packages_failed,
    package_id_matches,
    package_id_mismatches,
    file_stem_matches,
    file_stem_mismatches,
    exports,
    class_objects,
    duplicate_class_keys,
    script_objects,
    script_paths_resolved,
    script_hash_matches,
    script_hash_mismatches,
    gen_variable,
    gen_variable_numbered,
    cdo_subobject,
}

struct Provenance {
    name: String,
    container_id: u64,
    toc_entries: usize,
    package_chunks: usize,
    utoc_bytes: Option<u64>,
    ucas_bytes: Option<u64>,
    ucas_modified_unix: Option<u64>,
}

fn main() {
    let args = match parse_args() {
        Ok(a) => a,
        Err(msg) => {
            if !msg.is_empty() {
                eprintln!("error: {msg}");
            }
            eprintln!("{USAGE}");
            std::process::exit(2);
        }
    };
    match run(&args) {
        Ok(code) => std::process::exit(code),
        Err(msg) => {
            eprintln!("error: {msg}");
            std::process::exit(2);
        }
    }
}

fn run(args: &Args) -> Result<i32, String> {
    let mut counts = Counts::default();
    let (utocs, legacy) = discover(&args.paks)?;
    counts.legacy_paks_not_read = legacy.len();

    let global_path = args.paks.join("global.utoc");
    if !global_path.is_file() {
        return Err(format!("{}: not found", global_path.display()));
    }
    let (script, global) = load_script_objects(&global_path)?;
    let check = script.verify();
    counts.script_objects = check.objects;
    counts.script_paths_resolved = check.paths_resolved;
    counts.script_hash_matches = check.hash_matches;
    counts.script_hash_mismatches = check.hash_mismatches;

    let mut containers = Vec::new();
    // Listed although it holds no package this tool reads: every `/Script`
    // path in the output comes from it.
    let mut provenance = vec![provenance_of(&global, &global_path, 0)];
    let mut jobs = Vec::new();
    for utoc in &utocs {
        if utoc
            .file_name()
            .is_some_and(|n| n.eq_ignore_ascii_case("global.utoc"))
        {
            continue;
        }
        let container = Container::open(utoc).map_err(|e| e.to_string())?;
        let ci = containers.len();
        counts.containers += 1;
        counts.toc_entries += container.toc.chunk_ids.len();

        let mut by_entry: HashMap<u32, String> = HashMap::new();
        if !container.toc.directory_index.is_empty() {
            let index = dirindex::parse_directory_index(&container.toc.directory_index)
                .and_then(|d| d.files())
                .map_err(|e| format!("{}: {e}", container.name))?;
            counts.indexed_files += index.len();
            for (path, entry) in index {
                if entry as usize >= container.toc.chunk_ids.len()
                    || by_entry.insert(entry, path).is_some()
                {
                    counts.indexed_files_dropped += 1;
                }
            }
        }
        let mut packages_here = 0usize;
        for (entry, id) in container.toc.chunk_ids.iter().enumerate() {
            let file = by_entry.remove(&(entry as u32));
            if id.chunk_type != CHUNK_EXPORT_BUNDLE_DATA {
                if file.as_deref().is_some_and(is_package_file) {
                    counts.package_files_not_package_chunks += 1;
                }
                continue;
            }
            packages_here += 1;
            if file.is_none() {
                counts.package_chunks_unindexed += 1;
            }
            jobs.push(Job {
                container: ci,
                entry,
                file,
            });
        }
        counts.package_chunks += packages_here;
        provenance.push(provenance_of(&container, utoc, packages_here));
        containers.push(container);
    }
    if containers.is_empty() {
        return Err(format!(
            "{}: no .utoc containers besides global",
            args.paks.display()
        ));
    }

    let (scans, failures) = scan_all(&containers, &jobs, &script, args.jobs);
    counts.packages_failed = failures.len();

    // Second pass: every class object from every package, so a component
    // whose class is a Blueprint in another package can be followed there.
    let mut classes = ClassTable::new();
    for (job, scan) in &scans {
        counts.packages_read += 1;
        counts.exports += scan.exports;
        if scan.package_id_matches {
            counts.package_id_matches += 1;
        } else {
            counts.package_id_mismatches += 1;
        }
        match scan.file_stem_matches {
            Some(true) => counts.file_stem_matches += 1,
            Some(false) => counts.file_stem_mismatches += 1,
            None => {}
        }
        let id = containers[job.container].toc.chunk_ids[job.entry].id;
        for (hash, class) in &scan.classes {
            counts.class_objects += 1;
            if classes.insert((id, *hash), class.clone()).is_some() {
                counts.duplicate_class_keys += 1;
            }
        }
    }

    let mut rows = Vec::new();
    for (job, scan) in &scans {
        for cand in &scan.candidates {
            match cand.kind {
                "gen_variable" => counts.gen_variable += 1,
                _ => counts.cdo_subobject += 1,
            }
            if cand.numbered {
                counts.gen_variable_numbered += 1;
            }
            let Resolved {
                class,
                class_kind,
                native_class,
                class_ref,
            } = resolve(&cand.class, &script, &classes);
            *counts.class_kinds.entry(class_kind).or_insert(0) += 1;
            if args.kind.as_deref().is_some_and(|k| k != cand.kind) {
                continue;
            }
            if !args.names.is_empty() && !args.names.contains(&cand.instance) {
                continue;
            }
            rows.push(Row {
                instance: cand.instance.clone(),
                kind: cand.kind,
                asset: scan.name.clone(),
                export: cand.export.clone(),
                class,
                class_kind,
                native_class,
                outer: cand.outer.clone(),
                class_ref,
                container: containers[job.container].name.clone(),
            });
        }
    }
    rows.sort();

    let text = match args.format {
        Format::Tsv => render_tsv(&rows),
        Format::Json => render_json(&rows, &counts, &provenance, &args.paks),
    };
    match &args.out {
        Some(path) => {
            std::fs::write(path, text.as_bytes()).map_err(|e| format!("{}: {e}", path.display()))?
        }
        None => {
            let stdout = std::io::stdout();
            let mut lock = stdout.lock();
            lock.write_all(text.as_bytes())
                .and_then(|_| lock.flush())
                .map_err(|e| format!("stdout: {e}"))?;
        }
    }

    let failure_lines: Vec<String> = failures
        .iter()
        .map(|(job, e)| {
            format!(
                "{} entry {} ({}): {e}",
                containers[job.container].name,
                job.entry,
                job.file.as_deref().unwrap_or("unindexed")
            )
        })
        .collect();
    report(&counts, &provenance, &legacy, &failure_lines, &rows, args);
    let failed = counts.packages_failed > 0
        || counts.package_id_mismatches > 0
        || counts.script_hash_mismatches > 0
        || counts.script_paths_resolved != counts.script_objects
        || counts.packages_read == 0;
    if failed {
        eprintln!(
            "FAILED: the listing above is incomplete or a self-check did not hold; \
             see the counters. Rows that could be read were still written."
        );
        return Ok(1);
    }
    eprintln!("OK: every package read, every self-check held");
    Ok(0)
}

fn is_package_file(path: &str) -> bool {
    let lower = path.to_ascii_lowercase();
    lower.ends_with(".uasset") || lower.ends_with(".umap")
}

/// The `.utoc` files and the legacy `.pak` files in `dir`, each sorted by name.
/// Legacy paks are listed, not read, so the summary can say what was skipped.
fn discover(dir: &Path) -> Result<(Vec<PathBuf>, Vec<PathBuf>), String> {
    let entries = std::fs::read_dir(dir).map_err(|e| format!("{}: {e}", dir.display()))?;
    let mut utocs = Vec::new();
    let mut paks = Vec::new();
    for entry in entries {
        let path = entry.map_err(|e| format!("{}: {e}", dir.display()))?.path();
        let ext = path
            .extension()
            .map(|e| e.to_string_lossy().to_ascii_lowercase())
            .unwrap_or_default();
        match ext.as_str() {
            "utoc" => utocs.push(path),
            "pak" => paks.push(path),
            _ => {}
        }
    }
    utocs.sort();
    paks.sort();
    if utocs.is_empty() {
        return Err(format!("{}: no .utoc files", dir.display()));
    }
    Ok((utocs, paks))
}

/// Which files a run read, so its output can be matched to them.
fn provenance_of(container: &Container, utoc: &Path, package_chunks: usize) -> Provenance {
    Provenance {
        name: container.name.clone(),
        container_id: container.toc.container_id,
        toc_entries: container.toc.chunk_ids.len(),
        package_chunks,
        utoc_bytes: file_len(utoc),
        ucas_bytes: file_len(&container.ucas_path),
        ucas_modified_unix: modified(&container.ucas_path),
    }
}

fn load_script_objects(global: &Path) -> Result<(ScriptObjects, Container), String> {
    let container = Container::open(global).map_err(|e| e.to_string())?;
    let entries: Vec<usize> = container
        .toc
        .chunk_ids
        .iter()
        .enumerate()
        .filter(|(_, id)| id.chunk_type == CHUNK_SCRIPT_OBJECTS)
        .map(|(i, _)| i)
        .collect();
    if entries.len() != 1 {
        return Err(format!(
            "{}: {} script object chunks, expected exactly 1",
            global.display(),
            entries.len()
        ));
    }
    let mut ucas = container.open_ucas().map_err(|e| e.to_string())?;
    let bytes = container
        .read_chunk(&mut ucas, entries[0], u64::MAX)
        .map_err(|e| e.to_string())?;
    let script = parse_script_objects(&bytes).map_err(|e| format!("{}: {e}", global.display()))?;
    Ok((script, container))
}

type Scanned = Vec<(Job, PackageScan)>;
type Failed = Vec<(Job, String)>;

/// Read every package on `workers` threads. Results come back in job order, so
/// the output does not depend on scheduling.
fn scan_all(
    containers: &[Container],
    jobs: &[Job],
    script: &ScriptObjects,
    workers: usize,
) -> (Scanned, Failed) {
    let next = AtomicUsize::new(0);
    let results: Mutex<Vec<(usize, Result<PackageScan, String>)>> =
        Mutex::new(Vec::with_capacity(jobs.len()));
    std::thread::scope(|s| {
        for _ in 0..workers.min(jobs.len().max(1)) {
            s.spawn(|| {
                let mut handles: Vec<Option<std::fs::File>> =
                    (0..containers.len()).map(|_| None).collect();
                let mut local = Vec::new();
                loop {
                    let i = next.fetch_add(1, Ordering::Relaxed);
                    let Some(job) = jobs.get(i) else { break };
                    let container = &containers[job.container];
                    let outcome = match &mut handles[job.container] {
                        Some(file) => Ok(file),
                        slot => container.open_ucas().map(|file| slot.insert(file)),
                    }
                    .and_then(|file| scan_package(container, file, job, script))
                    .map_err(|e| e.to_string());
                    local.push((i, outcome));
                }
                results
                    .lock()
                    .unwrap_or_else(|p| p.into_inner())
                    .extend(local);
            });
        }
    });
    let mut all = results.into_inner().unwrap_or_else(|p| p.into_inner());
    all.sort_by_key(|(i, _)| *i);
    let mut ok = Vec::new();
    let mut failed = Vec::new();
    for (i, outcome) in all {
        match outcome {
            Ok(scan) => ok.push((jobs[i].clone(), scan)),
            Err(e) => failed.push((jobs[i].clone(), e)),
        }
    }
    (ok, failed)
}

/// A file's size, `None` when its metadata cannot be read: printed as `?` and
/// as JSON `null`, never as a plausible 0. So is `modified`.
fn file_len(path: &Path) -> Option<u64> {
    std::fs::metadata(path).map(|m| m.len()).ok()
}

/// Modification time in seconds since the Unix epoch.
fn modified(path: &Path) -> Option<u64> {
    let time = std::fs::metadata(path).and_then(|m| m.modified()).ok()?;
    time.duration_since(UNIX_EPOCH).ok().map(|d| d.as_secs())
}

fn text_or_absent(n: Option<u64>) -> String {
    n.map_or_else(|| "?".to_owned(), |n| n.to_string())
}

fn json_or_null(n: Option<u64>) -> String {
    n.map_or_else(|| "null".to_owned(), |n| n.to_string())
}

const COLUMNS: [&str; 10] = [
    "instance",
    "kind",
    "class",
    "class_kind",
    "native_class",
    "asset",
    "export",
    "outer",
    "class_ref",
    "container",
];

fn row_fields(r: &Row) -> [&str; 10] {
    [
        &r.instance,
        r.kind,
        &r.class,
        r.class_kind,
        &r.native_class,
        &r.asset,
        &r.export,
        &r.outer,
        &r.class_ref,
        &r.container,
    ]
}

fn render_tsv(rows: &[Row]) -> String {
    let mut out = COLUMNS.join("\t");
    out.push('\n');
    for r in rows {
        let fields: Vec<String> = row_fields(r).iter().map(|f| tsv_escape(f)).collect();
        out.push_str(&fields.join("\t"));
        out.push('\n');
    }
    out
}

/// Tabs, newlines and backslashes cannot appear raw in a TSV cell.
fn tsv_escape(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    for ch in s.chars() {
        match ch {
            '\\' => out.push_str("\\\\"),
            '\t' => out.push_str("\\t"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            c => out.push(c),
        }
    }
    out
}

fn json_string(out: &mut String, s: &str) {
    out.push('"');
    for ch in s.chars() {
        match ch {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if (c as u32) < 0x20 || (c as u32) > 0x7e => {
                let mut units = [0u16; 2];
                for u in c.encode_utf16(&mut units) {
                    let _ = write!(out, "\\u{u:04x}");
                }
            }
            c => out.push(c),
        }
    }
    out.push('"');
}

fn render_json(rows: &[Row], counts: &Counts, provenance: &[Provenance], paks: &Path) -> String {
    let mut out = String::from("{\n  \"paks_dir\": ");
    json_string(&mut out, &paks.display().to_string());
    out.push_str(",\n  \"containers\": [");
    for (i, p) in provenance.iter().enumerate() {
        out.push_str(if i == 0 { "\n    {" } else { ",\n    {" });
        out.push_str("\"name\": ");
        json_string(&mut out, &p.name);
        let _ = write!(
            out,
            ", \"container_id\": \"{:#018x}\", \"toc_entries\": {}, \"package_chunks\": {}, \
             \"utoc_bytes\": {}, \"ucas_bytes\": {}, \"ucas_modified_unix\": {}}}",
            p.container_id,
            p.toc_entries,
            p.package_chunks,
            json_or_null(p.utoc_bytes),
            json_or_null(p.ucas_bytes),
            json_or_null(p.ucas_modified_unix)
        );
    }
    out.push_str("\n  ],\n  \"counts\": {");
    for (i, (k, v)) in counts.pairs().iter().enumerate() {
        out.push_str(if i == 0 { "\n    " } else { ",\n    " });
        json_string(&mut out, k);
        let _ = write!(out, ": {v}");
    }
    out.push_str("\n  },\n  \"rows\": [");
    for (i, r) in rows.iter().enumerate() {
        out.push_str(if i == 0 { "\n    {" } else { ",\n    {" });
        for (j, (col, val)) in COLUMNS.iter().zip(row_fields(r)).enumerate() {
            if j > 0 {
                out.push_str(", ");
            }
            json_string(&mut out, col);
            out.push_str(": ");
            json_string(&mut out, val);
        }
        out.push('}');
    }
    out.push_str("\n  ]\n}\n");
    out
}

fn report(
    counts: &Counts,
    provenance: &[Provenance],
    legacy: &[PathBuf],
    failures: &[String],
    rows: &[Row],
    args: &Args,
) {
    let mut err = String::new();
    let _ = writeln!(err, "paks: {}", args.paks.display());
    for p in provenance {
        let _ = writeln!(
            err,
            "  {:<32} id {:#018x}  {:>7} chunks  {:>6} packages  ucas {:>12} bytes  modified {}",
            p.name,
            p.container_id,
            p.toc_entries,
            p.package_chunks,
            text_or_absent(p.ucas_bytes),
            text_or_absent(p.ucas_modified_unix)
        );
    }
    for pak in legacy {
        let _ = writeln!(
            err,
            "  not read (legacy .pak): {}",
            pak.file_name()
                .map(|n| n.to_string_lossy())
                .unwrap_or_default()
        );
    }
    for (k, v) in counts.pairs() {
        let _ = writeln!(err, "  {k:<36} {v}");
    }
    let _ = writeln!(err, "  rows written                         {}", rows.len());
    for line in failures.iter().take(20) {
        let _ = writeln!(err, "  failed: {line}");
    }
    if failures.len() > 20 {
        let _ = writeln!(err, "  ... and {} more failures", failures.len() - 20);
    }
    if !args.names.is_empty() {
        let found: BTreeSet<&str> = rows.iter().map(|r| r.instance.as_str()).collect();
        for name in &args.names {
            if !found.contains(name.as_str()) {
                let _ = writeln!(
                    err,
                    "  not found: {name} (no {} export has this instance name)",
                    args.kind
                        .as_deref()
                        .unwrap_or("gen_variable or cdo_subobject")
                );
            }
        }
    }
    eprint!("{err}");
}

#[cfg(test)]
mod tests {
    use super::*;

    /// A one-chunk TOC over stored (uncompressed) bytes.
    fn stored_toc(len: usize, chunk_type: u8, directory_index: Vec<u8>) -> Vec<u8> {
        use crate::toc::tests::{TocSpec, build_toc, one_chunk_toc};
        let (l, n) = (len as u64, len as u32);
        build_toc(&TocSpec {
            directory_index,
            ..one_chunk_toc(chunk_type, (0, l), 0x10000, vec![], &[(0, n, n, 0)])
        })
    }

    /// Run the tool with `--format json` over a synthetic Paks directory: a
    /// global container and one `other` whose single chunk is not a package,
    /// so the run fails but still reports. Returns the exit code and the JSON.
    fn run_synthetic(test: &str, other_index: Vec<u8>) -> (Result<i32, String>, String) {
        use crate::script::tests::script_from_paths;
        let dir = std::env::temp_dir().join(format!("ecc-{test}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        let script = script_from_paths(&["/Script/A"]);
        let global = stored_toc(script.len(), CHUNK_SCRIPT_OBJECTS, Vec::new());
        std::fs::write(dir.join("global.utoc"), global).unwrap();
        std::fs::write(dir.join("global.ucas"), &script).unwrap();
        std::fs::write(dir.join("other.utoc"), stored_toc(4, 2, other_index)).unwrap();
        std::fs::write(dir.join("other.ucas"), [0u8; 4]).unwrap();
        let out = dir.join("out.json");
        let args = Args {
            paks: dir.clone(),
            format: Format::Json,
            kind: None,
            names: BTreeSet::new(),
            jobs: 1,
            out: Some(out.clone()),
        };
        let code = run(&args);
        let json = std::fs::read_to_string(&out);
        std::fs::remove_dir_all(&dir).unwrap();
        (code, json.unwrap())
    }

    #[test]
    fn provenance_lists_the_global_container() {
        let (code, json) = run_synthetic("provenance", Vec::new());
        assert_eq!(code, Ok(1));
        let names: Vec<&str> = json
            .match_indices("\"name\": \"")
            .map(|(at, key)| {
                let rest = &json[at + key.len()..];
                &rest[..rest.find('"').unwrap()]
            })
            .collect();
        assert_eq!(names, ["global", "other"]);
    }

    #[test]
    fn directory_index_files_that_name_no_entry_of_their_own_are_counted() {
        use crate::dirindex::tests::build_index;
        const NONE: u32 = u32::MAX;
        // One directory holding three files, for TOC entries 0, 0 and 9; the
        // TOC has one entry.
        let index = build_index(
            "../../../",
            &[(NONE, NONE, NONE, 0)],
            &[(0, 1, 0), (1, 2, 0), (2, NONE, 9)],
            &["a.ubulk", "b.ubulk", "c.ubulk"],
        );
        let (code, json) = run_synthetic("dropped", index);
        assert_eq!(code, Ok(1));
        assert!(json.contains("\"indexed_files\": 3,"), "{json}");
        assert!(json.contains("\"indexed_files_dropped\": 2,"), "{json}");
        let (_, json) = run_synthetic("none-dropped", Vec::new());
        assert!(json.contains("\"indexed_files_dropped\": 0,"), "{json}");
    }

    #[test]
    fn a_size_or_time_that_cannot_be_read_is_absent_not_zero() {
        let missing = Path::new("no such dir/no such file.ucas");
        assert_eq!((file_len(missing), modified(missing)), (None, None));
        assert_eq!(text_or_absent(file_len(missing)), "?");
        assert_eq!(json_or_null(modified(missing)), "null");
        // Tests run from the package root.
        let here = Path::new("Cargo.toml");
        let len = std::fs::metadata(here).unwrap().len();
        assert_eq!(text_or_absent(file_len(here)), len.to_string());
        assert!(modified(here).is_some_and(|t| t > 1_700_000_000));
    }

    #[test]
    fn every_class_kind_prints_zeros_and_strays_included() {
        let mut counts = Counts::default();
        counts.class_kinds.insert("cycle", 2);
        let pairs = counts.pairs();
        let kinds: Vec<(&str, usize)> = pairs
            .iter()
            .filter_map(|(k, n)| Some((k.strip_prefix("class_kind.")?, *n)))
            .collect();
        let known: Vec<(&str, usize)> = CLASS_KINDS.iter().map(|k| (*k, 0)).collect();
        assert_eq!(kinds[..8], known);
        assert_eq!(kinds[8..], [("cycle", 2)]);
    }

    #[test]
    fn json_and_tsv_escape_what_they_must() {
        let mut s = String::new();
        json_string(&mut s, "a\"b\\c\td\u{e9}");
        assert_eq!(s, "\"a\\\"b\\\\c\\td\\u00e9\"");
        assert_eq!(tsv_escape("a\tb\\c\n"), "a\\tb\\\\c\\n");
    }
}
