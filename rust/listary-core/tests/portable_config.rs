use listary_core::{
    ActivationSource, CancellationToken, Core, CoreConfig, CustomAction, CustomCommand,
    FavoriteDynamicGroupKind, FavoriteMenu, FavoriteMenuItem, FavoritePin, FilterPreset,
    FilterTarget, PortableConfig, PortableHistory, PortableHistoryEntry, PortableItemIdentity,
    PortablePathMapping, PortablePathStyle, PortableRestoreOptions, PortableRoot, Priority,
    PriorityRule, PriorityRuleMatch, RefreshPolicy, RootKind, WebSearchProvider,
    WebSuggestionProvider, export_portable_config, read_portable_config, restore_portable_config,
    validate_portable_config, write_portable_config,
};
use serde_json::{Value, json};
use std::{collections::HashSet, fs, path::Path};
use tempfile::TempDir;

fn open_core(path: &Path) -> Core {
    Core::open(CoreConfig::new(path.to_path_buf())).expect("core opens")
}

fn write(path: impl AsRef<Path>, bytes: &[u8]) {
    let path = path.as_ref();
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent).expect("create fixture parent");
    }
    fs::write(path, bytes).expect("write fixture");
}

#[test]
fn portable_export_restore_rebuilds_clean_profile_and_translates_history_ids() {
    let sandbox = TempDir::new().unwrap();
    let source_state = sandbox.path().join("source-state");
    let destination_state = sandbox.path().join("destination-state");
    let files = sandbox.path().join("files");
    let a_root_path = files.join("a-root");
    let b_root_path = files.join("b-root");
    let missing_root_path = files.join("z-missing");
    let a_item_path = a_root_path.join("A.txt");
    let history_path = b_root_path.join("History.mov");
    let hidden_path = b_root_path.join("Hidden.mov");
    let excluded_dir = b_root_path.join("Excluded");
    let excluded_item_path = excluded_dir.join("Secret.mov");
    write(&a_item_path, b"a");
    write(&history_path, b"history");
    write(&hidden_path, b"hidden");
    write(&excluded_item_path, b"excluded");

    let source_history_id;
    let source_hidden_id;
    let source_root_ids;
    let source_favorites;
    let source_web_search;
    let source_priority_rules;
    {
        let core = open_core(&source_state);
        let mut a_root = core.add_root(a_root_path.clone(), RootKind::Local).unwrap();
        a_root.refresh_policy = RefreshPolicy::Daily {
            hour: 4,
            minute: 30,
        };
        a_root.priority = Priority::High;
        core.put_root(a_root.clone()).unwrap();

        let mut b_root = core.add_root(b_root_path.clone(), RootKind::Local).unwrap();
        b_root.refresh_policy = RefreshPolicy::Interval { seconds: 900 };
        core.put_root(b_root.clone()).unwrap();

        let mut missing_root = core
            .add_root(missing_root_path.clone(), RootKind::Network)
            .unwrap();
        missing_root.refresh_policy = RefreshPolicy::Never;
        missing_root.priority = Priority::Low;
        core.put_root(missing_root.clone()).unwrap();

        // Reconcile B before A so the fresh destination rebuild assigns different
        // catalog-local IDs after the deterministic portable root sort.
        core.reconcile_root(b_root.id, &CancellationToken::new())
            .unwrap();
        core.reconcile_root(a_root.id, &CancellationToken::new())
            .unwrap();
        assert!(
            core.reconcile_root(missing_root.id, &CancellationToken::new())
                .is_err()
        );

        source_history_id = core
            .get_item_by_path(&history_path.to_string_lossy())
            .unwrap()
            .id;
        source_hidden_id = core
            .get_item_by_path(&hidden_path.to_string_lossy())
            .unwrap()
            .id;
        let excluded_id = core
            .get_item_by_path(&excluded_item_path.to_string_lossy())
            .unwrap()
            .id;

        for offset in 0..3 {
            core.record_activation_with_source(
                source_history_id,
                1_800_000_000_000 + offset,
                ActivationSource::QuickSwitch,
            )
            .unwrap();
        }
        core.record_activation_with_source(
            source_hidden_id,
            1_800_000_000_100,
            ActivationSource::Launcher,
        )
        .unwrap();
        core.hide_recommendation(source_hidden_id).unwrap();
        core.record_activation_with_source(
            excluded_id,
            1_800_000_000_200,
            ActivationSource::FileSearch,
        )
        .unwrap();
        core.exclude_recommendation_path(excluded_dir.to_string_lossy().into_owned())
            .unwrap();

        let mut filter = FilterPreset::new("movie", "Movie sources");
        filter.extra_query = "History".into();
        filter.search_paths = vec![b_root_path.to_string_lossy().into_owned()];
        filter.target = FilterTarget::Files;
        filter.extensions = vec!["mov".into()];
        core.put_filter(filter).unwrap();
        let mut command = CustomCommand::new("scene", "Scene helper", "scene-helper");
        command.arguments = vec!["{query}".into(), "{current_folder}".into()];
        let command = core.put_command(command).unwrap();
        let mut action = CustomAction::new("Process clip", "clip-helper");
        action.arguments = vec!["{action_path}".into(), "{action_path:parent}".into()];
        core.put_action(action).unwrap();
        source_favorites = FavoriteMenu {
            items: vec![
                FavoriteMenuItem::Folder {
                    id: uuid::Uuid::new_v4(),
                    title: Some("A root".into()),
                    path: a_root_path.to_string_lossy().into_owned(),
                },
                FavoriteMenuItem::Submenu {
                    id: uuid::Uuid::new_v4(),
                    title: "Tools".into(),
                    items: vec![
                        FavoriteMenuItem::Separator {
                            id: uuid::Uuid::new_v4(),
                        },
                        FavoriteMenuItem::Command {
                            id: uuid::Uuid::new_v4(),
                            command_id: command.id,
                        },
                    ],
                },
                FavoriteMenuItem::DynamicGroup {
                    id: uuid::Uuid::new_v4(),
                    title: Some("Recent folders".into()),
                    kind: FavoriteDynamicGroupKind::RecentFolders,
                    limit: 7,
                },
            ],
            pins: vec![FavoritePin {
                id: uuid::Uuid::new_v4(),
                title: Some("Pinned A".into()),
                path: a_item_path.to_string_lossy().into_owned(),
            }],
        };
        core.put_favorites(source_favorites.clone()).unwrap();
        let mut first_web =
            WebSearchProvider::new("store", "First store", "https://first.test/?q={query}");
        first_web.hotkey = Some("Ctrl+1".into());
        first_web.suggestion_provider = WebSuggestionProvider::Google;
        let mut second_web =
            WebSearchProvider::new("store", "Second store", "https://second.test/?q={query}");
        second_web.suggestion_provider = WebSuggestionProvider::Custom {
            url_template: "https://suggest.test/?q={query}".into(),
        };
        source_web_search = vec![first_web, second_web];
        core.replace_web_search_providers(source_web_search.clone())
            .unwrap();
        source_priority_rules = vec![
            PriorityRule::path(
                b_root_path.join("Excluded").to_string_lossy(),
                true,
                Priority::Low,
            ),
            PriorityRule::regex(r"history\.mov$", Priority::High),
        ];
        core.replace_priority_rules(source_priority_rules.clone())
            .unwrap();
        core.flush().unwrap();
        source_root_ids = core
            .list_roots()
            .into_iter()
            .map(|root| root.id)
            .collect::<HashSet<_>>();
    }

    let (source_config, export_receipt) = export_portable_config(&source_state).unwrap();
    assert_eq!(export_receipt.root_count, 3);
    assert_eq!(export_receipt.filter_count, 1);
    assert_eq!(export_receipt.command_count, 1);
    assert_eq!(export_receipt.action_count, 1);
    assert_eq!(export_receipt.favorite_item_count, 6);
    assert_eq!(export_receipt.web_search_provider_count, 2);
    assert_eq!(export_receipt.priority_rule_count, 2);
    assert_eq!(source_config.favorites, source_favorites);
    assert_eq!(source_config.web_search_providers, source_web_search);
    assert_eq!(source_config.priority_rules, source_priority_rules);
    assert_eq!(export_receipt.history_entry_count, 3);
    assert_eq!(export_receipt.hidden_item_count, 1);
    assert_eq!(
        export_receipt
            .omissions
            .history_entries_without_portable_identity,
        0
    );
    assert_eq!(
        export_receipt
            .omissions
            .hidden_items_without_portable_identity,
        0
    );

    let artifact = sandbox.path().join("portable-config.json");
    write_portable_config(&artifact, &source_config).unwrap();
    let first_bytes = fs::read(&artifact).unwrap();
    let parsed = read_portable_config(&artifact).unwrap();
    assert_eq!(parsed, source_config);
    write_portable_config(&artifact, &parsed).unwrap();
    assert_eq!(fs::read(&artifact).unwrap(), first_bytes);

    let artifact_json: Value = serde_json::from_slice(&first_bytes).unwrap();
    let first_root = artifact_json["roots"][0].as_object().unwrap();
    assert_eq!(
        first_root.keys().cloned().collect::<HashSet<_>>(),
        ["path", "kind", "refresh_policy", "priority", "enabled"]
            .into_iter()
            .map(str::to_string)
            .collect()
    );
    assert!(first_root.get("id").is_none());
    assert!(first_root.get("availability").is_none());
    assert!(first_root.get("last_reconciled_unix_ms").is_none());
    assert!(first_root.get("last_error").is_none());
    let first_history_item = artifact_json["history"]["entries"][0]["item"]
        .as_object()
        .unwrap();
    assert!(first_history_item.get("id").is_none());
    assert_eq!(
        first_history_item.keys().cloned().collect::<HashSet<_>>(),
        ["root_path", "relative_path"]
            .into_iter()
            .map(str::to_string)
            .collect()
    );

    let restore_receipt = restore_portable_config(
        &destination_state,
        &parsed,
        &PortableRestoreOptions::default(),
    )
    .unwrap();
    assert!(!restore_receipt.idempotent);
    assert!(restore_receipt.rebuilt_local_catalog);
    assert!(!restore_receipt.copied_source_catalog);
    assert_eq!(restore_receipt.priority_rule_count, 2);
    assert_eq!(
        restore_receipt.unresolved_roots,
        vec![missing_root_path.to_string_lossy().into_owned()]
    );
    assert!(restore_receipt.unresolved_history_items.is_empty());
    assert!(restore_receipt.unresolved_hidden_items.is_empty());

    {
        let destination = open_core(&destination_state);
        let destination_roots = destination.list_roots();
        assert_eq!(destination_roots.len(), 3);
        assert!(
            destination_roots
                .iter()
                .all(|root| !source_root_ids.contains(&root.id))
        );
        let missing = destination_roots
            .iter()
            .find(|root| root.path == missing_root_path)
            .unwrap();
        assert_eq!(missing.refresh_policy, RefreshPolicy::Never);
        assert_eq!(missing.priority, Priority::Low);
        assert_eq!(
            missing.availability,
            listary_core::RootAvailability::Unavailable
        );

        let destination_history = destination
            .get_item_by_path(&history_path.to_string_lossy())
            .unwrap();
        assert_ne!(destination_history.id, source_history_id);
        let destination_hidden = destination
            .get_item_by_path(&hidden_path.to_string_lossy())
            .unwrap();
        assert_ne!(destination_hidden.id, source_hidden_id);

        let recommendations = destination.recommendations(1_800_000_000_300, 20);
        let restored = recommendations
            .iter()
            .find(|row| row.item.path == history_path.to_string_lossy())
            .unwrap();
        assert_eq!(restored.activation_count, 3);
        assert_eq!(restored.last_activated_unix_ms, 1_800_000_000_002);
        assert_eq!(restored.last_source, ActivationSource::QuickSwitch);
        assert!(
            recommendations
                .iter()
                .all(|row| row.item.path != hidden_path.to_string_lossy())
        );
        assert!(
            recommendations
                .iter()
                .all(|row| row.item.path != excluded_item_path.to_string_lossy())
        );

        let filters = destination.list_filters();
        assert_eq!(filters.len(), 1);
        assert_eq!(filters[0].keyword, "movie");
        assert_eq!(
            filters[0].search_paths,
            vec![b_root_path.to_string_lossy().into_owned()]
        );
        assert_eq!(destination.list_commands().len(), 1);
        assert_eq!(destination.list_commands()[0].keyword, "scene");
        assert_eq!(destination.list_actions().len(), 1);
        assert_eq!(destination.list_actions()[0].title, "Process clip");
        assert_eq!(destination.get_favorites(), source_favorites);
        assert_eq!(destination.list_web_search_providers(), source_web_search);
        assert_eq!(destination.list_priority_rules(), source_priority_rules);

        let history_json: Value =
            serde_json::from_slice(&fs::read(destination_state.join("history.json")).unwrap())
                .unwrap();
        let restored_history_id = history_json["entries"]
            .as_object()
            .unwrap()
            .iter()
            .find(|(_, value)| value["activation_count"].as_u64() == Some(3))
            .map(|(key, _)| key.parse::<u32>().unwrap())
            .unwrap();
        assert_eq!(restored_history_id, destination_history.id);
    }

    let (destination_config, destination_export_receipt) =
        export_portable_config(&destination_state).unwrap();
    assert_eq!(
        destination_export_receipt
            .omissions
            .history_entries_without_portable_identity,
        0
    );
    assert_eq!(
        destination_export_receipt
            .omissions
            .hidden_items_without_portable_identity,
        0
    );
    assert_eq!(destination_export_receipt.favorite_item_count, 6);
    assert_eq!(destination_export_receipt.web_search_provider_count, 2);
    assert_eq!(destination_config, source_config);

    let idempotent = restore_portable_config(
        &destination_state,
        &source_config,
        &PortableRestoreOptions::default(),
    )
    .unwrap();
    assert!(idempotent.idempotent);
    assert!(idempotent.round_trip_equivalent);
}

#[test]
fn portable_export_reports_stale_numeric_history_that_has_no_semantic_item() {
    let sandbox = TempDir::new().unwrap();
    let state = sandbox.path().join("state");
    let root_path = sandbox.path().join("root");
    let item_path = root_path.join("Gone.txt");
    write(&item_path, b"gone");

    {
        let core = open_core(&state);
        let root = core.add_root(root_path, RootKind::Local).unwrap();
        core.reconcile_root(root.id, &CancellationToken::new())
            .unwrap();
        let item = core.get_item_by_path(&item_path.to_string_lossy()).unwrap();
        core.record_activation_with_source(item.id, 123, ActivationSource::Launcher)
            .unwrap();
        core.hide_recommendation(item.id).unwrap();
        core.remove_root(root.id).unwrap();
    }

    let (config, receipt) = export_portable_config(&state).unwrap();
    assert!(config.roots.is_empty());
    assert!(config.history.entries.is_empty());
    assert!(config.history.hidden_items.is_empty());
    assert_eq!(
        receipt.omissions.history_entries_without_portable_identity,
        1
    );
    assert_eq!(receipt.omissions.hidden_items_without_portable_identity, 1);
}

#[test]
fn portable_restore_rejects_conflicting_destination_without_mutation() {
    let sandbox = TempDir::new().unwrap();
    let destination_state = sandbox.path().join("destination");
    let existing_root = sandbox.path().join("existing");
    let incoming_root = sandbox.path().join("incoming");
    fs::create_dir_all(&existing_root).unwrap();
    fs::create_dir_all(&incoming_root).unwrap();

    {
        let core = open_core(&destination_state);
        core.add_root(existing_root, RootKind::Local).unwrap();
    }
    let (before, _) = export_portable_config(&destination_state).unwrap();
    let incoming = PortableConfig {
        roots: vec![PortableRoot {
            path: incoming_root.to_string_lossy().into_owned(),
            kind: RootKind::Local,
            refresh_policy: RefreshPolicy::Automatic,
            priority: Priority::Normal,
            enabled: false,
        }],
        ..PortableConfig::default()
    };

    let error = restore_portable_config(
        &destination_state,
        &incoming,
        &PortableRestoreOptions::default(),
    )
    .unwrap_err();
    assert!(error.to_string().contains("conflicts with import"));
    let (after, _) = export_portable_config(&destination_state).unwrap();
    assert_eq!(after, before);
}

#[test]
fn portable_restore_treats_priority_rule_only_destination_as_non_empty() {
    let sandbox = TempDir::new().unwrap();
    let destination_state = sandbox.path().join("priority-only-destination");

    {
        let core = open_core(&destination_state);
        core.replace_priority_rules(vec![PriorityRule::regex(
            r"(?i)[\\/]private[\\/]",
            Priority::Ignored,
        )])
        .unwrap();
    }
    let (before, _) = export_portable_config(&destination_state).unwrap();
    assert_eq!(before.priority_rules.len(), 1);

    let error = restore_portable_config(
        &destination_state,
        &PortableConfig::default(),
        &PortableRestoreOptions::default(),
    )
    .unwrap_err();
    assert!(error.to_string().contains("conflicts with import"));

    let (after, _) = export_portable_config(&destination_state).unwrap();
    assert_eq!(after, before);
}

#[test]
fn portable_read_recovers_truncated_primary_from_backup_and_repairs_reopen() {
    let sandbox = TempDir::new().unwrap();
    let artifact = sandbox.path().join("portable.json");
    let backup_config = PortableConfig {
        commands: vec![CustomCommand::new(
            "backup",
            "Backup generation",
            "portable-tool",
        )],
        ..PortableConfig::default()
    };
    let newer_config = PortableConfig {
        commands: vec![CustomCommand::new(
            "newer",
            "Newer generation",
            "portable-tool",
        )],
        ..PortableConfig::default()
    };
    write_portable_config(&artifact, &backup_config).unwrap();
    write_portable_config(&artifact, &newer_config).unwrap();
    let backup_bytes = fs::read(artifact.with_extension("json.bak")).unwrap();
    let truncated = br#"{"schema":"listary_portable_config","schema_version":5,"#;
    fs::write(&artifact, truncated).unwrap();

    let recovered = read_portable_config(&artifact).unwrap();
    assert_eq!(recovered, backup_config);
    assert_eq!(
        fs::read(artifact.with_extension("json.corrupt")).unwrap(),
        truncated
    );
    assert_eq!(
        fs::read(artifact.with_extension("json.bak")).unwrap(),
        backup_bytes
    );

    let reopened = read_portable_config(&artifact).unwrap();
    assert_eq!(reopened, backup_config);
    assert_eq!(
        fs::read(artifact.with_extension("json.corrupt")).unwrap(),
        truncated
    );
}

#[test]
fn portable_read_preserves_every_corrupt_generation_with_actionable_error() {
    let sandbox = TempDir::new().unwrap();
    let artifact = sandbox.path().join("portable.json");
    let generations = [
        (artifact.clone(), b"{broken-primary".as_slice()),
        (
            artifact.with_extension("json.bak"),
            b"{broken-backup".as_slice(),
        ),
        (
            artifact.with_extension("json.tmp"),
            b"{broken-staging".as_slice(),
        ),
    ];
    for (path, bytes) in &generations {
        fs::write(path, bytes).unwrap();
    }

    let error = read_portable_config(&artifact).unwrap_err().to_string();
    assert!(error.contains("primary: corrupt"));
    assert!(error.contains("backup: corrupt"));
    assert!(error.contains("staging: corrupt"));
    assert!(error.contains("preserved for diagnosis"));
    for (path, bytes) in generations {
        assert_eq!(fs::read(path).unwrap(), bytes);
    }
    assert!(!artifact.with_extension("json.repair.tmp").exists());
}

#[test]
fn portable_read_never_downgrades_an_unsupported_future_primary() {
    let sandbox = TempDir::new().unwrap();
    let artifact = sandbox.path().join("portable.json");
    let supported = PortableConfig {
        commands: vec![CustomCommand::new(
            "supported",
            "Supported backup",
            "portable-tool",
        )],
        ..PortableConfig::default()
    };
    write_portable_config(&artifact, &supported).unwrap();
    fs::copy(&artifact, artifact.with_extension("json.bak")).unwrap();
    let future = PortableConfig {
        schema_version: listary_core::PORTABLE_CONFIG_SCHEMA_VERSION + 1,
        commands: vec![CustomCommand::new(
            "future",
            "Future primary",
            "portable-tool",
        )],
        ..PortableConfig::default()
    };
    let future_bytes = serde_json::to_vec_pretty(&future).unwrap();
    fs::write(&artifact, &future_bytes).unwrap();

    let error = read_portable_config(&artifact).unwrap_err().to_string();
    assert!(error.contains("primary generation"));
    assert!(error.contains("unsupported future schema version"));
    assert!(error.contains("refusing to downgrade"));
    assert_eq!(fs::read(&artifact).unwrap(), future_bytes);
    assert_eq!(
        read_portable_config(&artifact.with_extension("json.bak")).unwrap(),
        supported
    );
}

#[test]
fn portable_paths_reject_relative_traversal_and_mapping_escapes() {
    fn root(path: &str) -> PortableRoot {
        PortableRoot {
            path: path.into(),
            kind: RootKind::Local,
            refresh_policy: RefreshPolicy::Automatic,
            priority: Priority::Normal,
            enabled: false,
        }
    }

    for unsafe_root in [
        "relative/root",
        "../escape",
        r"C:\Portable\..\Escape",
        "/Users/example/./Root",
        r"C:\Portable\D:\Escape",
    ] {
        let config = PortableConfig {
            roots: vec![root(unsafe_root)],
            ..PortableConfig::default()
        };
        assert!(
            validate_portable_config(&config).is_err(),
            "unsafe root unexpectedly passed: {unsafe_root}"
        );
    }

    for unsafe_relative in ["../escape", "/absolute/escape", r"D:\absolute\escape"] {
        let config = PortableConfig {
            roots: vec![root(r"C:\Portable")],
            history: PortableHistory {
                entries: vec![PortableHistoryEntry {
                    item: PortableItemIdentity {
                        root_path: r"C:\Portable".into(),
                        relative_path: unsafe_relative.into(),
                    },
                    activation_count: 1,
                    last_activated_unix_ms: 1,
                    last_source: ActivationSource::FileSearch,
                }],
                ..PortableHistory::default()
            },
            ..PortableConfig::default()
        };
        assert!(
            validate_portable_config(&config).is_err(),
            "unsafe nested path unexpectedly passed: {unsafe_relative}"
        );
    }

    let sandbox = TempDir::new().unwrap();
    let config = PortableConfig {
        roots: vec![root(r"C:\Portable\Root")],
        ..PortableConfig::default()
    };
    for mapping in [
        PortablePathMapping {
            from: "Portable".into(),
            to: "/Users/safe".into(),
        },
        PortablePathMapping {
            from: r"C:\Portable".into(),
            to: "/Users/safe/../escape".into(),
        },
        PortablePathMapping {
            from: r"C:\Portable\Root\..".into(),
            to: "/Users/safe".into(),
        },
    ] {
        let destination = sandbox.path().join(uuid::Uuid::new_v4().to_string());
        assert!(
            restore_portable_config(
                &destination,
                &config,
                &PortableRestoreOptions {
                    path_mappings: vec![mapping],
                    target_path_style: PortablePathStyle::Posix,
                },
            )
            .is_err()
        );
        assert!(!destination.exists());
    }
}

#[test]
fn portable_mappings_support_nonexistent_windows_and_posix_destinations() {
    let sandbox = TempDir::new().unwrap();
    let windows_source = PortableConfig {
        roots: vec![PortableRoot {
            path: r"C:\Portable\Deleted\Root".into(),
            kind: RootKind::Local,
            refresh_policy: RefreshPolicy::Never,
            priority: Priority::Normal,
            enabled: false,
        }],
        priority_rules: vec![PriorityRule::regex(
            r"(?i)opaque[\\/]pattern",
            Priority::Low,
        )],
        ..PortableConfig::default()
    };
    let posix_state = sandbox.path().join("posix-state");
    restore_portable_config(
        &posix_state,
        &windows_source,
        &PortableRestoreOptions {
            path_mappings: vec![PortablePathMapping {
                from: r"C:\Portable".into(),
                to: "/Volumes/Definitely-Deleted".into(),
            }],
            target_path_style: PortablePathStyle::Posix,
        },
    )
    .unwrap();
    let (posix, _) = export_portable_config(&posix_state).unwrap();
    assert_eq!(
        posix.roots[0].path,
        "/Volumes/Definitely-Deleted/Deleted/Root"
    );
    match &posix.priority_rules[0].matcher {
        PriorityRuleMatch::Regex { pattern } => {
            assert_eq!(pattern, r"(?i)opaque[\\/]pattern");
        }
        other => panic!("regex rule was incorrectly treated as a path: {other:?}"),
    }

    let posix_source = PortableConfig {
        roots: vec![PortableRoot {
            path: "/Volumes/Portable/Deleted/Root".into(),
            kind: RootKind::Local,
            refresh_policy: RefreshPolicy::Never,
            priority: Priority::Normal,
            enabled: false,
        }],
        ..PortableConfig::default()
    };
    let windows_state = sandbox.path().join("windows-state");
    restore_portable_config(
        &windows_state,
        &posix_source,
        &PortableRestoreOptions {
            path_mappings: vec![PortablePathMapping {
                from: "/Volumes/Portable".into(),
                to: r"Q:\Definitely-Deleted".into(),
            }],
            target_path_style: PortablePathStyle::Windows,
        },
    )
    .unwrap();
    let (windows, _) = export_portable_config(&windows_state).unwrap();
    assert_eq!(windows.roots[0].path, r"Q:\Definitely-Deleted\Deleted\Root");
}

#[test]
fn portable_validation_and_path_mapping_fail_closed() {
    let sandbox = TempDir::new().unwrap();
    let malformed = sandbox.path().join("malformed.json");
    fs::write(&malformed, b"{not-json").unwrap();
    assert!(read_portable_config(&malformed).is_err());

    let legacy = sandbox.path().join("portable-v1.json");
    fs::write(
        &legacy,
        serde_json::to_vec(&json!({
            "schema": "listary_portable_config",
            "schema_version": 1,
            "roots": [],
            "filters": [],
            "history": {
                "entries": [],
                "hidden_items": [],
                "excluded_path_prefixes": []
            }
        }))
        .unwrap(),
    )
    .unwrap();
    let upgraded = read_portable_config(&legacy).unwrap();
    assert_eq!(
        upgraded.schema_version,
        listary_core::PORTABLE_CONFIG_SCHEMA_VERSION
    );
    assert!(upgraded.commands.is_empty());
    assert!(upgraded.actions.is_empty());
    assert!(upgraded.favorites.items.is_empty());
    assert!(upgraded.web_search_providers.is_empty());
    assert!(upgraded.priority_rules.is_empty());

    let legacy_v2 = sandbox.path().join("portable-v2.json");
    let legacy_command = CustomCommand::new("legacy", "Legacy command", "legacy-tool");
    let legacy_action = CustomAction::new("Legacy action", "legacy-action");
    let mut legacy_v2_json = serde_json::to_value(PortableConfig {
        schema_version: 2,
        commands: vec![legacy_command.clone()],
        actions: vec![legacy_action.clone()],
        ..PortableConfig::default()
    })
    .unwrap();
    legacy_v2_json.as_object_mut().unwrap().remove("favorites");
    fs::write(&legacy_v2, serde_json::to_vec(&legacy_v2_json).unwrap()).unwrap();
    let upgraded_v2 = read_portable_config(&legacy_v2).unwrap();
    assert_eq!(
        upgraded_v2.schema_version,
        listary_core::PORTABLE_CONFIG_SCHEMA_VERSION
    );
    assert_eq!(upgraded_v2.commands, vec![legacy_command]);
    assert_eq!(upgraded_v2.actions, vec![legacy_action]);
    assert!(upgraded_v2.favorites.items.is_empty());
    assert!(upgraded_v2.web_search_providers.is_empty());

    let legacy_v3 = sandbox.path().join("portable-v3.json");
    let mut legacy_v3_json = serde_json::to_value(PortableConfig {
        schema_version: 3,
        favorites: FavoriteMenu {
            items: vec![FavoriteMenuItem::Folder {
                id: uuid::Uuid::new_v4(),
                title: Some("Legacy favorite".into()),
                path: "/legacy/favorite".into(),
            }],
            pins: Vec::new(),
        },
        ..PortableConfig::default()
    })
    .unwrap();
    legacy_v3_json
        .as_object_mut()
        .unwrap()
        .remove("web_search_providers");
    fs::write(&legacy_v3, serde_json::to_vec(&legacy_v3_json).unwrap()).unwrap();
    let upgraded_v3 = read_portable_config(&legacy_v3).unwrap();
    assert_eq!(
        upgraded_v3.schema_version,
        listary_core::PORTABLE_CONFIG_SCHEMA_VERSION
    );
    assert_eq!(upgraded_v3.favorites.items.len(), 1);
    assert!(upgraded_v3.web_search_providers.is_empty());
    assert!(upgraded_v3.priority_rules.is_empty());

    let legacy_v4 = sandbox.path().join("portable-v4.json");
    let mut legacy_v4_json = serde_json::to_value(PortableConfig {
        schema_version: 4,
        ..PortableConfig::default()
    })
    .unwrap();
    legacy_v4_json
        .as_object_mut()
        .unwrap()
        .remove("priority_rules");
    fs::write(&legacy_v4, serde_json::to_vec(&legacy_v4_json).unwrap()).unwrap();
    let upgraded_v4 = read_portable_config(&legacy_v4).unwrap();
    assert_eq!(
        upgraded_v4.schema_version,
        listary_core::PORTABLE_CONFIG_SCHEMA_VERSION
    );
    assert!(upgraded_v4.priority_rules.is_empty());

    let legacy_v5 = sandbox.path().join("portable-v5.json");
    let mut legacy_v5_json = serde_json::to_value(PortableConfig {
        schema_version: 5,
        ..PortableConfig::default()
    })
    .unwrap();
    legacy_v5_json["favorites"]
        .as_object_mut()
        .unwrap()
        .remove("pins");
    fs::write(&legacy_v5, serde_json::to_vec(&legacy_v5_json).unwrap()).unwrap();
    let upgraded_v5 = read_portable_config(&legacy_v5).unwrap();
    assert_eq!(
        upgraded_v5.schema_version,
        listary_core::PORTABLE_CONFIG_SCHEMA_VERSION
    );
    assert!(upgraded_v5.favorites.pins.is_empty());

    let dangling_favorite = PortableConfig {
        favorites: FavoriteMenu {
            items: vec![FavoriteMenuItem::Command {
                id: uuid::Uuid::new_v4(),
                command_id: uuid::Uuid::new_v4(),
            }],
            pins: vec![FavoritePin {
                id: uuid::Uuid::new_v4(),
                title: Some("Portable pin".into()),
                path: r"C:\Portable\Root\Pinned.mov".into(),
            }],
        },
        ..PortableConfig::default()
    };
    assert!(
        validate_portable_config(&dangling_favorite)
            .unwrap_err()
            .to_string()
            .contains("favorites reference unknown command")
    );

    let mut invalid_web =
        WebSearchProvider::new("bad", "Bad provider", "https://example.test/?q={query}");
    invalid_web.url_template = "https://example.test/?q={raw}".into();
    let invalid_web_config = PortableConfig {
        web_search_providers: vec![invalid_web],
        ..PortableConfig::default()
    };
    assert!(validate_portable_config(&invalid_web_config).is_err());

    let future = PortableConfig {
        schema_version: listary_core::PORTABLE_CONFIG_SCHEMA_VERSION + 1,
        ..PortableConfig::default()
    };
    assert!(validate_portable_config(&future).is_err());

    let duplicate_root = PortableRoot {
        path: r"C:\Portable\Root".into(),
        kind: RootKind::Local,
        refresh_policy: RefreshPolicy::Automatic,
        priority: Priority::Normal,
        enabled: false,
    };
    let duplicates = PortableConfig {
        roots: vec![duplicate_root.clone(), duplicate_root.clone()],
        ..PortableConfig::default()
    };
    assert!(validate_portable_config(&duplicates).is_err());

    let mut filter = FilterPreset::new("portable", "Portable");
    filter.search_paths = vec![r"C:\Portable\Root".into()];
    let mut command = CustomCommand::new(
        "portablecmd",
        "Portable command",
        r"C:\Portable\Tools\command.exe",
    );
    command.working_directory = Some(r"C:\Portable\Root".into());
    command.arguments = vec!["{query}".into(), "{current_folder}".into()];
    let mut action = CustomAction::new("Portable action", r"C:\Portable\Tools\action.exe");
    action.working_directory = Some(r"C:\Portable\Root".into());
    action.arguments = vec!["{action_path}".into(), "{action_path:parent}".into()];
    let mut web_provider = WebSearchProvider::new(
        "portableweb",
        "Portable web",
        "https://example.test/?q={query}",
    );
    web_provider.icon_path = Some(r"C:\Portable\Icons\web.png".into());
    let portable = PortableConfig {
        roots: vec![duplicate_root],
        filters: vec![filter],
        commands: vec![command],
        actions: vec![action],
        favorites: FavoriteMenu {
            items: vec![FavoriteMenuItem::Folder {
                id: uuid::Uuid::new_v4(),
                title: Some("Portable favorite".into()),
                path: r"C:\Portable\Root\Favorite".into(),
            }],
            pins: vec![FavoritePin {
                id: uuid::Uuid::new_v4(),
                title: Some("Portable pin".into()),
                path: r"C:\Portable\Root\Pinned.mov".into(),
            }],
        },
        web_search_providers: vec![web_provider],
        priority_rules: vec![
            PriorityRule::path(r"C:\Portable\Root\Priority", true, Priority::High),
            PriorityRule::regex(r"/portable/root/.+\.tmp$", Priority::Low),
        ],
        history: PortableHistory {
            entries: Vec::new(),
            hidden_items: Vec::new(),
            excluded_path_prefixes: vec![r"C:\Portable\Root\Skip".into()],
        },
        ..PortableConfig::default()
    };
    let destination = sandbox.path().join("mapped-destination");
    let no_mapping = restore_portable_config(
        &destination,
        &portable,
        &PortableRestoreOptions {
            path_mappings: Vec::new(),
            target_path_style: PortablePathStyle::Posix,
        },
    )
    .unwrap_err();
    assert!(no_mapping.to_string().contains("explicit POSIX mapping"));
    assert!(!destination.exists());

    let mapped = restore_portable_config(
        &destination,
        &portable,
        &PortableRestoreOptions {
            path_mappings: vec![PortablePathMapping {
                from: r"C:\Portable".into(),
                to: "/Users/example".into(),
            }],
            target_path_style: PortablePathStyle::Posix,
        },
    )
    .unwrap();
    assert_eq!(mapped.root_count, 1);
    assert_eq!(mapped.command_count, 1);
    assert_eq!(mapped.action_count, 1);
    assert_eq!(mapped.favorite_item_count, 2);
    assert_eq!(mapped.web_search_provider_count, 1);
    assert_eq!(mapped.priority_rule_count, 2);
    let (mapped_config, _) = export_portable_config(&destination).unwrap();
    assert_eq!(mapped_config.roots[0].path, "/Users/example/Root");
    assert_eq!(
        mapped_config.filters[0].search_paths,
        vec!["/Users/example/Root".to_string()]
    );
    assert_eq!(
        mapped_config.commands[0].executable,
        "/Users/example/Tools/command.exe"
    );
    assert_eq!(
        mapped_config.commands[0].working_directory.as_deref(),
        Some("/Users/example/Root")
    );
    assert_eq!(
        mapped_config.actions[0].executable,
        "/Users/example/Tools/action.exe"
    );
    assert_eq!(
        mapped_config.actions[0].working_directory.as_deref(),
        Some("/Users/example/Root")
    );
    let FavoriteMenuItem::Folder { path, .. } = &mapped_config.favorites.items[0] else {
        panic!("mapped favorite must remain a folder item");
    };
    assert_eq!(path, "/Users/example/Root/Favorite");
    assert_eq!(
        mapped_config.favorites.pins[0].path,
        "/Users/example/Root/Pinned.mov"
    );
    assert_eq!(
        mapped_config.web_search_providers[0].icon_path.as_deref(),
        Some("/Users/example/Icons/web.png")
    );
    assert_eq!(
        mapped_config.history.excluded_path_prefixes,
        vec!["/users/example/root/skip".to_string()]
    );
    match &mapped_config.priority_rules[0].matcher {
        PriorityRuleMatch::Path { path, descendants } => {
            assert_eq!(path, "/Users/example/Root/Priority");
            assert!(*descendants);
        }
        other => panic!("expected mapped path priority rule, got {other:?}"),
    }
    match &mapped_config.priority_rules[1].matcher {
        PriorityRuleMatch::Regex { pattern } => {
            assert_eq!(pattern, r"/portable/root/.+\.tmp$");
        }
        other => panic!("expected preserved regex priority rule, got {other:?}"),
    }

    let unicode_destination = sandbox.path().join("unicode-mapped-destination");
    let unicode_root = PortableRoot {
        path: "C:/Ｋ/sub".into(),
        kind: RootKind::Local,
        refresh_policy: RefreshPolicy::Automatic,
        priority: Priority::Normal,
        enabled: false,
    };
    let unicode_config = PortableConfig {
        roots: vec![unicode_root],
        ..PortableConfig::default()
    };
    restore_portable_config(
        &unicode_destination,
        &unicode_config,
        &PortableRestoreOptions {
            path_mappings: vec![PortablePathMapping {
                from: "C:/K".into(),
                to: "/Users/unicode".into(),
            }],
            target_path_style: PortablePathStyle::Posix,
        },
    )
    .unwrap();
    let (unicode_restored, _) = export_portable_config(&unicode_destination).unwrap();
    assert_eq!(unicode_restored.roots[0].path, "/Users/unicode/sub");
}

#[test]
fn core_and_portable_root_identity_agree_on_trailing_separators() {
    let sandbox = TempDir::new().unwrap();
    let state = sandbox.path().join("state");
    let root = sandbox.path().join("root");
    fs::create_dir_all(&root).unwrap();
    let core = open_core(&state);
    core.add_root(root.clone(), RootKind::Local).unwrap();
    let duplicate = format!("{}{}", root.to_string_lossy(), std::path::MAIN_SEPARATOR);
    assert!(core.add_root(duplicate.into(), RootKind::Local).is_err());
}

#[test]
fn live_core_profile_lease_blocks_restore_before_staging() {
    let sandbox = TempDir::new().unwrap();
    let state = sandbox.path().join("state");
    let root = sandbox.path().join("restored-root");
    fs::create_dir_all(&root).unwrap();
    let config = PortableConfig {
        roots: vec![PortableRoot {
            path: root.to_string_lossy().into_owned(),
            kind: RootKind::Local,
            refresh_policy: RefreshPolicy::Automatic,
            priority: Priority::Normal,
            enabled: false,
        }],
        ..PortableConfig::default()
    };

    let live = open_core(&state);
    let error =
        restore_portable_config(&state, &config, &PortableRestoreOptions::default()).unwrap_err();
    assert!(matches!(error, listary_core::CoreError::AlreadyOpen));
    let parent = state.parent().unwrap();
    let name = state.file_name().unwrap().to_string_lossy();
    assert!(
        !parent
            .join(format!(".{name}.portable-import.json"))
            .exists()
    );
    assert!(!parent.join(format!(".{name}.portable-stage")).exists());
    assert!(!parent.join(format!(".{name}.portable-backup")).exists());
    assert!(live.list_roots().is_empty());
    drop(live);

    let receipt =
        restore_portable_config(&state, &config, &PortableRestoreOptions::default()).unwrap();
    assert_eq!(receipt.root_count, 1);
}
