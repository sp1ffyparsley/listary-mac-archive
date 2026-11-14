use std::path::PathBuf;

use listary_core::{
    ActivationSource, CancellationToken, Core, CoreConfig, DiscoveredItem, ItemKind, RootKind,
    SearchRequest,
};
use tempfile::TempDir;

fn item_id(core: &Core, query: &str) -> u32 {
    core.search(
        &SearchRequest {
            query: query.into(),
            limit: 5,
            ..SearchRequest::default()
        },
        &CancellationToken::new(),
    )
    .unwrap()
    .results[0]
        .item
        .id
}

#[test]
fn zero_query_recommendations_filter_before_limit_for_quick_switch_scope() {
    let state = TempDir::new().unwrap();
    let core = Core::open(CoreConfig::new(state.path().to_path_buf())).unwrap();
    let local = core
        .add_root(PathBuf::from("X:/quick-switch"), RootKind::Local)
        .unwrap();
    let other = core
        .add_root(PathBuf::from("Y:/other-root"), RootKind::Local)
        .unwrap();
    let offline = core
        .add_root(PathBuf::from("Z:/offline-root"), RootKind::Network)
        .unwrap();

    core.replace_root_snapshot(
        local.id,
        vec![
            DiscoveredItem::new(
                PathBuf::from("X:/quick-switch/MostRecentFile.mov"),
                ItemKind::File,
                1,
                100,
            ),
            DiscoveredItem::new(
                PathBuf::from("X:/quick-switch/FolderAlpha"),
                ItemKind::Folder,
                0,
                100,
            ),
            DiscoveredItem::new(
                PathBuf::from("X:/quick-switch/FolderBeta"),
                ItemKind::Folder,
                0,
                100,
            ),
        ],
    )
    .unwrap();
    core.replace_root_snapshot(
        other.id,
        vec![DiscoveredItem::new(
            PathBuf::from("Y:/other-root/OtherFolder"),
            ItemKind::Folder,
            0,
            100,
        )],
    )
    .unwrap();
    core.replace_root_snapshot(
        offline.id,
        vec![DiscoveredItem::new(
            PathBuf::from("Z:/offline-root/OfflineFolder"),
            ItemKind::Folder,
            0,
            100,
        )],
    )
    .unwrap();

    let file_id = item_id(&core, "MostRecentFile");
    let alpha_id = item_id(&core, "FolderAlpha");
    let beta_id = item_id(&core, "FolderBeta");
    let other_id = item_id(&core, "OtherFolder");
    let offline_id = item_id(&core, "OfflineFolder");

    for offset in 0..8 {
        core.record_activation_with_source(file_id, 10_000 + offset, ActivationSource::QuickSwitch)
            .unwrap();
    }
    for (id, at) in [
        (other_id, 10_020),
        (offline_id, 10_019),
        (alpha_id, 10_018),
        (beta_id, 10_017),
    ] {
        core.record_activation_with_source(id, at, ActivationSource::QuickSwitch)
            .unwrap();
    }
    core.set_root_unavailable(offline.id, "offline fixture".into())
        .unwrap();

    let response = core
        .search(
            &SearchRequest {
                query: String::new(),
                limit: 2,
                include_unavailable: false,
                root_ids: vec![local.id],
                kinds: vec![ItemKind::Folder],
                now_unix_ms: 10_100,
                ..SearchRequest::default()
            },
            &CancellationToken::new(),
        )
        .unwrap();

    let names: Vec<_> = response
        .results
        .iter()
        .map(|result| result.item.name.as_str())
        .collect();
    assert_eq!(names, vec!["FolderAlpha", "FolderBeta"]);
    assert!(
        response
            .results
            .iter()
            .all(|result| result.item.kind == ItemKind::Folder)
    );
    assert!(
        response
            .results
            .iter()
            .all(|result| result.item.root_id == local.id && result.item.available)
    );
}

#[test]
fn zero_query_recommendations_keep_history_hide_and_exclusion_rules() {
    let state = TempDir::new().unwrap();
    let core = Core::open(CoreConfig::new(state.path().to_path_buf())).unwrap();
    let root = core
        .add_root(PathBuf::from("X:/quick-switch-history"), RootKind::Local)
        .unwrap();
    core.replace_root_snapshot(
        root.id,
        vec![
            DiscoveredItem::new(
                PathBuf::from("X:/quick-switch-history/HiddenFolder"),
                ItemKind::Folder,
                0,
                100,
            ),
            DiscoveredItem::new(
                PathBuf::from("X:/quick-switch-history/excluded/ExcludedFolder"),
                ItemKind::Folder,
                0,
                100,
            ),
            DiscoveredItem::new(
                PathBuf::from("X:/quick-switch-history/VisibleFolder"),
                ItemKind::Folder,
                0,
                100,
            ),
        ],
    )
    .unwrap();

    let hidden = item_id(&core, "HiddenFolder");
    let excluded = item_id(&core, "ExcludedFolder");
    let visible = item_id(&core, "VisibleFolder");
    for (id, at) in [(hidden, 20_003), (excluded, 20_002), (visible, 20_001)] {
        core.record_activation_with_source(id, at, ActivationSource::QuickSwitch)
            .unwrap();
    }
    core.hide_recommendation(hidden).unwrap();
    core.exclude_recommendation_path("X:/quick-switch-history/excluded".into())
        .unwrap();

    let response = core
        .search(
            &SearchRequest {
                query: String::new(),
                limit: 10,
                root_ids: vec![root.id],
                kinds: vec![ItemKind::Folder],
                now_unix_ms: 20_100,
                ..SearchRequest::default()
            },
            &CancellationToken::new(),
        )
        .unwrap();

    assert_eq!(response.results.len(), 1);
    assert_eq!(response.results[0].item.id, visible);
    assert_eq!(response.results[0].item.name, "VisibleFolder");
}

#[test]
fn zero_query_pins_lead_history_without_duplicates_and_keep_scope_rules() {
    let state = TempDir::new().unwrap();
    let core = Core::open(CoreConfig::new(state.path().to_path_buf())).unwrap();
    let root = core
        .add_root(PathBuf::from("X:/pinned-recommendations"), RootKind::Local)
        .unwrap();
    core.replace_root_snapshot(
        root.id,
        vec![
            DiscoveredItem::new(
                PathBuf::from("X:/pinned-recommendations/PinnedFile.txt"),
                ItemKind::File,
                1,
                100,
            ),
            DiscoveredItem::new(
                PathBuf::from("X:/pinned-recommendations/PinnedFolder"),
                ItemKind::Folder,
                0,
                100,
            ),
            DiscoveredItem::new(
                PathBuf::from("X:/pinned-recommendations/HistoryFolder"),
                ItemKind::Folder,
                0,
                100,
            ),
            DiscoveredItem::new(
                PathBuf::from("X:/pinned-recommendations/excluded/ExcludedPin"),
                ItemKind::Folder,
                0,
                100,
            ),
        ],
    )
    .unwrap();

    let pinned_file = item_id(&core, "PinnedFile");
    let pinned_folder = item_id(&core, "PinnedFolder");
    let history_folder = item_id(&core, "HistoryFolder");
    let excluded_pin = item_id(&core, "ExcludedPin");
    assert!(core.toggle_pin(pinned_file).unwrap());
    assert!(core.toggle_pin(pinned_folder).unwrap());
    assert!(core.toggle_pin(excluded_pin).unwrap());
    core.hide_recommendation(pinned_file).unwrap();
    core.exclude_recommendation_path("X:/pinned-recommendations/excluded".into())
        .unwrap();
    core.record_activation_with_source(pinned_folder, 30_020, ActivationSource::QuickSwitch)
        .unwrap();
    core.record_activation_with_source(history_folder, 30_019, ActivationSource::QuickSwitch)
        .unwrap();

    let response = core
        .search(
            &SearchRequest {
                query: String::new(),
                limit: 3,
                root_ids: vec![root.id],
                now_unix_ms: 30_100,
                ..SearchRequest::default()
            },
            &CancellationToken::new(),
        )
        .unwrap();
    let ids: Vec<_> = response
        .results
        .iter()
        .map(|result| result.item.id)
        .collect();
    assert_eq!(ids, vec![pinned_file, pinned_folder, history_folder]);
    assert!(!ids.contains(&excluded_pin));

    let folders = core
        .search(
            &SearchRequest {
                query: String::new(),
                limit: 2,
                root_ids: vec![root.id],
                kinds: vec![ItemKind::Folder],
                now_unix_ms: 30_100,
                ..SearchRequest::default()
            },
            &CancellationToken::new(),
        )
        .unwrap();
    let folder_ids: Vec<_> = folders
        .results
        .iter()
        .map(|result| result.item.id)
        .collect();
    assert_eq!(folder_ids, vec![pinned_folder, history_folder]);

    assert!(!core.toggle_pin(pinned_file).unwrap());
    let after_unpin = core.recommendations(30_100, 10);
    assert!(!after_unpin.iter().any(|row| row.item.id == pinned_file));
}
