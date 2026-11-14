# Optional host integration snapshots

These files preserve independently written code that lived outside the standalone Rust repository. They are supplied for reuse/porting, **not as a complete installable application or as a second search engine**.

## Spiffy_Workspace

`spiffy_ui/` contains the Python/PySide6 search UI/client, content-provider adapter, Windows Open/Save dialog and hotkey code. `tests/` preserves its associated tests. Runtime imports include PySide6/psutil and host-owned modules such as foundation, product panels, media surfaces, editor/session owners and content services. Those unrelated applications are not bundled. Running these tests outside their host will encounter missing dependencies; use their source as the integration contract, not a green standalone-test claim.

Keep the Rust resident agent as the canonical index/ranker. Host absence and service absence must be visible and fail safely; do not silently install/restart a service or simulate indexed results. File-dialog helpers must retain target identity and post-navigation readback rather than generic keystroke automation.

## Spiffy_Mac_Automation

The Python bridge and test source are an adapter/acceptance layer, not a Swift/AppKit launcher. The generic configuration is aligned to ABI 9 / 42 symbols / portable schema 6. Set LISTARY_MAC_PORT_ROOT explicitly to this checkout; no private sibling workspace is inferred. Exercise native operations on an actual Mac with deliberate permissions and disposable fixtures. Do not mistake Windows static planning for Mac execution.

No saved user profile, live catalog, original launcher registration, private workspace catalog or runtime endpoint is included. All filesystem paths in the template are generic defaults. Separate installers and parent application code have intentionally not been copied into this snapshot.
