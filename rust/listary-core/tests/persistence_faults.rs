#![cfg(feature = "test-fault-injection")]

use listary_core::{
    ActivationSource, CancellationToken, Core, CoreConfig, DiscoveredItem, ItemKind,
    RootAvailability, RootKind, SearchRequest, StartupCacheSource,
};
use std::{
    env, fs,
    path::{Path, PathBuf},
    process::Command,
};
use tempfile::TempDir;

const CHILD_SCENARIO_ENV: &str = "LISTARY_CORE_TEST_CHILD_SCENARIO";
const STATE_DIR_ENV: &str = "LISTARY_CORE_TEST_STATE_DIR";
const FAILPOINT_ENV: &str = "LISTARY_CORE_TEST_FAILPOINT";
const FAILPOINT_MARKER_ENV: &str = "LISTARY_CORE_TEST_FAILPOINT_MARKER";

fn open_core(state: &Path) -> Core {
    Core::open(CoreConfig::new(state.to_path_buf())).expect("core opens")
}

fn search(core: &Core, query: &str) -> listary_core::SearchResponse {
    core.search(
        &SearchRequest {
            query: query.to_owned(),
            limit: 50,
            ..SearchRequest::default()
        },
        &CancellationToken::new(),
    )
    .expect("search succeeds")
}

fn search_available_only(core: &Core, query: &str) -> listary_core::SearchResponse {
    core.search(
        &SearchRequest {
            query: query.to_owned(),
            limit: 50,
            include_unavailable: false,
            ..SearchRequest::default()
        },
        &CancellationToken::new(),
    )
    .expect("search succeeds")
}

fn write(path: impl AsRef<Path>, bytes: &[u8]) {
    let path = path.as_ref();
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent).expect("create fixture parent");
    }
    fs::write(path, bytes).expect("write fixture");
}

fn run_fault_child(state: &Path, scenario: &str, failpoint: &str) {
    let marker = state.join(format!("fault-{scenario}-{failpoint}.marker"));
    let output = Command::new(env::current_exe().expect("current test executable"))
        .arg("--exact")
        .arg("fault_child_process")
        .arg("--nocapture")
        .env(CHILD_SCENARIO_ENV, scenario)
        .env(STATE_DIR_ENV, state)
        .env(FAILPOINT_ENV, failpoint)
        .env(FAILPOINT_MARKER_ENV, &marker)
        .output()
        .expect("spawn crash helper");

    assert_eq!(
        output.status.code(),
        Some(86),
        "child did not exit at failpoint {failpoint}; stdout={} stderr={}",
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    );
    assert_eq!(
        fs::read_to_string(&marker).expect("failpoint marker exists"),
        failpoint,
        "child exit must be tied to the requested failpoint"
    );
}

fn prepare_reconcile_fixture() -> (TempDir, TempDir) {
    let state = TempDir::new().expect("state tempdir");
    let files = TempDir::new().expect("files tempdir");
    write(files.path().join("old.txt"), b"old");
    write(files.path().join("delete.txt"), b"delete");
    {
        let core = open_core(state.path());
        let root = core
            .add_root(files.path().to_path_buf(), RootKind::Local)
            .expect("add root");
        core.reconcile_root(root.id, &CancellationToken::new())
            .expect("initial reconcile");
        core.flush().expect("publish initial cache");
    }

    fs::rename(
        files.path().join("old.txt"),
        files.path().join("renamed.txt"),
    )
    .expect("rename fixture");
    fs::remove_file(files.path().join("delete.txt")).expect("delete fixture");
    write(files.path().join("added.txt"), b"added");
    (state, files)
}

fn prepare_indexed_fixture() -> (TempDir, TempDir) {
    let state = TempDir::new().expect("state tempdir");
    let files = TempDir::new().expect("files tempdir");
    write(files.path().join("authority.txt"), b"authority");
    {
        let core = open_core(state.path());
        let root = core
            .add_root(files.path().to_path_buf(), RootKind::Local)
            .expect("add root");
        core.reconcile_root(root.id, &CancellationToken::new())
            .expect("initial reconcile");
        core.flush().expect("publish cache");
    }
    (state, files)
}

#[test]
fn dirty_reconcile_exit_before_catalog_commit_preserves_previous_generation() {
    let (state, _files) = prepare_reconcile_fixture();
    run_fault_child(
        state.path(),
        "dirty_reconcile",
        "store_before_snapshot_commit",
    );

    let (core, metrics) = Core::open_profiled(CoreConfig::new(state.path().to_path_buf()))
        .expect("reopen after pre-commit exit");
    assert_eq!(metrics.cache_source, Some(StartupCacheSource::Snapshot));
    assert_eq!(search(&core, "old").results.len(), 1);
    assert_eq!(search(&core, "delete").results.len(), 1);
    assert!(search(&core, "renamed").results.is_empty());
    assert!(search(&core, "added").results.is_empty());
}

#[test]
fn dirty_reconcile_exit_after_catalog_commit_rebuilds_stale_snapshot() {
    let (state, _files) = prepare_reconcile_fixture();
    run_fault_child(
        state.path(),
        "dirty_reconcile",
        "store_after_snapshot_commit",
    );

    let (core, metrics) = Core::open_profiled(CoreConfig::new(state.path().to_path_buf()))
        .expect("reopen after committed catalog exit");
    assert_eq!(
        metrics.cache_source,
        Some(StartupCacheSource::StaleGeneration)
    );
    assert!(search(&core, "old").results.is_empty());
    assert!(search(&core, "delete").results.is_empty());
    assert_eq!(search(&core, "renamed").results.len(), 1);
    assert_eq!(search(&core, "added").results.len(), 1);
}

#[test]
fn cache_temp_is_cleaned_when_old_snapshot_is_still_authoritative() {
    let (state, _files) = prepare_indexed_fixture();
    run_fault_child(state.path(), "flush", "cache_after_temp_write");

    let temp = state.path().join("cache/search-cache.bin.tmp");
    assert!(
        temp.exists(),
        "crash should leave the staged cache snapshot"
    );
    let (core, metrics) = Core::open_profiled(CoreConfig::new(state.path().to_path_buf()))
        .expect("reopen after staged cache exit");
    assert_eq!(metrics.cache_source, Some(StartupCacheSource::Snapshot));
    assert_eq!(search(&core, "authority").results.len(), 1);
    assert!(
        !temp.exists(),
        "startup should remove the stale cache temp file"
    );
}

#[test]
fn interrupted_compact_cache_payload_keeps_old_snapshot_and_cleans_partial_temp() {
    let (state, _files) = prepare_indexed_fixture();
    let snapshot = state.path().join("cache/search-cache.bin");
    let baseline_len = fs::metadata(&snapshot).unwrap().len();
    run_fault_child(state.path(), "flush", "cache_during_payload_write");

    let temp = state.path().join("cache/search-cache.bin.tmp");
    assert!(snapshot.exists(), "old authoritative snapshot must remain");
    assert!(
        temp.exists(),
        "interrupted payload should leave a temp file"
    );
    assert!(
        fs::metadata(&temp).unwrap().len() < baseline_len,
        "interrupted compact payload should be incomplete"
    );

    let (core, metrics) = Core::open_profiled(CoreConfig::new(state.path().to_path_buf()))
        .expect("reopen after interrupted compact-cache build");
    assert_eq!(metrics.cache_source, Some(StartupCacheSource::Snapshot));
    assert_eq!(search(&core, "authority").results.len(), 1);
    assert!(
        !temp.exists(),
        "startup should discard the partial temp file"
    );
}

#[test]
fn cache_publication_exit_after_target_remove_rebuilds_from_rocksdb() {
    let (state, _files) = prepare_indexed_fixture();
    run_fault_child(state.path(), "flush", "cache_after_target_remove");

    let snapshot = state.path().join("cache/search-cache.bin");
    let temp = state.path().join("cache/search-cache.bin.tmp");
    assert!(!snapshot.exists(), "target was removed before the crash");
    assert!(
        temp.exists(),
        "complete staged cache should remain after abrupt exit"
    );
    let (core, metrics) = Core::open_profiled(CoreConfig::new(state.path().to_path_buf()))
        .expect("reopen after cache publication exit");
    assert_eq!(metrics.cache_source, Some(StartupCacheSource::Missing));
    assert_eq!(search(&core, "authority").results.len(), 1);
    assert!(
        snapshot.exists(),
        "startup should republish the derived snapshot"
    );
    assert!(
        !temp.exists(),
        "startup should consume or remove the stale temp file"
    );
}

#[test]
fn history_exit_after_target_remove_recovers_backup_and_cleans_temp() {
    let (state, _files) = prepare_indexed_fixture();
    {
        let core = open_core(state.path());
        let item_id = search(&core, "authority").results[0].item.id;
        core.record_activation_with_source(item_id, 100, ActivationSource::FileSearch)
            .expect("persist baseline history");
    }
    run_fault_child(
        state.path(),
        "record_history",
        "history_after_target_remove",
    );

    let path = state.path().join("history.json");
    let backup = state.path().join("history.json.bak");
    let temp = state.path().join("history.json.tmp");
    assert!(
        !path.exists(),
        "history target was removed before the crash"
    );
    assert!(
        backup.exists(),
        "the previous history generation must be recoverable"
    );
    assert!(
        temp.exists(),
        "the new staged history generation should remain"
    );

    let core = open_core(state.path());
    let recommendation = core
        .recommendations(300, 10)
        .into_iter()
        .next()
        .expect("baseline history recovered");
    assert_eq!(recommendation.activation_count, 1);
    assert_eq!(recommendation.last_activated_unix_ms, 100);
    assert_eq!(recommendation.last_source, ActivationSource::FileSearch);
    assert!(
        !temp.exists(),
        "reopen should remove the abandoned history temp file"
    );
}

#[test]
fn first_history_write_exit_recovers_complete_staged_generation() {
    let (state, _files) = prepare_indexed_fixture();
    run_fault_child(
        state.path(),
        "record_history",
        "history_after_target_remove",
    );

    let path = state.path().join("history.json");
    let temp = state.path().join("history.json.tmp");
    assert!(!path.exists(), "first history generation was not published");
    assert!(
        temp.exists(),
        "complete first generation should remain staged"
    );

    let core = open_core(state.path());
    let recommendation = core
        .recommendations(300, 10)
        .into_iter()
        .next()
        .expect("staged first history generation recovered");
    assert_eq!(recommendation.activation_count, 1);
    assert_eq!(recommendation.last_activated_unix_ms, 200);
    assert_eq!(recommendation.last_source, ActivationSource::Launcher);
    assert!(
        path.exists(),
        "reopen should promote the valid staged history"
    );
    assert!(!temp.exists(), "promoted history temp should be consumed");
}

#[test]
fn root_removal_commit_is_atomic_across_catalog_and_root_metadata() {
    let (state, _files) = prepare_indexed_fixture();
    run_fault_child(
        state.path(),
        "remove_root",
        "store_after_root_delete_commit",
    );

    let (core, metrics) = Core::open_profiled(CoreConfig::new(state.path().to_path_buf()))
        .expect("reopen after root removal commit");
    assert_eq!(
        metrics.cache_source,
        Some(StartupCacheSource::StaleGeneration)
    );
    assert!(core.list_roots().is_empty());
    assert!(search(&core, "authority").results.is_empty());
}

#[test]
fn root_availability_and_item_flags_commit_atomically_across_restart() {
    let (before_state, _files) = prepare_indexed_fixture();
    run_fault_child(
        before_state.path(),
        "set_unavailable",
        "store_before_root_items_commit",
    );
    {
        let core = open_core(before_state.path());
        assert_eq!(
            core.list_roots()[0].availability,
            RootAvailability::Available
        );
        let result = search(&core, "authority");
        assert_eq!(result.results.len(), 1);
        assert!(result.results[0].item.available);
    }

    let (after_state, _files) = prepare_indexed_fixture();
    run_fault_child(
        after_state.path(),
        "set_unavailable",
        "store_after_root_items_commit",
    );
    {
        let core = open_core(after_state.path());
        assert_eq!(
            core.list_roots()[0].availability,
            RootAvailability::Unavailable
        );
        assert!(search_available_only(&core, "authority").results.is_empty());
        let result = search(&core, "authority");
        assert_eq!(result.results.len(), 1);
        assert!(!result.results[0].item.available);
    }

    let (available_state, _files) = prepare_indexed_fixture();
    {
        let core = open_core(available_state.path());
        let root = core.list_roots()[0].clone();
        core.set_root_unavailable(root.id, "offline".into())
            .expect("baseline unavailable");
    }
    run_fault_child(
        available_state.path(),
        "mark_available",
        "store_after_root_items_commit",
    );
    let core = open_core(available_state.path());
    assert_eq!(
        core.list_roots()[0].availability,
        RootAvailability::Available
    );
    let result = search(&core, "authority");
    assert_eq!(result.results.len(), 1);
    assert!(result.results[0].item.available);
}

#[test]
fn successful_full_reconcile_commits_root_metadata_with_catalog_generation() {
    let (state, files) = prepare_indexed_fixture();
    {
        let core = open_core(state.path());
        let root = core.list_roots()[0].clone();
        core.set_root_unavailable(root.id, "offline".into())
            .expect("baseline unavailable");
    }
    write(files.path().join("new-after-offline.txt"), b"new");

    run_fault_child(
        state.path(),
        "reconcile_root",
        "store_after_snapshot_commit",
    );

    let core = open_core(state.path());
    assert_eq!(
        core.list_roots()[0].availability,
        RootAvailability::Available
    );
    assert_eq!(search(&core, "authority").results.len(), 1);
    assert_eq!(search(&core, "new-after-offline").results.len(), 1);
    assert!(search(&core, "new-after-offline").results[0].item.available);
}

#[test]
fn upsert_batch_commits_catalog_generation_and_next_id_together() {
    let (before_state, _files) = prepare_indexed_fixture();
    run_fault_child(
        before_state.path(),
        "upsert",
        "store_before_put_items_commit",
    );
    assert!(
        search(&open_core(before_state.path()), "fault-upserted")
            .results
            .is_empty()
    );

    let (after_state, _files) = prepare_indexed_fixture();
    run_fault_child(after_state.path(), "upsert", "store_after_put_items_commit");
    let core = open_core(after_state.path());
    let committed = search(&core, "fault-upserted");
    assert_eq!(committed.results.len(), 1);
    let committed_id = committed.results[0].item.id;
    let root = core.list_roots()[0].clone();
    let next_ids = core
        .upsert_items(
            root.id,
            vec![DiscoveredItem::new(
                root.path.join("after-restart.txt"),
                ItemKind::File,
                1,
                1,
            )],
        )
        .expect("follow-up upsert");
    assert_eq!(next_ids.len(), 1);
    assert_ne!(next_ids[0], committed_id);
}

#[test]
fn fault_child_process() {
    let Some(scenario) = env::var_os(CHILD_SCENARIO_ENV) else {
        return;
    };
    let state = PathBuf::from(env::var_os(STATE_DIR_ENV).expect("child state dir"));
    let core = open_core(&state);
    let root = core
        .list_roots()
        .into_iter()
        .next()
        .expect("child fixture root");

    match scenario.to_string_lossy().as_ref() {
        "dirty_reconcile" => {
            core.reconcile_paths(root.id, vec![root.path.clone()], &CancellationToken::new())
                .expect("dirty reconcile reaches failpoint");
        }
        "flush" => {
            core.flush().expect("flush reaches failpoint");
        }
        "record_history" => {
            let item_id = search(&core, "authority").results[0].item.id;
            core.record_activation_with_source(item_id, 200, ActivationSource::Launcher)
                .expect("history write reaches failpoint");
        }
        "remove_root" => {
            core.remove_root(root.id)
                .expect("root removal reaches failpoint");
        }
        "set_unavailable" => {
            core.set_root_unavailable(root.id, "fault-injected offline".into())
                .expect("unavailable transition reaches failpoint");
        }
        "mark_available" => {
            core.mark_root_available(root.id)
                .expect("available transition reaches failpoint");
        }
        "reconcile_root" => {
            core.reconcile_root(root.id, &CancellationToken::new())
                .expect("root reconcile reaches failpoint");
        }
        "upsert" => {
            core.upsert_items(
                root.id,
                vec![DiscoveredItem::new(
                    root.path.join("fault-upserted.txt"),
                    ItemKind::File,
                    1,
                    1,
                )],
            )
            .expect("upsert reaches failpoint");
        }
        other => panic!("unknown child scenario: {other}"),
    }

    panic!("requested failpoint was not reached");
}
