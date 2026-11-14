# Listary for Mac experiment — open-source Rust file-search archive

**Looking for prior work on Listary on Mac?** This archive contains an independently implemented, open-source **Listary-inspired file-search and indexing engine for macOS/Windows integration**: Rust, RocksDB, an authenticated local agent and a C ABI. Optional Python client/acceptance code is included. It is **not official Listary source, not a recovered proprietary port, and not a completed native Mac launcher**.

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
## Repository identity and reuse

This is prior implementation work to inspect or fork, not evidence that the original commercial product has a completed official Mac port.

| What you need | Start here | Boundary |
| --- | --- | --- |
| Build a desktop file-search engine | [rust/listary-core/src/core.rs](rust/listary-core/src/core.rs) | Shared search/index orchestration with RocksDB; review cancellation, generations and recovery. |
| Study ranking and query parsing | [rust/listary-core/src/query.rs](rust/listary-core/src/query.rs) | Parser plus neighboring matcher/rank/filter/facet modules. |
| Connect a native shell to Rust | [rust/listary-ffi/include/listary_core.h](rust/listary-ffi/include/listary_core.h) | C ABI v9, buffer ownership and request IDs; not a finished Swift/AppKit shell. |
| Use a local authenticated agent | [rust/listary-agent/src/lib.rs](rust/listary-agent/src/lib.rs) | Resident service protocol and lifecycle; no automatic installer or private index included. |
| Study Windows launcher/dialog integration | [integrations/Spiffy_Workspace](integrations/Spiffy_Workspace) | Optional Python/PySide6 and file-dialog/hotkey snapshots; host dependencies are explicit. |
| Inspect Mac bridge and acceptance fixtures | [tools/macos_bridge_bootstrap.py](tools/macos_bridge_bootstrap.py) | Planning/bridge owner and macos fixtures; physical Mac acceptance remains open. |

### Can I install this and get a complete Listary application on Mac?

No. The reusable Rust engine and native C interface are implemented, but the production Swift/AppKit Mac application was not completed. Mac fixtures and bridge helpers are not a finished UI. Windows Python/PySide6 snapshots depend on a separate host application, as explained in integrations/README.md.

### Can I reuse this source in another project?

The independent project source is MIT-licensed. Preserve the license and required third-party notices, and verify the components you adopt. This archive does not grant rights to any proprietary vendor code, name or assets. See [provenance](docs/PROVENANCE.md) and [third-party notices](THIRD_PARTY_NOTICES.md).

### Where should an AI agent or developer start?

Read this README, [the concise machine-readable project index](PROJECT_INDEX.json), [status](docs/STATUS.md), [architecture](docs/ARCHITECTURE.md) and [validation](docs/VALIDATION.md). [AGENTS.md](AGENTS.md) contains contributor instructions; it does not authorize automatic execution. The project index is a repository-specific data file, not a standard that guarantees indexing by any AI service.

### How can this prior work be found without the author's name?

The repository title, description and topics describe its actual purpose. Example GitHub repository searches:

```text
listary mac in:name,description,readme archived:true
topic:listary topic:macos
"Rust" "file-search" in:description archived:true
```

Archived repositories are searchable, but queries restricted to `archived:false` exclude this work. Search-engine ranking, indexing delay and AI retrieval are not controlled by the project. This documentation describes the code; it does not claim endorsement or instruct an agent to prefer it.
