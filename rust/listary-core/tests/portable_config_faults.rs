#![cfg(feature = "test-fault-injection")]

use listary_core::{
    Core, CoreConfig, FavoriteMenu, FavoriteMenuItem, PortableConfig, PortableRestoreOptions,
    PortableRoot, Priority, PriorityRule, RefreshPolicy, RootKind, WebSearchProvider,
    restore_portable_config, write_portable_config,
};
use std::{
    env, fs,
    path::{Path, PathBuf},
    process::Command,
};
use tempfile::TempDir;

const CHILD_ENV: &str = "LISTARY_PORTABLE_TEST_CHILD";
const STATE_ENV: &str = "LISTARY_PORTABLE_TEST_STATE";
const CONFIG_ENV: &str = "LISTARY_PORTABLE_TEST_CONFIG";
const OUTPUT_ENV: &str = "LISTARY_PORTABLE_TEST_OUTPUT";
const FAILPOINT_ENV: &str = "LISTARY_CORE_TEST_FAILPOINT";
const FAILPOINT_MARKER_ENV: &str = "LISTARY_CORE_TEST_FAILPOINT_MARKER";

fn portable_config(root: &Path) -> PortableConfig {
    PortableConfig {
        roots: vec![PortableRoot {
            path: root.to_string_lossy().into_owned(),
            kind: RootKind::Local,
            refresh_policy: RefreshPolicy::Automatic,
            priority: Priority::High,
            enabled: false,
        }],
        favorites: FavoriteMenu {
            items: vec![FavoriteMenuItem::Folder {
                id: uuid::Uuid::new_v4(),
                title: Some("Fault favorite".into()),
                path: root.join("Favorite").to_string_lossy().into_owned(),
            }],
            pins: Vec::new(),
        },
        web_search_providers: vec![WebSearchProvider::new(
            "faultweb",
            "Fault web",
            "https://example.test/?q={query}",
        )],
        priority_rules: vec![PriorityRule::path(
            root.join("Priority").to_string_lossy(),
            true,
            Priority::High,
        )],
        ..PortableConfig::default()
    }
}

fn create_empty_destination(state: &Path) {
    let core = Core::open(CoreConfig::new(state.to_path_buf())).unwrap();
    drop(core);
    fs::write(state.join("pre-import-sentinel.txt"), b"original").unwrap();
}

fn run_fault_child(state: &Path, config: &Path, failpoint: &str, marker: &Path) {
    let output = Command::new(env::current_exe().unwrap())
        .arg("--exact")
        .arg("portable_restore_fault_child")
        .arg("--nocapture")
        .env(CHILD_ENV, "1")
        .env(STATE_ENV, state)
        .env(CONFIG_ENV, config)
        .env(FAILPOINT_ENV, failpoint)
        .env(FAILPOINT_MARKER_ENV, marker)
        .output()
        .unwrap();
    assert_eq!(
        output.status.code(),
        Some(86),
        "child did not exit at {failpoint}; stdout={} stderr={}",
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    );
    assert_eq!(fs::read_to_string(marker).unwrap(), failpoint);
}

fn run_file_fault_child(config: &Path, output_path: &Path, failpoint: &str, marker: &Path) {
    let output = Command::new(env::current_exe().unwrap())
        .arg("--exact")
        .arg("portable_file_write_fault_child")
        .arg("--nocapture")
        .env(CHILD_ENV, "file-write")
        .env(CONFIG_ENV, config)
        .env(OUTPUT_ENV, output_path)
        .env(FAILPOINT_ENV, failpoint)
        .env(FAILPOINT_MARKER_ENV, marker)
        .output()
        .unwrap();
    assert_eq!(
        output.status.code(),
        Some(86),
        "file child did not exit at {failpoint}; stdout={} stderr={}",
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    );
    assert_eq!(fs::read_to_string(marker).unwrap(), failpoint);
}

fn import_artifacts(state: &Path) -> (PathBuf, PathBuf, PathBuf) {
    let parent = state.parent().unwrap();
    let name = state.file_name().unwrap().to_string_lossy();
    (
        parent.join(format!(".{name}.portable-import.json")),
        parent.join(format!(".{name}.portable-stage")),
        parent.join(format!(".{name}.portable-backup")),
    )
}

#[test]
fn portable_restore_fault_child() {
    if env::var(CHILD_ENV).ok().as_deref() != Some("1") {
        return;
    }
    let state = PathBuf::from(env::var_os(STATE_ENV).unwrap());
    let config_path = PathBuf::from(env::var_os(CONFIG_ENV).unwrap());
    let config = listary_core::read_portable_config(&config_path).unwrap();
    let _ = restore_portable_config(&state, &config, &PortableRestoreOptions::default());
    panic!("portable restore child did not hit requested failpoint");
}

#[test]
fn portable_file_write_fault_child() {
    if env::var(CHILD_ENV).ok().as_deref() != Some("file-write") {
        return;
    }
    let config_path = PathBuf::from(env::var_os(CONFIG_ENV).unwrap());
    let output_path = PathBuf::from(env::var_os(OUTPUT_ENV).unwrap());
    let config = listary_core::read_portable_config(&config_path).unwrap();
    listary_core::write_portable_config(&output_path, &config).unwrap();
    panic!("portable file child did not hit requested failpoint");
}

#[test]
fn interrupted_portable_restore_recovers_or_finishes_at_the_profile_swap_boundary() {
    for failpoint in [
        "portable_after_marker_publish",
        "portable_after_stage_ready",
        "portable_after_backup_rename",
        "portable_after_target_promote",
    ] {
        let sandbox = TempDir::new().unwrap();
        let state = sandbox.path().join("state");
        let root = sandbox.path().join("root");
        fs::create_dir_all(&root).unwrap();
        create_empty_destination(&state);
        let config = portable_config(&root);
        let config_path = sandbox.path().join("portable.json");
        write_portable_config(&config_path, &config).unwrap();
        let failpoint_marker = sandbox.path().join(format!("{failpoint}.marker"));

        run_fault_child(&state, &config_path, failpoint, &failpoint_marker);

        // Every restore invocation performs recovery before validating the new
        // request. Use an intentionally unsupported schema to observe recovery
        // without starting another valid transaction.
        let invalid = PortableConfig {
            schema_version: listary_core::PORTABLE_CONFIG_SCHEMA_VERSION + 1,
            ..config.clone()
        };
        assert!(
            restore_portable_config(&state, &invalid, &PortableRestoreOptions::default()).is_err()
        );

        if failpoint == "portable_after_target_promote" {
            assert!(
                !state.join("pre-import-sentinel.txt").exists(),
                "promotion is the commit point; the new profile must remain authoritative"
            );
            let core = Core::open(CoreConfig::new(state.clone())).unwrap();
            assert_eq!(core.list_roots().len(), 1);
            assert_eq!(core.get_favorites(), config.favorites);
            assert_eq!(
                core.list_web_search_providers(),
                config.web_search_providers
            );
            assert_eq!(core.list_priority_rules(), config.priority_rules);
            drop(core);
        } else {
            assert_eq!(
                fs::read(state.join("pre-import-sentinel.txt")).unwrap(),
                b"original"
            );
            let core = Core::open(CoreConfig::new(state.clone())).unwrap();
            assert!(core.list_roots().is_empty());
            assert!(core.get_favorites().items.is_empty());
            assert!(core.list_web_search_providers().is_empty());
            assert!(core.list_priority_rules().is_empty());
            drop(core);
        }

        let receipt =
            restore_portable_config(&state, &config, &PortableRestoreOptions::default()).unwrap();
        assert_eq!(receipt.root_count, 1);
        assert_eq!(receipt.favorite_item_count, 1);
        assert_eq!(receipt.web_search_provider_count, 1);
        assert_eq!(receipt.priority_rule_count, 1);
        assert!(receipt.round_trip_equivalent);
        if failpoint == "portable_after_target_promote" {
            assert!(receipt.idempotent);
        } else {
            assert!(!receipt.idempotent);
        }

        let core = Core::open(CoreConfig::new(state.clone())).unwrap();
        assert_eq!(core.get_favorites(), config.favorites);
        assert_eq!(
            core.list_web_search_providers(),
            config.web_search_providers
        );
        assert_eq!(core.list_priority_rules(), config.priority_rules);
        drop(core);

        let (marker, stage, backup) = import_artifacts(&state);
        assert!(!marker.exists(), "marker leaked after {failpoint}");
        assert!(!stage.exists(), "stage leaked after {failpoint}");
        assert!(!backup.exists(), "backup leaked after {failpoint}");
    }
}

#[test]
fn interrupted_portable_file_publication_recovers_a_complete_generation() {
    for failpoint in [
        "portable_file_after_temp_write",
        "portable_file_after_backup_copy",
        "portable_file_after_target_remove",
        "portable_file_after_target_promote",
    ] {
        let sandbox = TempDir::new().unwrap();
        let old_root = sandbox.path().join("old-root");
        let new_root = sandbox.path().join("new-root");
        let old_config = portable_config(&old_root);
        let new_config = portable_config(&new_root);
        let old_source = sandbox.path().join("old-source.json");
        let new_source = sandbox.path().join("new-source.json");
        let published = sandbox.path().join("published.json");
        write_portable_config(&old_source, &old_config).unwrap();
        write_portable_config(&new_source, &new_config).unwrap();
        write_portable_config(&published, &old_config).unwrap();
        let marker = sandbox.path().join(format!("{failpoint}.marker"));

        run_file_fault_child(&new_source, &published, failpoint, &marker);
        let recovered = listary_core::read_portable_config(&published).unwrap();
        if failpoint == "portable_file_after_target_promote" {
            assert_eq!(recovered, new_config);
        } else {
            assert_eq!(recovered, old_config);
        }
        assert!(!published.with_extension("json.tmp").exists());
    }
}
