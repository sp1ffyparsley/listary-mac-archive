# Status at retirement

This is a useful shared engine, not a completed Listary-for-Mac product and not an officially supported clone.

| Component | Status |
| --- | --- |
| Rust/RocksDB catalog, cache, query and settings | Implemented with retained Windows test/benchmark evidence |
| Resident agent and C ABI v9 | Implemented; local authentication and lifecycle boundaries present |
| Windows Python/PySide6 interface | Optional snapshot included; depends on host workspace modules not bundled here |
| Native production Swift/AppKit Mac UI | Not implemented in the inspected archive |
| Mac dialog/window/AX fixtures | Present; fixtures are not an app |
| Finder/Open-Save Quick Switch, FSEvents, permissions | Contract/helpers exist; real-Mac integration and acceptance incomplete |
| Signing, notarization and long-run Mac stability | Not verified |
| Equal/better speed than commercial Listary | Not established by a controlled comparison |

Historical synthetic Windows evidence included a million-item bounded incremental-query p95 around 22 ms and one later fresh index reopen around two seconds with roughly 711 MiB peak process working set. Exhaustive scans were a different, slower workload. These are prior local receipts, not freshly reproduced public performance promises.

The optional Mac integration configuration had stale ABI metadata (v8 / 41 symbols / portable v5). In the public copy it is aligned with the actual v9 / 42-symbol / portable v6 source and requires an explicit root through LISTARY_MAC_PORT_ROOT. This does not prove native Mac behavior.

For a new fork: first reproduce build/tests in isolated state, inspect the native API contract and profile realistic workloads. Then build and verify the actual Mac shell on physical Apple Silicon, including real result opening and Open/Save post-navigation readback. Preserve safety and platform evidence instead of endlessly expanding backend feature lists. Keep the original commercial installation separate.
