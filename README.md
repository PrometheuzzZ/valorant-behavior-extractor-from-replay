# valorant-sens (vrfkit fork)

Changes relative to upstream vrfkit:

- `crates/vrfkit/src/sens.rs` - sensitivity estimation (sharpest peak of the
  yaw lattice), agent names (valorant-api.com), ranks, K/D/A, crosshair profile
  name, CSV export, network error diagnostics.
- `crates/vrfkit/src/cli.rs` - `sens` subcommand, drag-and-drop / double-click mode.
- `crates/vrfkit/src/main.rs`, `src/driver/*.rs` - QUIET flag for a silent export.
- `crates/vrfkit/Cargo.toml` - arrow-array / parquet dependencies.

## Building on Windows

    cargo build --release -p vrfkit
    target\release\vrfkit.exe sens path\to\replay.vrf

## Cross-building from Linux

    RUSTC_BOOTSTRAP=1 cargo build --release -p vrfkit -Zbuild-std=std,panic_abort --target x86_64-pc-windows-gnu

Requires mingw-w64 and rust-src. With `rustup target add x86_64-pc-windows-gnu`
available, the -Zbuild-std flags are not needed.

Tests: `cargo test --release -p vrfkit sens::`
