# Listary-inspired search — archived independent implementation

**Rust/RocksDB search engine and integration code, not official Listary source or a finished native Mac application.** Active development was retired on **November 13, 2026**. This source-only archive is available for study, reuse and forks under the [MIT license](LICENSE), with no ongoing support commitment.

## Read first

| Question | Guide |
| --- | --- |
| What is implemented, missing or unverified? | [Status](docs/STATUS.md) |
| Where are the engine, service and native API? | [Architecture](docs/ARCHITECTURE.md) |
| How do I build/test without affecting my profile? | [Build guide](docs/BUILDING.md) |
| What did this publication validate? | [Validation](docs/VALIDATION.md) |
| How should an agent continue safely? | [AGENTS.md](AGENTS.md) |
| Where did this source come from? | [Provenance](docs/PROVENANCE.md) |

## Included

- A shared Rust core for selected-root indexing, matching/ranking, filters, history, favorites, commands, facets and portable configuration.
- RocksDB-backed durable catalog, generation-bound rebuildable cache, cancellation and recovery tests.
- A local authenticated resident agent, plus C ABI **v9** for native-shell clients.
- Toolchain/dependency locks, Rust tests, Python acceptance/build helpers and Mac dialog/Accessibility fixtures.
- Independently written **optional integration snapshots**: Windows Python/PySide6 client/UI and dialog/hotkey adapters; Mac automation bridge. See [integration boundaries](integrations/README.md). These do not make a standalone Mac GUI.

Python is used for tooling and the optional Windows interface. Core indexing/search/ranking are Rust. The intended Swift/AppKit production Mac shell was not completed in this source tree. It is not a full source conversion of the proprietary Windows product.

## Developer quick start

Use the toolchain pinned in rust-toolchain.toml, a C++ toolchain and libclang/LLVM (RocksDB native dependencies). From the repository root:

```sh
cargo build --locked --release --workspace
cargo test --locked --workspace
```

Read [BUILDING.md](docs/BUILDING.md) for Windows prerequisites, portable settings and Mac acceptance. Do not run installers just to inspect source; they can register a resident service. No personal profile or prebuilt executable is included.

The related [OpusMac archive](https://github.com/sp1ffyparsley/opusmac-archive) contains an optional client for this independently implemented protocol. Neither project is a supported replacement for the commercial products.

## Limits

Physical Mac startup, real Finder/Open-Save behavior, FSEvents recovery, permissions, native UI, signing/notarization and commercial Listary performance parity remain unverified. Historical synthetic Windows benchmarks are not a head-to-head or end-to-end Mac result. No test count or successful cross-compile removes those limits.

No commercial Listary binaries, branding assets, recovered proprietary source, activation material, user indexes, tokens, private prompts, original Git history or personal screenshots are included. Dependencies retain their own licenses; see THIRD_PARTY_NOTICES.md and SECURITY.md.
