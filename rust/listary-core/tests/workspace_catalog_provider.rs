use listary_core::{
    ActivationSource, CancellationToken, Core, CoreConfig, CoreError, FacetCountsRequest,
    FileSearchRequest, ItemKind, RootKind, SearchRequest, WORKSPACE_PROVIDER_HINT_PREFIX,
    WorkspaceCatalogPlatform, export_portable_config, load_workspace_catalog,
};
use serde_json::json;
use std::{fs, path::Path};
use tempfile::TempDir;

fn open_core(state: &Path) -> Core {
    Core::open(CoreConfig::new(state.to_path_buf())).expect("core opens")
}

fn write_catalog(
    workspace: &Path,
    target_id: &str,
    label: &str,
    aliases: &[&str],
    launcher_name: &str,
) {
    fs::write(workspace.join(launcher_name), b"@echo off\n").unwrap();
    let aliases = aliases.iter().map(|value| json!(value)).collect::<Vec<_>>();
    let body = json!({
        "schema": "ss_scripts_workspace_catalog_v1",
        "version": 1,
        "workspace_id": "ss-scripts",
        "description": "fixture with real-catalog style extra fields",
        "policy": {"integration": "canonical"},
        "systems": [{
            "id": "spiffy_workspace",
            "label": "Spiffy Workspace",
            "platforms": ["windows", "macos"],
            "root": "Spiffy Workspace",
            "launch_targets": [{
                "id": target_id,
                "label": label,
                "aliases": aliases,
                "platform_paths": {
                    "windows": launcher_name,
                    "macos": launcher_name
                }
            }]
        }],
        "independent_repositories": [],
        "relations": [],
        "external_references": []
    });
    fs::write(
        workspace.join("workspace_catalog.json"),
        serde_json::to_vec_pretty(&body).unwrap(),
    )
    .unwrap();
}

fn write_empty_catalog(workspace: &Path) {
    let body = json!({
        "schema": "ss_scripts_workspace_catalog_v1",
        "version": 1,
        "workspace_id": "ss-scripts",
        "systems": [{
            "id": "spiffy_workspace",
            "platforms": ["windows", "macos"],
            "launch_targets": []
        }]
    });
    fs::write(
        workspace.join("workspace_catalog.json"),
        serde_json::to_vec_pretty(&body).unwrap(),
    )
    .unwrap();
}

fn load_fixture(workspace: &Path) -> listary_core::WorkspaceCatalogSnapshot {
    load_workspace_catalog(
        &workspace.join("workspace_catalog.json"),
        WorkspaceCatalogPlatform::Windows,
    )
    .expect("workspace catalog loads")
}

fn search(core: &Core, query: &str) -> listary_core::SearchResponse {
    core.search(
        &SearchRequest {
            query: query.to_string(),
            limit: 50,
            ..SearchRequest::default()
        },
        &CancellationToken::new(),
    )
    .expect("search succeeds")
}

#[test]
fn provider_alias_identity_history_and_reopen_use_one_core_owner() {
    let state = TempDir::new().unwrap();
    let workspace = TempDir::new().unwrap();
    write_catalog(
        workspace.path(),
        "spiffy_visual_clipboard",
        "Spiffy Clipboard",
        &["Visual Clipboard", "Photo Editor"],
        "Launch Spiffy Photo Editor.bat",
    );

    let snapshot = load_fixture(workspace.path());
    let provider_root = snapshot.root_id;
    let core = open_core(state.path());
    let receipt = core
        .replace_workspace_catalog_snapshot(snapshot, &CancellationToken::new())
        .unwrap();
    assert_eq!(receipt.item_count, 1);
    assert_eq!(receipt.removed_count, 0);
    assert!(
        core.list_roots()
            .iter()
            .all(|root| root.id != provider_root)
    );

    let by_alias = search(&core, "Photo Editor");
    assert_eq!(by_alias.results.len(), 1);
    assert_eq!(by_alias.results[0].item.kind, ItemKind::App);
    assert!(
        by_alias.results[0]
            .item
            .preview_hint
            .as_deref()
            .unwrap()
            .starts_with(WORKSPACE_PROVIDER_HINT_PREFIX)
    );
    let original_id = by_alias.results[0].item.id;
    core.record_activation_with_source(original_id, 10_000, ActivationSource::Launcher)
        .unwrap();

    write_catalog(
        workspace.path(),
        "spiffy_visual_clipboard",
        "Spiffy Clipboard",
        &["Visual Clipboard", "Spiffy Photo"],
        "Launch Spiffy Photo Editor.bat",
    );
    core.replace_workspace_catalog_snapshot(
        load_fixture(workspace.path()),
        &CancellationToken::new(),
    )
    .unwrap();
    let renamed_alias = search(&core, "Spiffy Photo");
    assert_eq!(renamed_alias.results.len(), 1);
    assert_eq!(renamed_alias.results[0].item.id, original_id);
    drop(core);

    let reopened = open_core(state.path());
    assert!(search(&reopened, "Spiffy Photo").results.is_empty());
    reopened
        .replace_workspace_catalog_snapshot(
            load_fixture(workspace.path()),
            &CancellationToken::new(),
        )
        .unwrap();
    assert_eq!(
        search(&reopened, "Spiffy Photo").results[0].item.id,
        original_id
    );
    assert!(
        reopened
            .recommendations(10_001, 10)
            .iter()
            .any(|recommendation| recommendation.item.id == original_id)
    );
}

#[test]
fn unchanged_provider_refresh_preserves_catalog_generation_and_file_search_session() {
    let state = TempDir::new().unwrap();
    let workspace = TempDir::new().unwrap();
    write_catalog(
        workspace.path(),
        "spiffy_visual_clipboard",
        "Spiffy Clipboard",
        &["Visual Clipboard", "Photo Editor"],
        "Launch Spiffy Photo Editor.bat",
    );
    let core = open_core(state.path());
    let first = core
        .replace_workspace_catalog_snapshot(
            load_fixture(workspace.path()),
            &CancellationToken::new(),
        )
        .unwrap();
    let second = core
        .replace_workspace_catalog_snapshot(
            load_fixture(workspace.path()),
            &CancellationToken::new(),
        )
        .unwrap();
    assert_eq!(second.catalog_generation, first.catalog_generation);
    assert_eq!(second.item_count, first.item_count);
    assert_eq!(second.removed_count, 0);

    let page = core
        .file_search(
            &FileSearchRequest {
                query: "Photo Editor".into(),
                expected_catalog_generation: Some(first.catalog_generation),
                ..FileSearchRequest::default()
            },
            &CancellationToken::new(),
        )
        .unwrap();
    assert_eq!(page.catalog_generation, first.catalog_generation);
    assert_eq!(page.total_matches, 1);
}

#[test]
fn provider_removal_scrubs_history_and_portable_omissions_then_recovers() {
    let state = TempDir::new().unwrap();
    let workspace = TempDir::new().unwrap();
    write_catalog(
        workspace.path(),
        "spiffy_storyboard",
        "Spiffy Storyboard",
        &["Storyboard"],
        "Launch Spiffy Storyboard.bat",
    );
    let core = open_core(state.path());
    core.replace_workspace_catalog_snapshot(
        load_fixture(workspace.path()),
        &CancellationToken::new(),
    )
    .unwrap();
    let item_id = search(&core, "Storyboard").results[0].item.id;
    core.record_activation(item_id, 20_000).unwrap();
    core.hide_recommendation(item_id).unwrap();

    write_empty_catalog(workspace.path());
    let removal = core
        .replace_workspace_catalog_snapshot(
            load_fixture(workspace.path()),
            &CancellationToken::new(),
        )
        .unwrap();
    assert_eq!(removal.item_count, 0);
    assert_eq!(removal.removed_count, 1);
    assert!(search(&core, "Storyboard").results.is_empty());
    assert!(core.get_item(item_id).is_none());
    drop(core);

    let (portable, receipt) = export_portable_config(state.path()).unwrap();
    assert!(portable.roots.is_empty());
    assert!(portable.history.entries.is_empty());
    assert!(portable.history.hidden_items.is_empty());
    assert_eq!(
        receipt.omissions.history_entries_without_portable_identity,
        0
    );
    assert_eq!(receipt.omissions.hidden_items_without_portable_identity, 0);

    write_catalog(
        workspace.path(),
        "spiffy_storyboard",
        "Spiffy Storyboard",
        &["Storyboard"],
        "Launch Spiffy Storyboard.bat",
    );
    let reopened = open_core(state.path());
    reopened
        .replace_workspace_catalog_snapshot(
            load_fixture(workspace.path()),
            &CancellationToken::new(),
        )
        .unwrap();
    assert_eq!(search(&reopened, "Storyboard").results.len(), 1);
}

#[test]
fn cancelled_provider_refresh_commits_nothing() {
    let state = TempDir::new().unwrap();
    let workspace = TempDir::new().unwrap();
    write_catalog(
        workspace.path(),
        "spiffy_video_editor",
        "Spiffy Video Editor",
        &["Video Editor"],
        "Launch Spiffy Workspace.bat",
    );
    let core = open_core(state.path());
    let token = CancellationToken::new();
    token.cancel();
    let error = core
        .replace_workspace_catalog_snapshot(load_fixture(workspace.path()), &token)
        .unwrap_err();
    assert!(matches!(error, CoreError::Cancelled));
    assert!(search(&core, "Video Editor").results.is_empty());
    assert_eq!(core.diagnostics().unwrap().item_count, 0);
}

#[test]
fn provider_semantically_dedupes_raw_launcher_across_search_file_search_and_facets() {
    let state = TempDir::new().unwrap();
    let workspace = TempDir::new().unwrap();
    write_catalog(
        workspace.path(),
        "spiffy_video_editor",
        "Spiffy Video Editor",
        &["Launch Spiffy Workspace"],
        "Launch Spiffy Workspace.bat",
    );
    let core = open_core(state.path());
    let root = core
        .add_root(workspace.path().to_path_buf(), RootKind::Local)
        .unwrap();
    core.reconcile_root(root.id, &CancellationToken::new())
        .unwrap();
    let before = search(&core, "Launch Spiffy Workspace");
    assert_eq!(before.results.len(), 1);
    assert_eq!(before.results[0].item.kind, ItemKind::File);

    core.replace_workspace_catalog_snapshot(
        load_fixture(workspace.path()),
        &CancellationToken::new(),
    )
    .unwrap();
    let after = search(&core, "Launch Spiffy Workspace");
    assert_eq!(after.results.len(), 1);
    assert_eq!(after.results[0].item.kind, ItemKind::App);
    assert!(
        after.results[0]
            .item
            .preview_hint
            .as_deref()
            .unwrap()
            .starts_with(WORKSPACE_PROVIDER_HINT_PREFIX)
    );

    let file_search = core
        .file_search(
            &FileSearchRequest {
                query: "Launch Spiffy Workspace".into(),
                ..FileSearchRequest::default()
            },
            &CancellationToken::new(),
        )
        .unwrap();
    assert_eq!(file_search.total_matches, 1);
    assert_eq!(file_search.results[0].item.kind, ItemKind::App);

    let facets = core
        .facet_counts(
            &FacetCountsRequest {
                query: "Launch Spiffy Workspace".into(),
                ..FacetCountsRequest::default()
            },
            &CancellationToken::new(),
        )
        .unwrap();
    assert_eq!(facets.matched_total, 1);
}

#[test]
fn malformed_reload_does_not_replace_last_valid_provider_snapshot() {
    let state = TempDir::new().unwrap();
    let workspace = TempDir::new().unwrap();
    write_catalog(
        workspace.path(),
        "spiffy_video_editor",
        "Spiffy Video Editor",
        &["Video Editor"],
        "Launch Spiffy Workspace.bat",
    );
    let core = open_core(state.path());
    core.replace_workspace_catalog_snapshot(
        load_fixture(workspace.path()),
        &CancellationToken::new(),
    )
    .unwrap();
    let prior_id = search(&core, "Video Editor").results[0].item.id;

    fs::write(
        workspace.path().join("workspace_catalog.json"),
        b"{not-json",
    )
    .unwrap();
    assert!(
        load_workspace_catalog(
            &workspace.path().join("workspace_catalog.json"),
            WorkspaceCatalogPlatform::Windows,
        )
        .is_err()
    );
    let still_live = search(&core, "Video Editor");
    assert_eq!(still_live.results.len(), 1);
    assert_eq!(still_live.results[0].item.id, prior_id);
}
