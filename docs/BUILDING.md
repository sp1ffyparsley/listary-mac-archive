# Build and test guide

## Prerequisites

Use Rust **1.97.1** from rust-toolchain.toml (minimal profile plus rustfmt and Clippy). The workspace uses edition 2024 and Cargo.lock. Native RocksDB needs a working C++ toolchain and libclang/LLVM; a first build is not lightweight.

On Windows, tools/run-core-validation.ps1 expects Visual Studio C++ Build Tools discoverable through vswhere and LLVM under the documented Program Files location. Inspect the script and adjust your own environment rather than copying a private machine configuration. On macOS install Xcode Command Line Tools and the matching Rust target, then inspect tools/macos_bridge_bootstrap.py and its tests. No Mac production app is bundled.

```sh
cargo metadata --locked --format-version 1 --no-deps
cargo fmt --all -- --check
cargo clippy --locked --workspace --all-targets -- -D warnings
cargo test --locked --workspace
cargo test --locked -p listary-core --features test-fault-injection --test persistence_faults -- --test-threads=1
cargo test --locked -p listary-core --features test-fault-injection --test portable_config_faults -- --test-threads=1
cargo build --locked --release --workspace
```

Use a disposable output/cache location through CARGO_TARGET_DIR when appropriate. Remove temporary benchmarks after retaining a compact result. Tests under rust and tools use fixtures; never point them at a personal catalog.

## Python helper checks

Python 3.10+ is required by type syntax. The archived development requirement is pinned in requirements-dev.txt; publication checks may use an already installed pytest version and must record it.

```sh
python -m venv .venv
# Activate the environment using your shell's normal command.
python -m pip install -r requirements-dev.txt
python -m pytest tools/tests -q
python tools/macos_bridge_bootstrap.py --plan --json
```

The plan command does not compile or exercise a Mac. Acceptance helpers deliberately reject inappropriate host operations. The integration snapshots are not part of the tools-only pytest suite; their host dependencies are described separately.

## Deliberate local service use

Inspect `cargo run --locked -p listary-agent -- --help` before using the resident service. Select an explicit isolated state directory and only roots you own/authorize; do not silently ingest a private workspace catalog. Install/uninstall scripts change local startup state and are not needed for source inspection. Never publish generated endpoint files.

## Native Mac work

Build the Rust core/FFI on the actual Mac, run bridge preflight and disposable smoke checks, then link the resulting native library to a real shell. Verify buffer lifecycle and cancellation, Finder/Open-Save navigation with post-action readback, FSEvents recovery, focus, Accessibility permissions and signing. A static bridge plan or Windows DLL test is not this acceptance.
