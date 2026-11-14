# Publication validation

Checked on Windows on November 13, 2026. This is a source-publication check, not certification of a native Mac app or commercial Listary parity.

| Check | Result |
| --- | --- |
| Cargo workspace metadata, locked/offline, no dependency build | Passed; three workspace crates recognized |
| Rustfmt under pinned Rust 1.97.1 | Passed after formatting the public copy |
| Python acceptance/handoff/bootstrap helper tests | 117 passed with pytest 9.1.1 |
| Mac bridge `--plan --json` | Passed as a Windows static plan, not execution on Mac |
| Syntax parsing of included Python source | Passed |
| Gitleaks 8.30.1 source scan | No findings after fixture sanitization; no broad allowlist added |
| Full Rust/RocksDB rebuild and native Rust runtime suites | Not rerun during this publication |
| Standalone optional Windows UI integration tests | Not run outside their host application |
| Physical Apple Silicon execution and native UI | Not performed |

## Initial failures and corrections

The first Python helper run had 114 passes and three failures because a standalone copied source folder lacked the Git metadata required by the source-provenance helper. A fresh, sanitized public Git repository was initialized; the helpers were rerun and all 117 passed. The original private Git history was not imported. Initial Rustfmt differences were corrected in the public source only; the subsequent formatting check passed.

The expanded integration review found personal Windows home paths in synthetic tests and three distinct hard-coded fixture token strings that triggered nine generic-secret findings. Those strings were replaced with deterministic non-secret test values while retaining original lengths and repeated-value relationships; home paths were replaced with a generic example account. The original private archive was not changed. These integration tests are preserved as host-dependent source, not presented as a fully run standalone application suite.

Historical Windows performance/test receipts are not fresh proof of the public checkout's entire native engine. In particular, no controlled commercial-product performance comparison or real Mac runtime was performed. Use BUILDING.md and the native header/tests to reproduce missing gates before relying on the software.

Raw logs and runtime receipts remain private. The final publication includes a source manifest and fresh public Git history, with no user database, live endpoint token, private chat or original reflog. Scanner success is not an exhaustive security audit.
