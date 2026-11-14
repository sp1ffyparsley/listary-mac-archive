# Architecture and ownership

```text
optional UI / native Mac shell
       |                         |
       | local agent protocol    | C ABI v9
       v                         v
 rust/listary-agent       rust/listary-ffi
       |                         |
       +--------> rust/listary-core <--------+
                       |
           RocksDB catalog + derived cache
```

| Path | Canonical responsibility |
| --- | --- |
| `rust/listary-core/src/store.rs` | Durable catalog and transitions |
| `indexer.rs`, `core.rs` | Crawling, dirty-path reconciliation and orchestration |
| `query.rs`, `matcher.rs`, `rank.rs`, `filters.rs`, `facets.rs` | Parsing, candidate evaluation, ordering, filters/counts |
| `cache.rs` | Rebuildable generation/identity-bound search snapshot; never the durable authority |
| `history.rs`, `favorites.rs`, `commands_actions.rs`, `priority_rules.rs` | User-owned settings/history and pure resolution |
| `portable.rs` | Versioned portable configuration with explicit cross-platform path mappings |
| `workspace_catalog.rs` | Optional validated launcher catalog ingestion, not a second index |
| `rust/listary-agent` | Resident protocol/watcher lifecycle and authenticated local requests |
| `rust/listary-ffi/include/listary_core.h` | API v9, 42 exported symbols and native ownership boundary |
| `tools` | Validation, handoff/export, acceptance and explicit installer helpers |
| `macos` | Test fixtures and AX helpers, not a production launcher |
| `integrations` | Optional host-specific snapshots; see their dependency warnings |

Do not introduce another database/ranker for a UI port. Schedule synchronous search/FFI work off the UI thread; respect request IDs, cancellation, generation fencing and buffer ownership. Review the public header and actual Rust implementation instead of assuming any proprietary wire format.

Portable configuration is intended for user-owned roots/settings, not copying RocksDB or generated endpoint/token files. Use explicit root-path mapping between OSes. Keep imports conflict-safe and recoverable. Native application/process execution belongs to platform clients rather than silently executed user command templates inside the search core.

Authentication endpoint descriptors are runtime secrets and must never be committed. Optional HTTP mode must retain its read-only/authentication constraints. Test disconnect, cancellation and startup/recovery with disposable state. Scanning a whole disk or enabling browser/content providers requires explicit consent from the operator.
