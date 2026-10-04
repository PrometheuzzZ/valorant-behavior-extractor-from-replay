//! `sens` subcommand: estimate every player's in-game mouse sensitivity.
//!
//! Method: VALORANT turns the camera by `0.07 deg * sens` per mouse count, and
//! the replay stores yaw quantized to 16 bits (360/65536 deg). Every per-tick
//! yaw change is therefore an integer number of mouse steps (plus rounding).
//! We histogram the integer yaw deltas and scan candidate step sizes S for the
//! one whose lattice the deltas sit on (phase coherence |mean exp(2*pi*i*d/S)|).
//! Integer data cannot tell S from S/(S-1) (aliasing), so both are reported
//! when the alias is a plausible sensitivity.

use std::collections::HashMap;
use std::fs::{self, File};
use std::io::Write;
use std::path::{Path, PathBuf};
use std::process::Command;

use arrow_array::cast::AsArray;
use arrow_array::types::{Float32Type, Int32Type, Int64Type, UInt32Type};
use arrow_array::{Array, RecordBatch};
use parquet::arrow::ProjectionMask;
use parquet::arrow::arrow_reader::ParquetRecordBatchReaderBuilder;

use crate::error::CliError;

const UNIT: f64 = 360.0 / 65536.0;
const YAW_PER_COUNT: f64 = 0.07;
const MAX_DELTA: i64 = 2000;

fn err(msg: impl Into<String>) -> CliError {
    CliError::Usage(msg.into())
}

/// Read selected columns of a parquet file as record batches.
fn read_batches(path: &Path, cols: &[&str]) -> Result<Vec<RecordBatch>, CliError> {
    let file = File::open(path)?;
    let builder = ParquetRecordBatchReaderBuilder::try_new(file)
        .map_err(|e| err(format!("parquet: {e}")))?;
    let schema = builder.parquet_schema();
    let idx: Vec<usize> = cols
        .iter()
        .map(|c| {
            (0..schema.num_columns())
                .find(|&i| schema.column(i).name() == *c)
                .ok_or_else(|| err(format!("column {c} missing in {}", path.display())))
        })
        .collect::<Result<_, _>>()?;
    let mask = ProjectionMask::leaves(schema, idx);
    let reader = builder
        .with_projection(mask)
        .with_batch_size(65536)
        .build()
        .map_err(|e| err(format!("parquet: {e}")))?;
    reader
        .map(|b| b.map_err(|e| err(format!("parquet: {e}"))))
        .collect()
}

/// Value of a dictionary<int32,string> column at row `i`.
fn dict_str(col: &dyn Array, i: usize) -> Option<String> {
    if col.is_null(i) {
        return None;
    }
    let d = col.as_dictionary::<Int32Type>();
    let key = d.keys().value(i) as usize;
    Some(d.values().as_string::<i32>().value(key).to_string())
}

struct Player {
    actor: u32,
    subject: String,
    character: u32,
    agent: String,
    agent_code: String,
    rank: Option<i64>,
    kills: Option<i64>,
    deaths: Option<i64>,
    assists: Option<i64>,
    crosshair: Option<String>,
    est: Option<Estimate>,
    beh: Option<Behavior>,
    crouch_times: Vec<u32>,
}

/// Per-round motor habits, counted only while the player is alive inside a
/// window: barriers down (phase 4) to death or round decided (next phase).
/// Rounds with <10 s alive are skipped. Rates are medians over rounds,
/// per-round counts are means.
#[derive(Clone)]
struct Behavior {
    rounds: usize,
    combat_window: bool,
    crouch_total: usize,
    total_rounds: usize,
    total_alive_s: f64,
    jumps_total: usize,
    flicks_total: usize,
    crouch_pr: f64,
    jumps_pr: f64,
    flicks_pr: f64,
    crouch_pm: f64,
    jumps_pm: f64,
    flicks_pm: f64,
    still: f64,
    yaw_p50: f64,
    yaw_p90: f64,
}

/// One movement sample: (time_ms, tick, yaw, vel_x, vel_y, vel_z).
type Sample = (u32, u32, f32, f32, f32, f32);

fn median(mut v: Vec<f64>) -> f64 {
    v.retain(|x| x.is_finite());
    if v.is_empty() {
        return f64::NAN;
    }
    v.sort_by(|a, b| a.partial_cmp(b).unwrap());
    let n = v.len();
    if n % 2 == 1 { v[n / 2] } else { (v[n / 2 - 1] + v[n / 2]) / 2.0 }
}

/// Linear-interpolated percentile (numpy default), `q` in 0..=100.
fn percentile(mut v: Vec<f64>, q: f64) -> f64 {
    if v.is_empty() {
        return f64::NAN;
    }
    v.sort_by(|a, b| a.partial_cmp(b).unwrap());
    let pos = q / 100.0 * (v.len() - 1) as f64;
    let (lo, hi) = (pos.floor() as usize, pos.ceil() as usize);
    v[lo] + (v[hi] - v[lo]) * (pos - lo as f64)
}

const FLICK_DEG_S: f64 = 600.0;
const JUMP_VZ: f32 = 250.0;

fn behavior(samples: &[Sample], windows: &[(u64, u64)], combat_window: bool, deaths: &[u32], crouch: &[u32]) -> Option<Behavior> {
    let mut per: Vec<[f64; 9]> = Vec::new();
    // Exact totals over every window the player was alive in (no 10 s filter).
    let (mut t_rounds, mut t_alive_s, mut t_crouch, mut t_jumps, mut t_flicks) = (0usize, 0.0f64, 0usize, 0usize, 0usize);
    for &(t0, t1) in windows {
        let tend = deaths.iter().map(|&d| u64::from(d)).find(|&d| d >= t0 && d < t1).unwrap_or(t1);
        let g: Vec<&Sample> = samples
            .iter()
            .filter(|x| u64::from(x.0) >= t0 && u64::from(x.0) < tend)
            .collect();
        if g.len() < 2 {
            continue;
        }
        // Turn speed per valid tick pair; `fast` keeps the tick order (invalid pairs = false)
        // so one continuous fast movement counts as ONE flick.
        let mut w = Vec::with_capacity(g.len());
        let mut fast = Vec::with_capacity(g.len());
        for k in 1..g.len() {
            let dt = (f64::from(g[k].1) - f64::from(g[k - 1].1)) / 128.0;
            if dt > 0.0 && dt < 0.1 {
                let (a, b) = ((f64::from(g[k - 1].2) / UNIT).round() as i64, (f64::from(g[k].2) / UNIT).round() as i64);
                let d = ((b - a + 32768).rem_euclid(65536) - 32768) as f64 * UNIT;
                let v = d.abs() / dt;
                w.push(v);
                fast.push(v > FLICK_DEG_S);
            } else {
                fast.push(false);
            }
        }
        let flicks = (0..fast.len()).filter(|&k| fast[k] && (k == 0 || !fast[k - 1])).count();
        let jumps = (1..g.len()).filter(|&k| g[k].5 > JUMP_VZ && g[k - 1].5 <= JUMP_VZ).count();
        let crouches = crouch.iter().filter(|&&c| u64::from(c) >= t0 && u64::from(c) < tend).count();
        t_rounds += 1;
        t_alive_s += w.len() as f64 / 128.0;
        t_crouch += crouches;
        t_jumps += jumps;
        t_flicks += flicks;
        if g.len() < 128 * 10 || w.len() < 128 {
            continue;
        }
        let alive_min = w.len() as f64 / 128.0 / 60.0;
        let moving: Vec<f64> = w.iter().copied().filter(|&x| x > 1.0).collect();
        per.push([
            crouches as f64 / alive_min,
            jumps as f64 / alive_min,
            flicks as f64 / alive_min,
            w.iter().filter(|&&x| x < 1.0).count() as f64 / w.len() as f64,
            if moving.is_empty() { f64::NAN } else { median(moving.clone()) },
            percentile(moving, 90.0),
            crouches as f64,
            jumps as f64,
            flicks as f64,
        ]);
    }
    if per.is_empty() {
        return None;
    }
    let col = |i: usize| median(per.iter().map(|r| r[i]).collect());
    let mean = |i: usize| per.iter().map(|r| r[i]).sum::<f64>() / per.len() as f64;
    Some(Behavior {
        rounds: per.len(),
        combat_window,
        crouch_total: t_crouch,
        total_rounds: t_rounds,
        total_alive_s: t_alive_s,
        jumps_total: t_jumps,
        flicks_total: t_flicks,
        crouch_pr: mean(6),
        jumps_pr: mean(7),
        flicks_pr: mean(8),
        crouch_pm: col(0),
        jumps_pm: col(1),
        flicks_pm: col(2),
        still: col(3),
        yaw_p50: col(4),
        yaw_p90: col(5),
    })
}

fn map_name(code: &str) -> String {
    match code {
        "Ascent" => "Ascent", "Bonsai" => "Split", "Triad" => "Haven", "Duality" => "Bind",
        "Port" => "Icebox", "Foxtrot" => "Breeze", "Canyon" => "Fracture", "Pitt" => "Pearl",
        "Jam" => "Lotus", "Juliett" => "Sunset", "Infinity" => "Abyss", "Rook" => "Corrode",
        other => return other.to_string(),
    }
    .to_string()
}

fn print_behavior(i: usize, p: &Player) {
    println!();
    println!("#{} {}  ({})", i + 1, p.subject, p.agent);
    match &p.beh {
        None => println!("  not enough alive time to measure"),
        Some(b) => {
            let f = |v: f64, d: usize| if v.is_finite() { format!("{v:.d$}") } else { "?".into() };
            println!(
                "  Window: {}",
                if b.combat_window { "barriers down -> death or round end" } else { "round start -> death (no phase data in replay)" }
            );
            println!("  Rounds measured (alive >= 10 s)        {}", b.rounds);
            println!("                                         per minute   per round");
            println!("  Crouches                               {:<12} {}", f(b.crouch_pm, 2), f(b.crouch_pr, 2));
            println!("  Jumps                                  {:<12} {}", f(b.jumps_pm, 2), f(b.jumps_pr, 2));
            println!("  Flicks (> 600 deg/s)                   {:<12} {}", f(b.flicks_pm, 1), f(b.flicks_pr, 1));
            println!("  Share of time with mouse still         {}", f(b.still, 2));
            println!("  Typical turn speed, deg/s (median)     {}", f(b.yaw_p50, 1));
            println!("  Fast turns, deg/s (90th percentile)    {}", f(b.yaw_p90, 1));
            println!("  (per minute and shares: medians over rounds; per round: mean)");
            println!();
            println!("  Exact totals for the match (same window, all rounds alive in):");
            let secs = b.total_alive_s.round() as u64;
            println!("  Rounds                                 {}", b.total_rounds);
            println!("  Time alive in combat                   {} min {} s", secs / 60, secs % 60);
            println!("  Crouches                               {}", b.crouch_total);
            println!("  Jumps                                  {}", b.jumps_total);
            println!("  Flicks (> 600 deg/s, one per movement) {}", b.flicks_total);
        }
    }
}

struct Estimate {
    sens: f64,
    alt: Option<f64>,
    peak: f64,
    floor: f64,
    samples: usize,
}

impl Estimate {
    fn confidence(&self) -> &'static str {
        let ratio = self.peak / self.floor.max(1e-9);
        if self.samples < 2000 || self.peak < 0.15 || ratio < 3.0 {
            "no data"
        } else if self.alt.is_some() {
            if self.peak > 0.4 { "medium" } else { "low" }
        } else if (self.peak > 0.4 && ratio > 6.0) || self.peak > 0.7 {
            "high"
        } else {
            "medium"
        }
    }
}

/// Parse `players` from vrfkit's hand-rolled manifest.json (one player per line).
fn parse_manifest_players(text: &str) -> Vec<(u32, String, u32)> {
    let num = |line: &str, key: &str| -> Option<u32> {
        let at = line.find(key)? + key.len();
        let rest = line[at..].trim_start_matches([' ', ':']);
        rest.chars().take_while(|c| c.is_ascii_digit()).collect::<String>().parse().ok()
    };
    let mut out = Vec::new();
    for line in text.lines() {
        if !line.contains("\"subject\"") || !line.contains("\"character_net_guid\"") {
            continue;
        }
        let subject = line
            .split("\"subject\":")
            .nth(1)
            .and_then(|r| r.split('"').nth(1))
            .unwrap_or("")
            .to_string();
        if let (Some(a), Some(c)) = (num(line, "\"actor_net_guid\""), num(line, "\"character_net_guid\"")) {
            out.push((a, subject, c));
        }
    }
    out
}

/// developerName -> displayName from valorant-api.com (one request per run).
fn fetch_agent_names() -> HashMap<String, String> {
    let mut map = HashMap::new();
    let Some(body) = http_get_checked("https://valorant-api.com/v1/agents", &|b: &str| b.contains("developerName")) else { return map };
    // In each agent object "displayName" precedes "developerName"; ability
    // displayNames come after it, so pair each developerName with the nearest
    // preceding displayName.
    let mut last_display: Option<String> = None;
    let mut rest = body.as_str();
    loop {
        let d = rest.find("\"displayName\"");
        let v = rest.find("\"developerName\"");
        match (d, v) {
            (Some(d), Some(v)) if d < v => {
                last_display = json_string(&rest[d..], "displayName");
                rest = &rest[d + 13..];
            }
            (_, Some(v)) => {
                if let (Some(dev), Some(disp)) = (json_string(&rest[v..], "developerName"), last_display.take()) {
                    map.insert(dev, disp);
                }
                rest = &rest[v + 15..];
            }
            _ => break,
        }
    }
    map
}

fn agent_name(code: &str) -> String {
    let n = match code {
        "Sarge" => "Brimstone",
        "Pandemic" => "Viper",
        "Wraith" => "Omen",
        "Killjoy" => "Killjoy",
        "Gumshoe" => "Cypher",
        "Hunter" => "Sova",
        "Thorne" => "Sage",
        "Phoenix" | "Apollo" => "Phoenix",
        "Wushu" => "Jett",
        "Vampire" => "Reyna",
        "Clay" => "Raze",
        "Breach" => "Breach",
        "Guide" => "Skye",
        "Stealth" | "Stealthboi" => "Yoru",
        "Rift" => "Astra",
        "Grenadier" => "KAY/O",
        "Deadeye" => "Chamber",
        "Sprinter" => "Neon",
        "BountyHunter" => "Fade",
        "Mage" => "Harbor",
        "Aggrobot" => "Gekko",
        "Cable" => "Deadlock",
        "Sequoia" => "Iso",
        "Smonk" => "Clove",
        "Nox" => "Vyse",
        "Cashew" => "Tejo",
        "Terra" => "Waylay",
        other => return other.to_string(),
    };
    n.to_string()
}

fn rank_name(tier: i64) -> String {
    const DIV: [&str; 3] = ["1", "2", "3"];
    const NAMES: [&str; 8] = ["Iron", "Bronze", "Silver", "Gold", "Platinum", "Diamond", "Ascendant", "Immortal"];
    match tier {
        i64::MIN..=-1 => "-".into(),
        0..=2 => "Unranked".into(),
        3..=26 => {
            let i = (tier - 3) as usize;
            format!("{} {}", NAMES[i / 3], DIV[i % 3])
        }
        27 => "Radiant".into(),
        t => format!("tier {t}"),
    }
}

/// Phase coherence of integer deltas (given as a sparse histogram) at step `s`.
fn coherence(hist: &[(f64, f64)], total: f64, s: f64) -> f64 {
    let w = std::f64::consts::TAU / s;
    let (mut re, mut im) = (0.0, 0.0);
    for &(k, h) in hist {
        let (sn, cs) = (w * k).sin_cos();
        re += h * cs;
        im += h * sn;
    }
    (re * re + im * im).sqrt() / total
}

fn estimate(yaws: &[f32]) -> Option<Estimate> {
    let mut counts: HashMap<i64, f64> = HashMap::new();
    let mut prev: Option<i64> = None;
    let mut n = 0usize;
    for &y in yaws {
        let q = (f64::from(y) / UNIT).round() as i64;
        if let Some(p) = prev {
            let d = (q - p + 32768).rem_euclid(65536) - 32768;
            if d != 0 && d.abs() < MAX_DELTA {
                *counts.entry(d).or_default() += 1.0;
                n += 1;
            }
        }
        prev = Some(q);
    }
    if n < 500 {
        return None;
    }
    let hist: Vec<(f64, f64)> = counts.into_iter().map(|(k, h)| (k as f64, h)).collect();
    let total = n as f64;
    let to_s = |sens: f64| sens * YAW_PER_COUNT / UNIT;

    // Coarse scan: sensitivities 0.12 .. 6. Below ~0.12 the step nears one
    // 16-bit unit, where every integer delta "fits". A real lattice also gives a
    // *sharp* peak: score = C(s) - max(C(s*0.97), C(s*1.03)) rejects the smooth
    // rise of C at huge steps, where small deltas trivially look aligned.
    let c = |sv: f64| coherence(&hist, total, to_s(sv));
    let mut scan = Vec::new();
    let mut sens = 0.12;
    while sens <= 6.0 {
        let here = c(sens);
        let score = here - c(sens * 0.97).max(c(sens * 1.03));
        scan.push((sens, here, score));
        sens += if sens < 1.0 { 0.0005 } else if sens < 3.0 { 0.001 } else { 0.003 };
    }
    let mut sorted: Vec<f64> = scan.iter().map(|x| x.1).collect();
    sorted.sort_by(|a, b| a.partial_cmp(b).unwrap());
    let floor = sorted[sorted.len() / 2];
    let top = scan.iter().map(|x| x.2).fold(f64::MIN, f64::max);
    if top < 0.08 {
        return None; // no sharp lattice peak anywhere: report "no data", not a guess
    }
    // Largest lattice among near-maximal sharp peaks (true step S also lights up S/2, S/3...).
    let best = scan
        .iter()
        .filter(|x| x.2 >= 0.85 * top)
        .map(|x| x.0)
        .fold(0.0_f64, f64::max);
    let peak = c(best);
    // Fine refine around it.
    let mut fine = (best, 0.0);
    let win = (best * 0.004).max(0.001);
    let mut s = best - win;
    while s <= best + win {
        let c = coherence(&hist, total, to_s(s));
        if c > fine.1 {
            fine = (s, c);
        }
        s += win / 40.0;
    }
    // Snap to a two-decimal value when it fits (almost) as well: players type 0.35, not 0.3512.
    let snapped = (fine.0 * 100.0).round() / 100.0;
    let sens = if coherence(&hist, total, to_s(snapped)) >= 0.95 * fine.1 { snapped } else { fine.0 };
    // Alias partner: S' = S / (S - 1).
    let st = to_s(sens);
    let alt = if st > 1.0 {
        let a = st / (st - 1.0) * UNIT / YAW_PER_COUNT;
        (a >= 0.12 && (a - sens).abs() > 0.002).then_some(a)
    } else {
        None
    };
    // Prefer the "round" one (players type values like 0.35) when ambiguous.
    let roundness = |v: f64| ((v * 100.0) - (v * 100.0).round()).abs();
    let is_round = |v: f64| roundness(v) < 0.05;
    // Without a round one, keep the larger (sens) value first.
    let (sens, alt) = match alt {
        Some(a) if is_round(a) && !is_round(sens) => (a, Some(sens)),
        other => (sens, other),
    };
    // A round value whose alias is not round: the alias is almost surely not what was typed.
    let alt = alt.filter(|&a| !(is_round(sens) && !is_round(a)));
    Some(Estimate { sens, alt, peak: fine.1.max(peak), floor, samples: n })
}

/// Last network error, shown once so failures are not silent.
static NET_ERR: std::sync::Mutex<Option<String>> = std::sync::Mutex::new(None);

fn note_err(e: String) {
    if let Ok(mut g) = NET_ERR.lock() {
        g.get_or_insert(e);
    }
}

const UA: &str = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) valorant-sens/1.1";

/// GET via system curl (Windows 10+), then PowerShell. `valid` decides whether
/// a body is the real answer (not an HTML error page); if not, the next method is tried.
fn http_get_checked(url: &str, valid: &dyn Fn(&str) -> bool) -> Option<String> {
    let run = |cmd: &mut Command, what: &str| -> Option<String> {
        match cmd.output() {
            Err(e) => {
                note_err(format!("{what}: failed to start ({e})"));
                None
            }
            Ok(out) => {
                let body = String::from_utf8_lossy(&out.stdout).trim_start_matches('\u{feff}').to_string();
                if out.status.success() && valid(&body) {
                    Some(body)
                } else {
                    let err = String::from_utf8_lossy(&out.stderr);
                    let snippet: String = body.chars().take(150).collect();
                    note_err(format!(
                        "{what}: exit code {:?}; response: {}; error: {}",
                        out.status.code(),
                        snippet.replace(['\r', '\n'], " "),
                        err.trim().chars().take(200).collect::<String>()
                    ));
                    None
                }
            }
        }
    };
    let mut curl = Command::new("curl");
    curl.args(["-sS", "-L", "--compressed", "-m", "15", "-A", UA]);
    if cfg!(windows) {
        curl.arg("--ssl-no-revoke");
    }
    curl.arg(url);
    if let Some(b) = run(&mut curl, "curl") {
        return Some(b);
    }
    if cfg!(windows) {
        let ps = format!(
            "$ProgressPreference='SilentlyContinue'; [Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12; \
             [Console]::OutputEncoding=[Text.Encoding]::UTF8; \
             $r=Invoke-WebRequest -UseBasicParsing -TimeoutSec 15 -UserAgent '{UA}' '{url}'; \
             [Text.Encoding]::UTF8.GetString($r.RawContentStream.ToArray())"
        );
        if let Some(b) = run(Command::new("powershell").args(["-NoProfile", "-NonInteractive", "-Command", &ps]), "powershell") {
            return Some(b);
        }
    }
    None
}


/// Minimal JSON string field extractor with \uXXXX and escape handling.
fn json_string(body: &str, key: &str) -> Option<String> {
    let pat = format!("\"{key}\"");
    let at = body.find(&pat)? + pat.len();
    let rest = body[at..].trim_start().strip_prefix(':')?.trim_start().strip_prefix('"')?;
    let mut out = String::new();
    let mut chars = rest.chars();
    let mut pending_hi: Option<u32> = None;
    while let Some(c) = chars.next() {
        match c {
            '"' => return Some(out),
            '\\' => match chars.next()? {
                'u' => {
                    let hex: String = chars.by_ref().take(4).collect();
                    let v = u32::from_str_radix(&hex, 16).ok()?;
                    if (0xD800..0xDC00).contains(&v) {
                        pending_hi = Some(v);
                    } else if let (Some(hi), true) = (pending_hi.take(), (0xDC00..0xE000).contains(&v)) {
                        out.extend(char::from_u32(0x10000 + ((hi - 0xD800) << 10) + (v - 0xDC00)));
                    } else {
                        out.extend(char::from_u32(v));
                    }
                }
                'n' => out.push('\n'),
                't' => out.push('\t'),
                other => out.push(other),
            },
            c => out.push(c),
        }
    }
    None
}

fn pad(s: &str, w: usize) -> String {
    let n = s.chars().count();
    if n >= w { s.to_string() } else { format!("{s}{}", " ".repeat(w - n)) }
}

pub fn run(vrf_path: &str, offline: bool, interactive: bool, show_behavior: bool) -> Result<(), CliError> {
    let tmp: PathBuf = std::env::temp_dir().join(format!(
        "vrfsens_{}_{}",
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_nanos())
            .unwrap_or(0)
    ));
    println!("Parsing replay: {vrf_path}");
    let result = crate::driver::run_quiet(vrf_path, tmp.to_str().unwrap_or("vrfsens_tmp"))
        .and_then(|()| analyse(vrf_path, &tmp, offline, interactive, show_behavior));
    let _ = fs::remove_dir_all(&tmp);
    result
}

fn analyse(vrf_path: &str, dir: &Path, offline: bool, interactive: bool, show_behavior: bool) -> Result<(), CliError> {
    let manifest = fs::read_to_string(dir.join("manifest.json"))?;
    let mut players: Vec<Player> = parse_manifest_players(&manifest)
        .into_iter()
        .map(|(actor, subject, character)| Player {
            actor,
            subject,
            character,
            agent: "?".into(),
            agent_code: String::new(),
            rank: None,
            kills: None,
            deaths: None,
            assists: None,
            crosshair: None,
            est: None,
            beh: None,
            crouch_times: Vec::new(),
        })
        .collect();
    if players.is_empty() {
        return Err(err("no players found in the replay"));
    }

    // Round phases from the game state: 3 = buy (barriers up), 4 = barriers down, 5 = round decided.
    let mut phases: Vec<(u32, i64)> = Vec::new();
    // Field rows: agent class (pawn's PlayerState row), rank, K/D/A, crosshair profile.
    let by_actor: HashMap<u32, usize> = players.iter().enumerate().map(|(i, p)| (p.actor, i)).collect();
    let by_char: HashMap<u32, usize> = players.iter().enumerate().map(|(i, p)| (p.character, i)).collect();
    for b in read_batches(&dir.join("fields.parquet"), &["time_ms", "actor_net_guid", "group_path", "field_name", "value_i64", "value_bool", "value_str"])? {
        let ftime = b.column_by_name("time_ms").unwrap().as_primitive::<UInt32Type>();
        let vb = b.column_by_name("value_bool").unwrap().as_boolean();
        let actor = b.column_by_name("actor_net_guid").unwrap().as_primitive::<UInt32Type>();
        let group = b.column_by_name("group_path").unwrap();
        let field = b.column_by_name("field_name").unwrap();
        let vi = b.column_by_name("value_i64").unwrap().as_primitive::<Int64Type>();
        let vs = b.column_by_name("value_str").unwrap();
        for i in 0..b.num_rows() {
            let a = actor.value(i);
            let pi = by_actor.get(&a).copied();
            let ci = by_char.get(&a).copied();
            if pi.is_none() && ci.is_none() {
                if dict_str(field.as_ref(), i).as_deref() == Some("MulticastSetPhase.NewPhase") && !vi.is_null(i) {
                    phases.push((ftime.value(i), vi.value(i)));
                }
                continue;
            }
            let Some(f) = dict_str(field.as_ref(), i) else { continue };
            if let Some(ci) = ci {
                // One row per press (true) and one per release (false): count presses only.
                if f == "bCrouchHeld" && !vb.is_null(i) && vb.value(i) {
                    players[ci].crouch_times.push(ftime.value(i));
                }
                if f == "PlayerState" && players[ci].agent == "?" {
                    if let Some(g) = dict_str(group.as_ref(), i) {
                        // /Game/Characters/<Code>/<Code>_PC.<Code>_PC_C
                        if let Some(code) = g.split('/').nth(3) {
                            players[ci].agent = agent_name(code);
                            players[ci].agent_code = code.to_string();
                        }
                    }
                }
            }
            if let Some(pi) = pi {
                let int = (!vi.is_null(i)).then(|| vi.value(i));
                let p = &mut players[pi];
                match f.as_str() {
                    "CompetitiveTier" => p.rank = int.or(p.rank),
                    "AggregateKills" => p.kills = int.or(p.kills),
                    "AggregateDeaths" => p.deaths = int.or(p.deaths),
                    "AggregateAssists" => p.assists = int.or(p.assists),
                    "ProfileName" => {
                        if let Some(s) = dict_str(vs.as_ref(), i) {
                            p.crosshair = Some(s);
                        }
                    }
                    _ => {}
                }
            }
        }
    }

    // Round starts and deaths (word1 = victim character GUID).
    let mut round_starts: Vec<u32> = Vec::new();
    let mut deaths: HashMap<u32, Vec<u32>> = HashMap::new();
    for b in read_batches(&dir.join("events.parquet"), &["group", "time1", "word1"])? {
        let g = b.column_by_name("group").unwrap();
        let t = b.column_by_name("time1").unwrap().as_primitive::<UInt32Type>();
        let w1 = b.column_by_name("word1").unwrap().as_primitive::<UInt32Type>();
        for i in 0..b.num_rows() {
            match dict_str(g.as_ref(), i).as_deref() {
                Some("roundStarted") => round_starts.push(t.value(i)),
                Some("characterDeath") if !w1.is_null(i) => deaths.entry(w1.value(i)).or_default().push(t.value(i)),
                _ => {}
            }
        }
    }
    round_starts.sort_unstable();
    for v in deaths.values_mut() {
        v.sort_unstable();
    }
    phases.sort_unstable();
    phases.dedup_by_key(|x| x.0);
    let combat: Vec<(u64, u64)> = phases
        .iter()
        .enumerate()
        .filter(|(_, x)| x.1 == 4)
        .map(|(k, x)| (u64::from(x.0), phases.get(k + 1).map_or(u64::MAX, |n| u64::from(n.0))))
        .collect();
    let (windows, combat_window) = if combat.is_empty() {
        let mut v: Vec<(u64, u64)> = round_starts.windows(2).map(|w| (u64::from(w[0]), u64::from(w[1]))).collect();
        if let Some(&l) = round_starts.last() {
            v.push((u64::from(l), u64::MAX));
        }
        (v, false)
    } else {
        (combat, true)
    };

    // Movement per character, in (time_ms, tick) order.
    let mut rows: HashMap<u32, Vec<Sample>> = HashMap::new();
    for b in read_batches(&dir.join("movement.parquet"), &["time_ms", "character_net_guid", "yaw", "timestamp", "vel_x", "vel_y", "vel_z"])? {
        let t = b.column_by_name("time_ms").unwrap().as_primitive::<UInt32Type>();
        let c = b.column_by_name("character_net_guid").unwrap().as_primitive::<UInt32Type>();
        let y = b.column_by_name("yaw").unwrap().as_primitive::<Float32Type>();
        let k = b.column_by_name("timestamp").unwrap().as_primitive::<UInt32Type>();
        let vx = b.column_by_name("vel_x").unwrap().as_primitive::<Float32Type>();
        let vy = b.column_by_name("vel_y").unwrap().as_primitive::<Float32Type>();
        let vz = b.column_by_name("vel_z").unwrap().as_primitive::<Float32Type>();
        for i in 0..b.num_rows() {
            let ch = c.value(i);
            if by_char.contains_key(&ch) {
                rows.entry(ch).or_default().push((t.value(i), k.value(i), y.value(i), vx.value(i), vy.value(i), vz.value(i)));
            }
        }
    }
    for p in &mut players {
        if let Some(mut r) = rows.remove(&p.character) {
            r.sort_by_key(|x| (x.0, x.1));
            let yaws: Vec<f32> = r.iter().map(|x| x.2).collect();
            p.est = estimate(&yaws);
            p.crouch_times.sort_unstable();
            let empty = Vec::new();
            p.beh = behavior(&r, &windows, combat_window, deaths.get(&p.character).unwrap_or(&empty), &p.crouch_times);
        }
    }
    let map = manifest
        .split("/Game/Maps/")
        .nth(1)
        .and_then(|r| r.split('/').next())
        .map(map_name)
        .unwrap_or_else(|| "?".into());

    if !offline {
        println!("Fetching agent names...");
        let api = fetch_agent_names();
        for p in &mut players {
            if let Some(n) = api.get(&p.agent_code) {
                p.agent = n.clone();
            }
        }
        if api.is_empty() {
            println!("Could not fetch agent names from the API, using the built-in table.");
            if let Some(e) = NET_ERR.lock().ok().and_then(|g| g.clone()) {
                println!("Reason: {e}");
            }
        }
    }

    // Print table.
    println!();
    println!("Map: {map}");
    println!(
        "{} {} {} {} {} {} {} {} {}",
        pad("#", 3),
        pad("PUUID", 10), pad("Agent", 10), pad("Rank", 12), pad("K/D/A", 9),
        pad("Sens", 7), pad("Alt", 7), pad("Confidence", 11), "Crosshair profile"
    );
    println!("{}", "-".repeat(108));
    let kda = |p: &Player| {
        let f = |v: Option<i64>| v.map_or("-".into(), |x| x.to_string());
        format!("{}/{}/{}", f(p.kills), f(p.deaths), f(p.assists))
    };
    for (n, p) in players.iter().enumerate() {
        let who = p.subject.chars().take(8).collect::<String>() + "…";
        let (s, alt, conf) = match &p.est {
            Some(e) => (
                if e.confidence() == "no data" { "?".into() } else { format!("{:.3}", e.sens) },
                if e.confidence() == "no data" { String::new() } else { e.alt.map_or(String::new(), |a| format!("{a:.3}")) },
                e.confidence(),
            ),
            None => ("-".into(), String::new(), "no data"),
        };
        println!(
            "{} {} {} {} {} {} {} {} {}",
            pad(&(n + 1).to_string(), 3),
            pad(&who, 10), pad(&p.agent, 10), pad(&p.rank.map_or("-".into(), rank_name), 12),
            pad(&kda(p), 9), pad(&s, 7), pad(&alt, 7), pad(conf, 11),
            p.crosshair.as_deref().map_or("-".to_string(), |c| {
                if c.chars().count() > 40 { c.chars().take(39).collect::<String>() + "…" } else { c.to_string() }
            })
        );
    }

    // CSV next to the replay (UTF-8 with BOM so Excel shows Cyrillic).
    let csv_path = Path::new(vrf_path).with_extension("sens.csv");
    let write_csv = || -> std::io::Result<()> {
        let mut f = File::create(&csv_path)?;
        f.write_all("\u{feff}".as_bytes())?;
        writeln!(f, "map;puuid;agent;rank;kills;deaths;assists;sens;sens_alt;confidence;coherence;crosshair_profile;window;rounds_measured;rounds_total;alive_seconds;crouches_total;jumps_total;flicks_total;crouches_per_min;crouches_per_round;jumps_per_min;jumps_per_round;flicks_per_min;flicks_per_round;mouse_still_share;turn_speed_median;turn_speed_p90")?;
        for p in &players {
            let (s, a, c, coh) = match &p.est {
                Some(e) if e.confidence() == "no data" => (String::new(), String::new(), e.confidence(), format!("{:.3}", e.peak)),
                Some(e) => (format!("{:.3}", e.sens), e.alt.map_or(String::new(), |a| format!("{a:.3}")), e.confidence(), format!("{:.3}", e.peak)),
                None => (String::new(), String::new(), "no data", String::new()),
            };
            let o = |v: Option<i64>| v.map_or(String::new(), |x| x.to_string());
            writeln!(
                f,
                "{};{};{};{};{};{};{};{};{};{};{};{};{}",
                map, p.subject, p.agent,
                p.rank.map_or(String::new(), rank_name), o(p.kills), o(p.deaths), o(p.assists),
                s, a, c, coh, p.crosshair.clone().unwrap_or_default().replace(';', ","),
                p.beh.as_ref().map_or(";;;;;;;;;;;;;;;".to_string(), |b| format!(
                    "{};{};{};{:.0};{};{};{};{:.2};{:.2};{:.2};{:.2};{:.1};{:.1};{:.3};{:.1};{:.1}",
                    if b.combat_window { "combat" } else { "round" },
                    b.rounds, b.total_rounds, b.total_alive_s, b.crouch_total, b.jumps_total, b.flicks_total, b.crouch_pm, b.crouch_pr, b.jumps_pm, b.jumps_pr,
                    b.flicks_pm, b.flicks_pr, b.still, b.yaw_p50, b.yaw_p90
                ))
            )?;
        }
        Ok(())
    };
    println!();
    match write_csv() {
        Ok(()) => println!("Table saved: {}", csv_path.display()),
        Err(e) => println!("Could not save CSV ({e})"),
    }
    println!("Alt = second possible value: at low sensitivity the replay data cannot tell the two apart.");
    if interactive {
        loop {
            println!();
            print!("Player # for behavior stats (1-{}, 'a' = all, Enter = done): ", players.len());
            let _ = std::io::stdout().flush();
            let mut line = String::new();
            if std::io::stdin().read_line(&mut line).unwrap_or(0) == 0 {
                break;
            }
            let line = line.trim();
            if line.is_empty() {
                break;
            }
            if line.eq_ignore_ascii_case("a") {
                for (i, p) in players.iter().enumerate() {
                    print_behavior(i, p);
                }
                continue;
            }
            match line.parse::<usize>() {
                Ok(n) if (1..=players.len()).contains(&n) => print_behavior(n - 1, &players[n - 1]),
                _ => println!("Enter a number from 1 to {}.", players.len()),
            }
        }
    } else if show_behavior {
        for (i, p) in players.iter().enumerate() {
            print_behavior(i, p);
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn json_unicode() {
        let b = r#"{"displayName": "K\u0041Y/O \u00e9"}"#;
        assert_eq!(json_string(b, "displayName").unwrap(), "KAY/O é");
        let b = r#"{"displayName": "Vyse"}"#;
        assert_eq!(json_string(b, "displayName").unwrap(), "Vyse");
    }

    #[test]
    fn agent_pairs() {
        let body = r#"{"data":[{"uuid":"x","displayName":"Vyse","description":"d","developerName":"Nox","abilities":[{"displayName":"Shear"}]},{"displayName":"Waylay","developerName":"Terra"}]}"#;
        // exercise the pairing logic without the network
        let mut map = HashMap::new();
        let mut last: Option<String> = None;
        let mut rest = body;
        loop {
            let d = rest.find("\"displayName\"");
            let v = rest.find("\"developerName\"");
            match (d, v) {
                (Some(d), Some(v)) if d < v => { last = json_string(&rest[d..], "displayName"); rest = &rest[d + 13..]; }
                (_, Some(v)) => { if let (Some(a), Some(b)) = (json_string(&rest[v..], "developerName"), last.take()) { map.insert(a, b); } rest = &rest[v + 15..]; }
                _ => break,
            }
        }
        assert_eq!(map["Nox"], "Vyse");
        assert_eq!(map["Terra"], "Waylay");
    }

    #[test]
    fn ranks() {
        assert_eq!(rank_name(11), "Silver 3");
        assert_eq!(rank_name(15), "Platinum 1");
        assert_eq!(rank_name(27), "Radiant");
    }
}
