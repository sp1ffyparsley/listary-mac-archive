use listary_core::{
    ActivationSource, CancellationToken, Core, CoreConfig, CoreError, CustomCommand,
    DiscoveredItem, FacetCountsRequest, FavoriteDynamicGroupKind, FavoriteMenu, FavoriteMenuItem,
    FileFacetCategory, FileSearchRequest, FilterPreset, FilterTarget, ItemKind, ModifiedTimeFacet,
    OpenFolderCandidate, Priority, RefreshPolicy, RootAvailability, RootKind, SearchContext,
    SearchRequest, StartupCacheSource, WebSearchProvider, WebSearchRequest, WebSuggestionProvider,
};
use serde_json::{Value, json};
use std::{
    fs,
    path::{Path, PathBuf},
    sync::{Arc, Barrier},
    thread,
    time::Duration,
};
use tempfile::TempDir;

fn open_core(state: &Path) -> Core {
    Core::open(CoreConfig::new(state.to_path_buf())).expect("core opens")
}

fn search(core: &Core, query: &str) -> listary_core::SearchResponse {
    let request = SearchRequest {
        query: query.to_string(),
        limit: 50,
        ..SearchRequest::default()
    };
    core.search(&request, &CancellationToken::new())
        .expect("search succeeds")
}

fn search_contract(core: &Core, request: &SearchRequest) -> Value {
    let response = core
        .search(request, &CancellationToken::new())
        .expect("contract search succeeds");
    json!({
        "results": response.results,
        "parsed": response.parsed,
        "considered_candidates": response.considered_candidates,
        "cancelled": response.cancelled,
    })
}

fn write(path: impl AsRef<Path>, bytes: &[u8]) {
    if let Some(parent) = path.as_ref().parent() {
        fs::create_dir_all(parent).unwrap();
    }
    fs::write(path, bytes).unwrap();
}

#[test]
fn reconcile_tracks_create_rename_move_and_delete() {
    let state = TempDir::new().unwrap();
    let files = TempDir::new().unwrap();
    write(files.path().join("alpha.txt"), b"alpha");
    write(files.path().join("nested/movie.mp4"), b"movie");
    write(files.path().join("delete-me.md"), b"delete");

    let core = open_core(state.path());
    let root = core
        .add_root(files.path().to_path_buf(), RootKind::Local)
        .unwrap();
    let first = core
        .reconcile_root(root.id, &CancellationToken::new())
        .unwrap();
    assert_eq!(first.discovered, 4); // nested folder + three files
    assert_eq!(search(&core, "alpha").results.len(), 1);
    assert_eq!(search(&core, "movie").results.len(), 1);
    let alpha_id = search(&core, "alpha").results[0].item.id;
    let movie_id = search(&core, "movie").results[0].item.id;

    fs::rename(
        files.path().join("alpha.txt"),
        files.path().join("beta.txt"),
    )
    .unwrap();
    fs::rename(
        files.path().join("nested/movie.mp4"),
        files.path().join("movie-moved.mp4"),
    )
    .unwrap();
    fs::remove_file(files.path().join("delete-me.md")).unwrap();
    core.reconcile_root(root.id, &CancellationToken::new())
        .unwrap();

    assert!(search(&core, "alpha").results.is_empty());
    assert_eq!(search(&core, "beta").results[0].item.name, "beta.txt");
    assert_eq!(search(&core, "beta").results[0].item.id, alpha_id);
    let moved = search(&core, "movie-moved");
    assert_eq!(moved.results.len(), 1);
    assert_eq!(moved.results[0].item.id, movie_id);
    assert_eq!(
        PathBuf::from(&moved.results[0].item.parent_path),
        files.path()
    );
    assert!(search(&core, "delete-me").results.is_empty());
}

#[test]
fn batched_rename_chains_and_cycles_keep_file_identity_across_reopen() {
    for dirty in [false, true] {
        let state = TempDir::new().unwrap();
        let files = TempDir::new().unwrap();
        let a = files.path().join("identity-alpha.txt");
        let b = files.path().join("identity-beta.txt");
        let c = files.path().join("identity-charlie.txt");
        write(&a, b"original-alpha");
        write(&b, b"original-beta");
        let core = open_core(state.path());
        let root = core
            .add_root(files.path().to_path_buf(), RootKind::Local)
            .unwrap();
        core.reconcile_root(root.id, &CancellationToken::new())
            .unwrap();
        let original_a = core.get_item_by_path(&a.to_string_lossy()).unwrap();
        let original_b = core.get_item_by_path(&b.to_string_lossy()).unwrap();
        assert!(original_a.stable_file_id.is_some());
        assert_ne!(original_a.stable_file_id, original_b.stable_file_id);
        core.record_activation(original_a.id, 1_000).unwrap();
        fs::rename(&a, &c).unwrap();
        fs::rename(&b, &a).unwrap();
        let reconcile = |core: &Core| {
            if dirty {
                core.reconcile_paths(
                    root.id,
                    vec![c.clone(), a.clone(), b.clone()],
                    &CancellationToken::new(),
                )
                .unwrap();
            } else {
                core.reconcile_root(root.id, &CancellationToken::new())
                    .unwrap();
            }
        };
        reconcile(&core);
        assert_eq!(
            core.get_item_by_path(&a.to_string_lossy()).unwrap().id,
            original_b.id
        );
        assert_eq!(
            core.get_item_by_path(&c.to_string_lossy()).unwrap().id,
            original_a.id
        );
        assert!(core.get_item_by_path(&b.to_string_lossy()).is_none());
        assert_eq!(search(&core, "identity").results.len(), 2);
        assert_eq!(core.diagnostics().unwrap().history_entries, 1);

        // Swap the two files without an unused final pathname.
        fs::rename(&a, &b).unwrap();
        fs::rename(&c, &a).unwrap();
        fs::rename(&b, &c).unwrap();
        reconcile(&core);
        reconcile(&core); // A replay cannot steal or duplicate IDs.
        assert_eq!(
            core.get_item_by_path(&a.to_string_lossy()).unwrap().id,
            original_a.id
        );
        assert_eq!(
            core.get_item_by_path(&c.to_string_lossy()).unwrap().id,
            original_b.id
        );
        core.flush().unwrap();
        drop(core);
        let reopened = open_core(state.path());
        assert_eq!(search(&reopened, "identity").results.len(), 2);
        assert_eq!(
            reopened.get_item_by_path(&a.to_string_lossy()).unwrap().id,
            original_a.id
        );
        assert_eq!(
            reopened.get_item_by_path(&c.to_string_lossy()).unwrap().id,
            original_b.id
        );
        assert!(reopened.get_item_by_path(&b.to_string_lossy()).is_none());
    }
}

#[test]
fn renamed_hardlink_occurrence_keeps_identity_after_unchanged_sibling_is_claimed() {
    let state = TempDir::new().unwrap();
    let files = TempDir::new().unwrap();
    let original = files.path().join("hardlink-origin.txt");
    let alias_old = files.path().join("hardlink-alias-old.txt");
    let alias_new = files.path().join("hardlink-alias-new.txt");
    write(&original, b"shared hardlink object");
    fs::hard_link(&original, &alias_old).unwrap();

    let core = open_core(state.path());
    let root = core
        .add_root(files.path().to_path_buf(), RootKind::Local)
        .unwrap();
    core.reconcile_root(root.id, &CancellationToken::new())
        .unwrap();
    let original_before = core.get_item_by_path(&original.to_string_lossy()).unwrap();
    let alias_before = core.get_item_by_path(&alias_old.to_string_lossy()).unwrap();
    assert_ne!(original_before.id, alias_before.id);
    assert_eq!(original_before.stable_file_id, alias_before.stable_file_id);

    fs::rename(&alias_old, &alias_new).unwrap();
    core.reconcile_root(root.id, &CancellationToken::new())
        .unwrap();

    let original_after = core.get_item_by_path(&original.to_string_lossy()).unwrap();
    let alias_after = core.get_item_by_path(&alias_new.to_string_lossy()).unwrap();
    assert_eq!(original_after.id, original_before.id);
    assert_eq!(alias_after.stable_file_id, alias_before.stable_file_id);
    assert_eq!(
        alias_after.id, alias_before.id,
        "once the unchanged hardlink occurrence claims its exact path identity, the single remaining old/new occurrence is unambiguous"
    );
    assert!(
        core.get_item_by_path(&alias_old.to_string_lossy())
            .is_none()
    );

    core.flush().unwrap();
    drop(core);
    let reopened = open_core(state.path());
    assert_eq!(
        reopened
            .get_item_by_path(&alias_new.to_string_lossy())
            .unwrap()
            .id,
        alias_before.id
    );
}

#[cfg(windows)]
#[test]
fn junction_occurrence_identity_dirty_reconcile_and_broken_target_match_full_crawl() {
    use std::process::{Command, Stdio};

    fn create_junction(link: &Path, target: &Path) {
        let status = Command::new("cmd")
            .args(["/c", "mklink", "/J"])
            .arg(link)
            .arg(target)
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .status()
            .expect("launch mklink for disposable junction fixture");
        assert!(status.success(), "create disposable junction fixture");
    }

    let state = TempDir::new().unwrap();
    let files = TempDir::new().unwrap();
    let outside = TempDir::new().unwrap();
    let target = files.path().join("target");
    let alias = files.path().join("alias-junction");
    let escape = files.path().join("escape-junction");
    let target_child = target.join("needle.txt");
    let alias_child = alias.join("needle.txt");
    write(&target_child, b"needle");
    create_junction(&alias, &target);
    create_junction(&escape, outside.path());

    let core = open_core(state.path());
    let root = core
        .add_root(files.path().to_path_buf(), RootKind::Local)
        .unwrap();
    core.reconcile_root(root.id, &CancellationToken::new())
        .unwrap();

    let alias_before = core.get_item_by_path(&alias.to_string_lossy()).unwrap();
    let target_before = core.get_item_by_path(&target.to_string_lossy()).unwrap();
    assert_ne!(alias_before.id, target_before.id);
    assert_ne!(alias_before.stable_file_id, target_before.stable_file_id);
    assert!(
        core.get_item_by_path(&alias_child.to_string_lossy())
            .is_none()
    );

    let before = core.diagnostics().unwrap();
    core.reconcile_paths(root.id, vec![alias.clone()], &CancellationToken::new())
        .unwrap();
    let after_alias_reconcile = core.diagnostics().unwrap();
    assert_eq!(after_alias_reconcile.item_count, before.item_count);
    assert_eq!(
        after_alias_reconcile.catalog_generation, before.catalog_generation,
        "replaying one junction occurrence cannot materialize its target subtree"
    );
    assert_eq!(
        core.get_item_by_path(&alias.to_string_lossy()).unwrap().id,
        alias_before.id
    );
    assert!(
        core.get_item_by_path(&alias_child.to_string_lossy())
            .is_none()
    );

    let generation_before_escape = core.catalog_generation().unwrap();
    assert!(matches!(
        core.reconcile_paths(root.id, vec![escape], &CancellationToken::new()),
        Err(CoreError::InvalidRequest(_))
    ));
    assert_eq!(core.catalog_generation().unwrap(), generation_before_escape);

    fs::remove_dir_all(&target).unwrap();
    core.reconcile_paths(
        root.id,
        vec![target, alias.clone()],
        &CancellationToken::new(),
    )
    .unwrap();
    let broken_alias = core.get_item_by_path(&alias.to_string_lossy()).unwrap();
    assert_eq!(broken_alias.id, alias_before.id);
    assert_eq!(broken_alias.stable_file_id, alias_before.stable_file_id);
    assert!(
        core.get_item_by_path(&target_child.to_string_lossy())
            .is_none()
    );

    core.flush().unwrap();
    drop(core);
    let reopened = open_core(state.path());
    reopened
        .reconcile_root(root.id, &CancellationToken::new())
        .unwrap();
    let reopened_alias = reopened.get_item_by_path(&alias.to_string_lossy()).unwrap();
    assert_eq!(reopened_alias.id, alias_before.id);
    assert_eq!(reopened_alias.stable_file_id, alias_before.stable_file_id);
    assert!(
        reopened
            .get_item_by_path(&alias_child.to_string_lossy())
            .is_none()
    );
}

#[test]
fn same_path_replacement_retires_old_id_in_cache_and_durable_catalog() {
    for dirty in [false, true] {
        let state = TempDir::new().unwrap();
        let files = TempDir::new().unwrap();
        let replacements = TempDir::new_in(files.path().parent().unwrap()).unwrap();
        let path = files.path().join("replacement-target.txt");
        let replacement = replacements.path().join("incoming.txt");
        write(&path, b"old");
        write(&replacement, b"new-file-created-before-old-removal");
        let core = open_core(state.path());
        let root = core
            .add_root(files.path().to_path_buf(), RootKind::Local)
            .unwrap();
        core.reconcile_root(root.id, &CancellationToken::new())
            .unwrap();
        let old = core.get_item_by_path(&path.to_string_lossy()).unwrap();
        core.record_activation(old.id, 1_000).unwrap();
        fs::remove_file(&path).unwrap();
        fs::rename(&replacement, &path).unwrap();
        if dirty {
            core.reconcile_paths(root.id, vec![path.clone()], &CancellationToken::new())
                .unwrap();
        } else {
            core.reconcile_root(root.id, &CancellationToken::new())
                .unwrap();
        }
        let new = core.get_item_by_path(&path.to_string_lossy()).unwrap();
        assert_ne!(new.stable_file_id, old.stable_file_id);
        assert_ne!(new.id, old.id);
        assert!(core.get_item(old.id).is_none());
        assert_eq!(search(&core, "replacement-target").results.len(), 1);
        core.flush().unwrap();
        drop(core);
        let reopened = open_core(state.path());
        assert!(reopened.get_item(old.id).is_none());
        assert_eq!(
            reopened
                .get_item_by_path(&path.to_string_lossy())
                .unwrap()
                .id,
            new.id
        );
        assert_eq!(reopened.diagnostics().unwrap().item_count, 1);
    }
}

#[test]
fn app_bundle_reconciliation_repairs_private_rows_and_survives_reopen() {
    for dirty in [false, true] {
        let state = TempDir::new().unwrap();
        let files = TempDir::new().unwrap();
        let bundle = files.path().join("\u{00c9}ditor Suite.APP");
        let direct = bundle.join("private-direct.txt");
        let contents = bundle.join("Contents");
        let nested_bundle = contents.join("Helper.app");
        let nested_file = nested_bundle.join("private-helper.txt");
        let ordinary = files.path().join("Example.app.backup").join("ordinary.txt");
        write(&direct, b"private direct file");
        write(&nested_file, b"private nested file");
        write(&ordinary, b"ordinary data");
        let core = open_core(state.path());
        let root = core
            .add_root(files.path().to_path_buf(), RootKind::Local)
            .unwrap();
        let report = core
            .reconcile_root(root.id, &CancellationToken::new())
            .unwrap();
        assert_eq!(report.discovered, 3);
        assert_eq!(core.diagnostics().unwrap().item_count, 3);
        assert!(search(&core, "private").results.is_empty());
        let app = core.get_item_by_path(&bundle.to_string_lossy()).unwrap();
        assert_eq!(app.kind, ItemKind::App);
        core.record_activation(app.id, 1_000).unwrap();
        let ordinary_id = core
            .get_item_by_path(&ordinary.to_string_lossy())
            .unwrap()
            .id;

        // Model rows published by the old incremental crawler. One later event
        // must repair the entire bundle, including stale siblings of that event.
        let leaked = [
            (&direct, ItemKind::File),
            (&contents, ItemKind::Folder),
            (&nested_bundle, ItemKind::App),
            (&nested_file, ItemKind::File),
        ];
        core.upsert_items(
            root.id,
            leaked
                .iter()
                .map(|(path, kind)| DiscoveredItem::new((*path).clone(), *kind, 1, 1))
                .collect(),
        )
        .unwrap();
        let leaked_ids = leaked
            .iter()
            .map(|(path, _)| core.get_item_by_path(&path.to_string_lossy()).unwrap().id)
            .collect::<Vec<_>>();
        let before = core.diagnostics().unwrap().catalog_generation;
        if dirty {
            let report = core
                .reconcile_paths_at_generation(
                    root.id,
                    vec![nested_file.clone()],
                    Some(before),
                    &CancellationToken::new(),
                )
                .unwrap();
            assert_eq!(report.discovered, 1);
        } else {
            core.reconcile_root(root.id, &CancellationToken::new())
                .unwrap();
        }
        assert!(core.diagnostics().unwrap().catalog_generation > before);
        assert_eq!(core.diagnostics().unwrap().item_count, 3);
        for id in &leaked_ids {
            assert!(core.get_item(*id).is_none());
        }
        assert_eq!(
            core.get_item(app.id).unwrap().path,
            bundle.to_string_lossy()
        );
        assert_eq!(
            core.get_item(ordinary_id).unwrap().path,
            ordinary.to_string_lossy()
        );

        let generation = core.diagnostics().unwrap().catalog_generation;
        for _ in 0..2 {
            let report = core
                .reconcile_paths_at_generation(
                    root.id,
                    vec![
                        nested_file.clone(),
                        direct.clone(),
                        nested_bundle.clone(),
                        bundle.join("deleted.txt"),
                        nested_file.clone(),
                    ],
                    Some(generation),
                    &CancellationToken::new(),
                )
                .unwrap();
            assert_eq!(report.discovered, 1, "bundle events must coalesce");
            assert_eq!(core.diagnostics().unwrap().catalog_generation, generation);
        }
        core.flush().unwrap();
        drop(core);
        let reopened = open_core(state.path());
        assert_eq!(reopened.diagnostics().unwrap().item_count, 3);
        assert_eq!(reopened.get_item(app.id).unwrap().kind, ItemKind::App);
        assert!(search(&reopened, "private").results.is_empty());
        assert_eq!(
            search(&reopened, "ordinary").results[0].item.id,
            ordinary_id
        );
        assert_eq!(reopened.recommendations(1_100, 10)[0].item.id, app.id);
        for id in leaked_ids {
            assert!(reopened.get_item(id).is_none());
        }
    }
}

#[test]
fn app_bundle_nested_events_track_rename_delete_and_ordinary_folder_updates() {
    let state = TempDir::new().unwrap();
    let files = TempDir::new().unwrap();
    let bundle = files.path().join("Original.app");
    let renamed = files.path().join("Renamed.app");
    let private_relative = Path::new("Contents").join("Helper.app").join("private.txt");
    let ordinary = files.path().join("ordinary").join("before.txt");
    let ordinary_renamed = ordinary.with_file_name("after.txt");
    write(bundle.join(&private_relative), b"private");
    write(&ordinary, b"before");
    let core = open_core(state.path());
    let root = core
        .add_root(files.path().to_path_buf(), RootKind::Local)
        .unwrap();
    core.reconcile_root(root.id, &CancellationToken::new())
        .unwrap();
    let app = core.get_item_by_path(&bundle.to_string_lossy()).unwrap();
    let file = core.get_item_by_path(&ordinary.to_string_lossy()).unwrap();
    core.record_activation(app.id, 1_000).unwrap();

    fs::rename(&bundle, &renamed).unwrap();
    fs::rename(&ordinary, &ordinary_renamed).unwrap();
    write(&ordinary_renamed, b"after with changed size");
    let events = vec![
        renamed.join(&private_relative),
        bundle.join(&private_relative),
        ordinary_renamed.clone(),
        ordinary.clone(),
    ];
    core.reconcile_paths(root.id, events.clone(), &CancellationToken::new())
        .unwrap();
    assert!(core.get_item_by_path(&bundle.to_string_lossy()).is_none());
    assert_eq!(
        core.get_item(app.id).unwrap().path,
        renamed.to_string_lossy()
    );
    assert_eq!(
        core.get_item(file.id).unwrap().path,
        ordinary_renamed.to_string_lossy()
    );
    assert_eq!(core.get_item(file.id).unwrap().size, 23);
    assert!(search(&core, "private").results.is_empty());
    assert_eq!(core.diagnostics().unwrap().item_count, 3);
    let generation = core.diagnostics().unwrap().catalog_generation;
    core.reconcile_paths(root.id, events, &CancellationToken::new())
        .unwrap();
    assert_eq!(core.diagnostics().unwrap().catalog_generation, generation);
    core.flush().unwrap();
    drop(core);

    let reopened = open_core(state.path());
    assert_eq!(
        reopened.get_item(app.id).unwrap().path,
        renamed.to_string_lossy()
    );
    assert_eq!(reopened.recommendations(1_100, 10)[0].item.id, app.id);
    fs::remove_dir_all(&renamed).unwrap();
    fs::remove_file(&ordinary_renamed).unwrap();
    reopened
        .reconcile_paths(
            root.id,
            vec![renamed.join(&private_relative), ordinary_renamed],
            &CancellationToken::new(),
        )
        .unwrap();
    assert!(reopened.get_item(app.id).is_none());
    assert!(reopened.get_item(file.id).is_none());
    assert_eq!(reopened.diagnostics().unwrap().item_count, 1); // ordinary folder remains
    reopened.flush().unwrap();
    drop(reopened);
    let reopened = open_core(state.path());
    assert!(reopened.get_item(app.id).is_none());
    assert!(reopened.get_item(file.id).is_none());
    assert_eq!(reopened.diagnostics().unwrap().item_count, 1);
}

#[test]
fn app_bundle_dirty_path_promotion_keeps_admission_guards() {
    let state = TempDir::new().unwrap();
    let files = TempDir::new().unwrap();
    let outside = TempDir::new().unwrap();
    let bundle = files.path().join("Example.app");
    let private = bundle.join("Contents").join("private.txt");
    write(&private, b"private");
    let core = open_core(state.path());
    let root = core
        .add_root(files.path().to_path_buf(), RootKind::Local)
        .unwrap();
    core.reconcile_root(root.id, &CancellationToken::new())
        .unwrap();
    let generation = core.diagnostics().unwrap().catalog_generation;
    for path in [
        bundle.join("..").join("outside.txt"),
        outside
            .path()
            .join("Other.app")
            .join("Contents")
            .join("private.txt"),
    ] {
        assert!(matches!(
            core.reconcile_paths(root.id, vec![path], &CancellationToken::new()),
            Err(CoreError::InvalidRequest(_))
        ));
    }
    assert!(matches!(
        core.reconcile_paths_at_generation(
            root.id,
            vec![private.clone()],
            Some(generation - 1),
            &CancellationToken::new()
        ),
        Err(CoreError::StaleCatalog { .. })
    ));
    let cancelled = CancellationToken::new();
    cancelled.cancel();
    assert!(matches!(
        core.reconcile_paths(root.id, vec![private], &cancelled),
        Err(CoreError::Cancelled)
    ));
    assert_eq!(core.diagnostics().unwrap().catalog_generation, generation);
    assert_eq!(core.diagnostics().unwrap().item_count, 1);
}

#[test]
#[cfg(any(unix, windows))]
fn app_bundle_dirty_path_promotion_checks_original_and_promoted_symlink_targets() {
    #[cfg(unix)]
    use std::os::unix::fs::symlink as symlink_dir;
    #[cfg(windows)]
    use std::os::windows::fs::symlink_dir;

    let state = TempDir::new().unwrap();
    let files = TempDir::new().unwrap();
    let outside = TempDir::new().unwrap();
    let bundle = files.path().join("Example.app");
    let external = outside.path().join("External.app");
    let safe = files.path().join("ordinary");
    write(bundle.join("Contents").join("private.txt"), b"private");
    write(external.join("secret.txt"), b"outside");
    write(safe.join("safe.txt"), b"inside");
    let escape = bundle.join("escape");
    if let Err(error) = symlink_dir(&external, &escape) {
        #[cfg(windows)]
        if error.kind() == std::io::ErrorKind::PermissionDenied
            || error.raw_os_error() == Some(1314)
        {
            eprintln!("symlink fixture unavailable: {error}; native symlink proof remains pending");
            return;
        }
        panic!("create disposable symlink fixture: {error}");
    }
    let alias_bundle = files.path().join("Alias.app");
    symlink_dir(&external, &alias_bundle).unwrap();
    symlink_dir(&safe, external.join("back-inside")).unwrap();
    let core = open_core(state.path());
    let root = core
        .add_root(files.path().to_path_buf(), RootKind::Local)
        .unwrap();
    core.reconcile_root(root.id, &CancellationToken::new())
        .unwrap();
    let before = core.diagnostics().unwrap();

    // A safe promoted bundle must not hide an escaping original event. An
    // original event resolving back inside the root must not authorize a
    // promoted bundle whose own target is outside it either.
    for path in [
        escape.join("secret.txt"),
        alias_bundle.join("back-inside").join("safe.txt"),
    ] {
        assert!(matches!(
            core.reconcile_paths(root.id, vec![path], &CancellationToken::new()),
            Err(CoreError::InvalidRequest(_))
        ));
        assert_eq!(
            core.diagnostics().unwrap().catalog_generation,
            before.catalog_generation
        );
        assert_eq!(core.diagnostics().unwrap().item_count, before.item_count);
    }
}

#[test]
fn duplicate_and_out_of_order_dirty_paths_converge_to_current_disk_state() {
    let state = TempDir::new().unwrap();
    let files = TempDir::new().unwrap();
    let nested = files.path().join("nested");
    write(nested.join("rename-me.txt"), b"rename");
    write(nested.join("remove-me.txt"), b"remove");

    let core = open_core(state.path());
    let root = core
        .add_root(files.path().to_path_buf(), RootKind::Local)
        .unwrap();
    core.reconcile_root(root.id, &CancellationToken::new())
        .unwrap();
    let original_rename_id = search(&core, "rename-me").results[0].item.id;

    fs::rename(nested.join("rename-me.txt"), nested.join("renamed.txt")).unwrap();
    fs::remove_file(nested.join("remove-me.txt")).unwrap();
    write(nested.join("added.txt"), b"added");

    let noisy_batch = vec![
        nested.join("added.txt"),
        nested.join("rename-me.txt"),
        nested.clone(),
        nested.join("renamed.txt"),
        nested.join("added.txt"),
        nested.join("remove-me.txt"),
    ];
    core.reconcile_paths(root.id, noisy_batch.clone(), &CancellationToken::new())
        .unwrap();
    let generation_after_first_reconcile = core.catalog_generation().unwrap();

    assert!(search(&core, "rename-me").results.is_empty());
    assert!(search(&core, "remove-me").results.is_empty());
    let renamed_id = search(&core, "renamed").results[0].item.id;
    assert_eq!(renamed_id, original_rename_id);
    let added_id = search(&core, "added").results[0].item.id;

    // Replaying the same duplicate/out-of-order watcher batch must be idempotent.
    let mut replay = noisy_batch;
    replay.reverse();
    core.reconcile_paths(root.id, replay, &CancellationToken::new())
        .unwrap();
    assert_eq!(
        core.catalog_generation().unwrap(),
        generation_after_first_reconcile,
        "a duplicate/out-of-order dirty batch with no filesystem delta must not churn catalog generation"
    );
    assert_eq!(search(&core, "renamed").results[0].item.id, renamed_id);
    assert_eq!(search(&core, "added").results[0].item.id, added_id);

    // A stale-looking create path replayed after the file is gone cannot resurrect it:
    // reconciliation always derives the final answer from the current filesystem.
    fs::remove_file(nested.join("added.txt")).unwrap();
    core.reconcile_paths(
        root.id,
        vec![
            nested.join("added.txt"),
            nested.clone(),
            nested.join("added.txt"),
        ],
        &CancellationToken::new(),
    )
    .unwrap();
    assert!(search(&core, "added").results.is_empty());
    assert_eq!(search(&core, "renamed").results[0].item.id, renamed_id);
}

#[test]
fn dirty_reconcile_generation_fence_rejects_stale_batch_without_mutation() {
    let state = TempDir::new().unwrap();
    let files = TempDir::new().unwrap();
    write(files.path().join("existing.txt"), b"existing");

    let core = open_core(state.path());
    let root = core
        .add_root(files.path().to_path_buf(), RootKind::Local)
        .unwrap();
    core.reconcile_root(root.id, &CancellationToken::new())
        .unwrap();
    let stale_generation = core.catalog_generation().unwrap();

    write(files.path().join("late.txt"), b"late");
    core.set_root_unavailable(root.id, "temporary disconnect".into())
        .unwrap();
    core.mark_root_available(root.id).unwrap();
    let current_generation = core.catalog_generation().unwrap();
    assert!(current_generation > stale_generation);

    let error = core
        .reconcile_paths_at_generation(
            root.id,
            vec![files.path().join("late.txt")],
            Some(stale_generation),
            &CancellationToken::new(),
        )
        .unwrap_err();
    assert!(matches!(
        error,
        CoreError::StaleCatalog { expected, actual }
            if expected == stale_generation && actual == current_generation
    ));
    assert!(search(&core, "late").results.is_empty());

    core.reconcile_paths_at_generation(
        root.id,
        vec![files.path().join("late.txt")],
        Some(current_generation),
        &CancellationToken::new(),
    )
    .unwrap();
    assert_eq!(search(&core, "late").results.len(), 1);
}

#[test]
fn overlapping_roots_are_rejected_in_both_directions_and_on_update() {
    let state = TempDir::new().unwrap();
    let core = open_core(state.path());
    let root = core
        .add_root(PathBuf::from("X:/roots/project"), RootKind::Local)
        .unwrap();

    assert!(
        core.add_root(PathBuf::from("X:/roots/project/nested"), RootKind::Local)
            .is_err()
    );
    assert!(
        core.add_root(PathBuf::from("X:/roots"), RootKind::Local)
            .is_err()
    );

    let mut sibling = core
        .add_root(PathBuf::from("X:/roots-sibling"), RootKind::Local)
        .unwrap();
    sibling.path = PathBuf::from("X:/roots/project/nested");
    assert!(core.put_root(sibling).is_err());
    assert_eq!(core.list_roots().len(), 2);
    assert!(core.list_roots().iter().any(|item| item.id == root.id));
}

#[test]
fn persistence_custom_roots_and_history_survive_restart() {
    let state = TempDir::new().unwrap();
    let files = TempDir::new().unwrap();
    write(files.path().join("SpiffyScene.mov"), b"scene");

    let root_id;
    let item_id;
    {
        let core = open_core(state.path());
        let root = core
            .add_root(files.path().to_path_buf(), RootKind::Local)
            .unwrap();
        root_id = root.id;
        core.reconcile_root(root.id, &CancellationToken::new())
            .unwrap();
        let result = search(&core, "spiffyscene");
        item_id = result.results[0].item.id;
        core.record_activation_with_source(
            item_id,
            1_800_000_000_000,
            ActivationSource::FileSearch,
        )
        .unwrap();
        core.flush().unwrap();
    }

    let reopened = open_core(state.path());
    assert!(reopened.list_roots().iter().any(|r| r.id == root_id));
    let result = search(&reopened, "spiffyscene");
    assert_eq!(result.results[0].item.id, item_id);
    let recommendation = reopened
        .recommendations(1_800_000_000_100, 10)
        .into_iter()
        .next()
        .unwrap();
    assert_eq!(recommendation.item.id, item_id);
    assert_eq!(recommendation.last_source, ActivationSource::FileSearch);
    assert_eq!(reopened.diagnostics().unwrap().history_entries, 1);
}

#[test]
fn favorites_preserve_order_command_integrity_and_dynamic_sources() {
    let state = TempDir::new().unwrap();
    let recent_path = "X:/favorites/Recent";
    let other_path = "X:/favorites/Other";
    let command_id;
    let menu;

    {
        let core = open_core(state.path());
        let root = core
            .add_root(PathBuf::from("X:/favorites"), RootKind::Local)
            .unwrap();
        core.replace_root_snapshot(
            root.id,
            vec![
                DiscoveredItem::new(PathBuf::from(recent_path), ItemKind::Folder, 0, 10),
                DiscoveredItem::new(PathBuf::from(other_path), ItemKind::Folder, 0, 20),
            ],
        )
        .unwrap();

        let command = core
            .put_command(CustomCommand::new(
                "favorite",
                "Favorite command",
                "favorite-tool",
            ))
            .unwrap();
        command_id = command.id;
        let invalid = FavoriteMenu {
            items: vec![FavoriteMenuItem::Command {
                id: uuid::Uuid::new_v4(),
                command_id: uuid::Uuid::new_v4(),
            }],
            pins: Vec::new(),
        };
        assert!(
            core.put_favorites(invalid)
                .unwrap_err()
                .to_string()
                .contains("unknown command")
        );

        menu = FavoriteMenu {
            items: vec![
                FavoriteMenuItem::Folder {
                    id: uuid::Uuid::new_v4(),
                    title: Some("Pinned".into()),
                    path: other_path.into(),
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
                            command_id,
                        },
                    ],
                },
                FavoriteMenuItem::DynamicGroup {
                    id: uuid::Uuid::new_v4(),
                    title: Some("Recent folders".into()),
                    kind: FavoriteDynamicGroupKind::RecentFolders,
                    limit: 5,
                },
                FavoriteMenuItem::DynamicGroup {
                    id: uuid::Uuid::new_v4(),
                    title: Some("Open folders".into()),
                    kind: FavoriteDynamicGroupKind::OpenFolders,
                    limit: 5,
                },
            ],
            pins: Vec::new(),
        };
        assert_eq!(core.put_favorites(menu.clone()).unwrap(), menu);
        let recent_id = core.get_item_by_path(recent_path).unwrap().id;
        core.record_activation_with_source(
            recent_id,
            1_800_000_000_000,
            ActivationSource::QuickSwitch,
        )
        .unwrap();

        let resolved = core
            .resolve_favorites(
                1_800_000_000_100,
                vec![
                    OpenFolderCandidate {
                        path: "/Users/test/Open".into(),
                        title: Some("Open Project".into()),
                    },
                    OpenFolderCandidate {
                        path: "/Users/test/Open/".into(),
                        title: Some("Duplicate".into()),
                    },
                ],
            )
            .unwrap();
        let resolved = serde_json::to_value(resolved).unwrap();
        assert_eq!(resolved["items"][0]["source"], "favorite");
        assert_eq!(
            resolved["items"][1]["items"][1]["command_id"],
            command_id.to_string()
        );
        assert_eq!(resolved["items"][2]["items"][0]["source"], "recent");
        assert_eq!(resolved["items"][2]["items"][0]["path"], recent_path);
        assert_eq!(resolved["items"][3]["items"].as_array().unwrap().len(), 1);
        assert_eq!(resolved["items"][3]["items"][0]["source"], "open_folder");
        assert_eq!(resolved["items"][3]["items"][0]["path"], "/Users/test/Open");

        assert!(
            core.remove_command(command_id)
                .unwrap_err()
                .to_string()
                .contains("referenced by favorites menu")
        );
        core.flush().unwrap();
    }

    let reopened = open_core(state.path());
    assert_eq!(reopened.get_favorites(), menu);
    let resolved = serde_json::to_value(
        reopened
            .resolve_favorites(1_800_000_000_200, Vec::new())
            .unwrap(),
    )
    .unwrap();
    assert_eq!(resolved["items"][2]["items"][0]["source"], "recent");
    reopened.put_favorites(FavoriteMenu::default()).unwrap();
    reopened.remove_command(command_id).unwrap();
}

#[test]
fn web_search_providers_persist_resolve_in_order_and_do_not_enter_local_search() {
    let state = TempDir::new().unwrap();
    let files = TempDir::new().unwrap();
    write(files.path().join("store-notes.txt"), b"local result");
    let first_id = uuid::Uuid::new_v4();
    let second_id = uuid::Uuid::new_v4();
    let disabled_id = uuid::Uuid::new_v4();
    let providers;

    {
        let core = open_core(state.path());
        let root = core
            .add_root(files.path().to_path_buf(), RootKind::Local)
            .unwrap();
        core.reconcile_root(root.id, &CancellationToken::new())
            .unwrap();

        let mut first =
            WebSearchProvider::new("Store", "First store", "https://first.test/?q={query}");
        first.id = first_id;
        first.suggestion_provider = WebSuggestionProvider::Google;
        let mut second =
            WebSearchProvider::new("store", "Second store", "https://second.test/?q={query}");
        second.id = second_id;
        second.suggestion_provider = WebSuggestionProvider::Custom {
            url_template: "https://suggest.test/?q={query}".into(),
        };
        let mut disabled = WebSearchProvider::new(
            "store",
            "Disabled store",
            "https://disabled.test/?q={query}",
        );
        disabled.id = disabled_id;
        disabled.enabled = false;
        providers = vec![first, second, disabled];

        let canonical = core
            .replace_web_search_providers(providers.clone())
            .unwrap();
        assert_eq!(canonical[0].keyword, "store");
        assert_eq!(canonical[1].keyword, "store");
        assert_eq!(canonical[0].id, first_id);
        assert_eq!(canonical[1].id, second_id);

        let resolved = core
            .resolve_web_search(&WebSearchRequest::Input {
                input: "STORE café / test".into(),
            })
            .unwrap();
        assert_eq!(resolved.matches.len(), 2);
        assert_eq!(resolved.matches[0].provider_id, first_id);
        assert_eq!(resolved.matches[1].provider_id, second_id);
        assert_eq!(
            resolved.matches[0].url,
            "https://first.test/?q=caf%C3%A9%20%2F%20test"
        );
        let direct = core
            .resolve_web_search(&WebSearchRequest::Provider {
                provider_id: second_id,
                query: "rust ffi".into(),
            })
            .unwrap();
        assert_eq!(direct.matches[0].url, "https://second.test/?q=rust%20ffi");
        assert!(
            core.resolve_web_search(&WebSearchRequest::Provider {
                provider_id: disabled_id,
                query: "blocked".into(),
            })
            .is_err()
        );

        // Web provider settings and resolution never enter the local catalog/search pipeline.
        let local = search(&core, "store");
        assert_eq!(local.results.len(), 1);
        assert_eq!(local.results[0].item.name, "store-notes.txt");
        core.flush().unwrap();
    }

    let reopened = open_core(state.path());
    assert_eq!(reopened.list_web_search_providers().len(), 3);
    assert_eq!(reopened.list_web_search_providers()[0].id, first_id);
    assert_eq!(reopened.list_web_search_providers()[1].id, second_id);
    let resolved = reopened
        .resolve_web_search(&WebSearchRequest::Input {
            input: "store persisted".into(),
        })
        .unwrap();
    assert_eq!(resolved.matches.len(), 2);
    assert_eq!(search(&reopened, "store").results.len(), 1);
}

#[test]
fn file_search_facets_reuse_query_scope_and_cross_filter_without_history_ownership() {
    const DAY_MS: i64 = 86_400_000;
    const NOW: i64 = 1_800_057_600_000;
    let start_today = NOW - (NOW % DAY_MS);
    let state = TempDir::new().unwrap();
    let core = open_core(state.path());
    let root = core
        .add_root(PathBuf::from("X:/facets"), RootKind::Local)
        .unwrap();
    let mut ignored = DiscoveredItem::new(
        PathBuf::from("X:/facets/ignored.pdf"),
        ItemKind::File,
        10,
        start_today + 1_000,
    );
    ignored.priority = Priority::Ignored;
    core.replace_root_snapshot(
        root.id,
        vec![
            DiscoveredItem::new(
                PathBuf::from("X:/facets/Folder"),
                ItemKind::Folder,
                0,
                start_today + 1_000,
            ),
            DiscoveredItem::new(
                PathBuf::from("X:/facets/notes.pdf"),
                ItemKind::File,
                10,
                start_today - 2 * DAY_MS,
            ),
            DiscoveredItem::new(
                PathBuf::from("X:/facets/still.png"),
                ItemKind::File,
                10,
                start_today - 10 * DAY_MS,
            ),
            DiscoveredItem::new(
                PathBuf::from("X:/facets/clip.mov"),
                ItemKind::File,
                10,
                start_today - 100 * DAY_MS,
            ),
            DiscoveredItem::new(
                PathBuf::from("X:/facets/track.flac"),
                ItemKind::File,
                10,
                start_today - 400 * DAY_MS,
            ),
            DiscoveredItem::new(
                PathBuf::from("X:/facets/archive.7z"),
                ItemKind::File,
                10,
                start_today - 2 * DAY_MS,
            ),
            DiscoveredItem::new(
                PathBuf::from("X:/facets/private/hidden.pdf"),
                ItemKind::File,
                10,
                start_today + 1_000,
            ),
            ignored,
        ],
    )
    .unwrap();
    core.exclude_recommendation_path("X:/facets/private".into())
        .unwrap();

    let response = core
        .facet_counts(
            &FacetCountsRequest {
                root_ids: vec![root.id],
                now_unix_ms: NOW,
                timezone_offset_minutes: 0,
                ..FacetCountsRequest::default()
            },
            &CancellationToken::new(),
        )
        .unwrap();
    assert_eq!(response.matched_total, 6);
    assert_eq!(response.considered_candidates, 8);
    let category_counts: std::collections::HashMap<_, _> = response
        .categories
        .iter()
        .map(|row| (row.category, row.count))
        .collect();
    for category in [
        FileFacetCategory::Folder,
        FileFacetCategory::Document,
        FileFacetCategory::Picture,
        FileFacetCategory::Video,
        FileFacetCategory::Audio,
        FileFacetCategory::Other,
    ] {
        assert_eq!(category_counts[&category], 1, "{category:?} count drifted");
    }
    let time_counts: std::collections::HashMap<_, _> = response
        .modified_time
        .iter()
        .map(|row| (row.range, row.count))
        .collect();
    assert_eq!(time_counts[&ModifiedTimeFacet::Last1Day], 1);
    assert_eq!(time_counts[&ModifiedTimeFacet::Last7Days], 3);
    assert_eq!(time_counts[&ModifiedTimeFacet::Last30Days], 4);
    assert_eq!(time_counts[&ModifiedTimeFacet::Last365Days], 5);
    assert_eq!(time_counts[&ModifiedTimeFacet::Older], 1);

    let cross_filtered = core
        .facet_counts(
            &FacetCountsRequest {
                root_ids: vec![root.id],
                categories: vec![FileFacetCategory::Document, FileFacetCategory::Picture],
                modified_time: Some(ModifiedTimeFacet::Last7Days),
                now_unix_ms: NOW,
                timezone_offset_minutes: 0,
                ..FacetCountsRequest::default()
            },
            &CancellationToken::new(),
        )
        .unwrap();
    assert_eq!(cross_filtered.matched_total, 1);
    let category_counts: std::collections::HashMap<_, _> = cross_filtered
        .categories
        .iter()
        .map(|row| (row.category, row.count))
        .collect();
    assert_eq!(category_counts[&FileFacetCategory::Folder], 1);
    assert_eq!(category_counts[&FileFacetCategory::Document], 1);
    assert_eq!(category_counts[&FileFacetCategory::Picture], 0);
    assert_eq!(category_counts[&FileFacetCategory::Other], 1);
    let time_counts: std::collections::HashMap<_, _> = cross_filtered
        .modified_time
        .iter()
        .map(|row| (row.range, row.count))
        .collect();
    assert_eq!(time_counts[&ModifiedTimeFacet::Last1Day], 0);
    assert_eq!(time_counts[&ModifiedTimeFacet::Last7Days], 1);
    assert_eq!(time_counts[&ModifiedTimeFacet::Last30Days], 2);
    assert_eq!(time_counts[&ModifiedTimeFacet::Last365Days], 2);
    assert_eq!(time_counts[&ModifiedTimeFacet::Older], 0);

    let zero_query_audio = core
        .search(
            &SearchRequest {
                query: String::new(),
                root_ids: vec![root.id],
                categories: vec![FileFacetCategory::Audio],
                now_unix_ms: NOW,
                timezone_offset_minutes: 0,
                ..SearchRequest::default()
            },
            &CancellationToken::new(),
        )
        .unwrap();
    assert_eq!(zero_query_audio.results.len(), 1);
    assert_eq!(zero_query_audio.results[0].item.name, "track.flac");
    assert!(zero_query_audio.considered_candidates > 0);

    let mut docs = FilterPreset::new("docs", "PDFs");
    docs.target = FilterTarget::Files;
    docs.search_paths = vec!["X:/facets".into()];
    docs.extensions = vec!["pdf".into()];
    core.put_filter(docs).unwrap();
    let scoped = core
        .facet_counts(
            &FacetCountsRequest {
                query: "docs:".into(),
                root_ids: vec![root.id],
                now_unix_ms: NOW,
                timezone_offset_minutes: 0,
                ..FacetCountsRequest::default()
            },
            &CancellationToken::new(),
        )
        .unwrap();
    assert_eq!(scoped.matched_total, 1);
    assert_eq!(
        scoped
            .categories
            .iter()
            .find(|row| row.category == FileFacetCategory::Document)
            .unwrap()
            .count,
        1
    );
}

#[test]
fn facet_counts_are_exact_past_search_budget_and_cancellable() {
    const ITEM_COUNT: usize = 5_001;
    let state = TempDir::new().unwrap();
    let core = open_core(state.path());
    let root = core
        .add_root(PathBuf::from("X:/facet-scale"), RootKind::Local)
        .unwrap();
    core.replace_root_snapshot(
        root.id,
        (0..ITEM_COUNT)
            .map(|index| {
                DiscoveredItem::new(
                    PathBuf::from(format!("X:/facet-scale/common_{index:05}.pdf")),
                    ItemKind::File,
                    100,
                    1_800_000_000_000,
                )
            })
            .collect(),
    )
    .unwrap();

    let request = FacetCountsRequest {
        query: "common".into(),
        root_ids: vec![root.id],
        now_unix_ms: 1_800_057_600_000,
        ..FacetCountsRequest::default()
    };
    let cancelled = CancellationToken::new();
    cancelled.cancel();
    assert!(matches!(
        core.facet_counts(&request, &cancelled),
        Err(CoreError::Cancelled)
    ));

    let response = core
        .facet_counts(&request, &CancellationToken::new())
        .unwrap();
    assert_eq!(response.considered_candidates, ITEM_COUNT as u64);
    assert_eq!(response.matched_total, ITEM_COUNT as u64);
    assert_eq!(
        response
            .categories
            .iter()
            .find(|row| row.category == FileFacetCategory::Document)
            .unwrap()
            .count,
        ITEM_COUNT as u64
    );

    let bounded_search = core
        .search(
            &SearchRequest {
                query: "common".into(),
                limit: 10,
                candidate_limit: 37,
                root_ids: vec![root.id],
                categories: vec![FileFacetCategory::Document],
                ..SearchRequest::default()
            },
            &CancellationToken::new(),
        )
        .unwrap();
    assert_eq!(bounded_search.considered_candidates, 37);
    assert_eq!(bounded_search.results.len(), 10);
}

#[test]
fn global_ranking_keeps_text_quality_above_history_but_context_can_dominate() {
    let state = TempDir::new().unwrap();
    let core = open_core(state.path());
    let root = core
        .add_root(PathBuf::from("X:/synthetic"), RootKind::Local)
        .unwrap();
    core.replace_root_snapshot(
        root.id,
        vec![
            DiscoveredItem::new(
                PathBuf::from("X:/synthetic/elsewhere/snowfall.mp4"),
                ItemKind::File,
                100,
                100,
            ),
            DiscoveredItem::new(
                PathBuf::from("X:/synthetic/current/s-n-o-w-f-a-l-l.mp4"),
                ItemKind::File,
                100,
                100,
            ),
        ],
    )
    .unwrap();

    let exact = search(&core, "snowfall");
    assert_eq!(exact.results[0].item.name, "snowfall.mp4");
    let fuzzy_id = exact
        .results
        .iter()
        .find(|r| r.item.name.starts_with("s-n-o"))
        .unwrap()
        .item
        .id;
    for i in 0..50 {
        core.record_activation(fuzzy_id, 1_800_000_000_000 + i)
            .unwrap();
    }
    let after_history = search(&core, "snowfall");
    assert_eq!(after_history.results[0].item.name, "snowfall.mp4");

    let contextual = SearchRequest {
        query: "snowfall".into(),
        context: SearchContext {
            current_folder: Some("X:/synthetic/current".into()),
            contextual: true,
        },
        ..SearchRequest::default()
    };
    let result = core.search(&contextual, &CancellationToken::new()).unwrap();
    assert!(result.results[0].item.name.starts_with("s-n-o"));
    assert!(result.results[0].score.context_boost > 0);
}

#[test]
fn recommendations_hide_and_search_exclusion_apply_to_existing_history() {
    let state = TempDir::new().unwrap();
    let core = open_core(state.path());
    let root = core
        .add_root(PathBuf::from("X:/history"), RootKind::Local)
        .unwrap();
    core.replace_root_snapshot(
        root.id,
        vec![
            DiscoveredItem::new(
                PathBuf::from("X:/history/private/secret.mov"),
                ItemKind::File,
                100,
                100,
            ),
            DiscoveredItem::new(
                PathBuf::from("X:/history/public/edit.mov"),
                ItemKind::File,
                100,
                100,
            ),
            DiscoveredItem::new(
                PathBuf::from("X:/history/privateer/allowed.mov"),
                ItemKind::File,
                100,
                100,
            ),
        ],
    )
    .unwrap();
    let secret = search(&core, "secret").results[0].item.id;
    let public = search(&core, "edit").results[0].item.id;
    core.record_activation(secret, 1_800_000_000_000).unwrap();
    core.record_activation(public, 1_800_000_000_100).unwrap();
    assert_eq!(core.recommendations(1_800_000_000_200, 10).len(), 2);

    core.hide_recommendation(public).unwrap();
    assert_eq!(core.recommendations(1_800_000_000_300, 10).len(), 1);

    core.exclude_recommendation_path("X:/history/private".into())
        .unwrap();
    assert!(core.recommendations(1_800_000_000_400, 10).is_empty());
    assert!(search(&core, "secret").results.is_empty());
    assert_eq!(search(&core, "allowed").results.len(), 1);
}

#[test]
fn root_configuration_fails_closed_for_duplicates_and_invalid_schedules() {
    let state = TempDir::new().unwrap();
    let core = open_core(state.path());
    let root = core
        .add_root(PathBuf::from("X:/configured-root"), RootKind::Local)
        .unwrap();

    let duplicate = core.add_root(PathBuf::from("x:\\configured-root"), RootKind::Local);
    assert!(matches!(duplicate, Err(CoreError::InvalidRequest(_))));

    let mut invalid = root.clone();
    invalid.refresh_policy = RefreshPolicy::Interval { seconds: 0 };
    assert!(matches!(
        core.put_root(invalid),
        Err(CoreError::InvalidRequest(_))
    ));

    let mut valid = root;
    valid.refresh_policy = RefreshPolicy::Weekly {
        weekday_from_monday: 2,
        hour: 4,
        minute: 30,
    };
    core.put_root(valid).unwrap();
}

#[test]
fn root_updates_keep_runtime_health_core_owned_and_disable_every_search_surface() {
    let state = TempDir::new().unwrap();
    let core = open_core(state.path());
    let root = core
        .add_root(PathBuf::from("X:/coherent-root"), RootKind::Local)
        .unwrap();
    core.replace_root_snapshot(
        root.id,
        vec![DiscoveredItem::new(
            PathBuf::from("X:/coherent-root/coherent-scene.mov"),
            ItemKind::File,
            500,
            100,
        )],
    )
    .unwrap();
    let item_id = search(&core, "coherent-scene").results[0].item.id;
    core.record_activation(item_id, 1_800_000_000_000).unwrap();

    let mut update = root.clone();
    update.priority = Priority::High;
    update.availability = RootAvailability::Unavailable;
    update.last_error = Some("caller must not own this".into());
    update.last_reconciled_unix_ms = Some(123);
    core.put_root(update).unwrap();

    let stored = core
        .list_roots()
        .into_iter()
        .find(|candidate| candidate.id == root.id)
        .unwrap();
    assert_eq!(stored.availability, RootAvailability::Available);
    assert_eq!(stored.last_error, None);
    assert_eq!(stored.last_reconciled_unix_ms, None);
    assert_eq!(
        search(&core, "coherent-scene").results[0].item.priority,
        Priority::High
    );

    let mut disabled = stored.clone();
    disabled.enabled = false;
    core.put_root(disabled).unwrap();
    assert!(search(&core, "coherent-scene").results.is_empty());
    assert!(core.recommendations(1_800_000_000_100, 10).is_empty());
    assert_eq!(
        core.facet_counts(
            &FacetCountsRequest {
                query: "coherent-scene".into(),
                ..FacetCountsRequest::default()
            },
            &CancellationToken::new(),
        )
        .unwrap()
        .matched_total,
        0
    );
    assert_eq!(
        core.file_search(
            &FileSearchRequest {
                query: "coherent-scene".into(),
                ..FileSearchRequest::default()
            },
            &CancellationToken::new(),
        )
        .unwrap()
        .total_matches,
        0
    );

    let mut enabled = core
        .list_roots()
        .into_iter()
        .find(|candidate| candidate.id == root.id)
        .unwrap();
    enabled.enabled = true;
    core.put_root(enabled).unwrap();
    assert_eq!(search(&core, "coherent-scene").results.len(), 1);

    let mut illegal_path_change = core
        .list_roots()
        .into_iter()
        .find(|candidate| candidate.id == root.id)
        .unwrap();
    illegal_path_change.path = PathBuf::from("X:/different-root");
    assert!(matches!(
        core.put_root(illegal_path_change),
        Err(CoreError::InvalidRequest(_))
    ));
    assert_eq!(
        core.list_roots()
            .into_iter()
            .find(|candidate| candidate.id == root.id)
            .unwrap()
            .path,
        PathBuf::from("X:/coherent-root")
    );
}

#[test]
fn root_and_dirty_path_inputs_reject_relative_and_dot_traversal_paths() {
    let state = TempDir::new().unwrap();
    let core = open_core(state.path());

    assert!(matches!(
        core.add_root(PathBuf::from("relative/root"), RootKind::Local),
        Err(CoreError::InvalidRequest(_))
    ));
    assert!(matches!(
        core.add_root(PathBuf::from("X:/safe/../escape"), RootKind::Local),
        Err(CoreError::InvalidRequest(_))
    ));
    let posix = core
        .add_root(PathBuf::from("/Volumes/Listary-Test"), RootKind::Local)
        .unwrap();
    assert_eq!(posix.path, PathBuf::from("/Volumes/Listary-Test"));

    let root = core
        .add_root(PathBuf::from("X:/watch-root"), RootKind::Local)
        .unwrap();
    for invalid in [
        PathBuf::from("X:/watch-root/../escape"),
        PathBuf::from("X:/outside-root"),
        PathBuf::from("relative/dirty-path"),
    ] {
        assert!(matches!(
            core.reconcile_paths(root.id, vec![invalid], &CancellationToken::new()),
            Err(CoreError::InvalidRequest(_))
        ));
    }
}

#[test]
fn unavailable_root_keeps_records_and_can_be_filtered() {
    let state = TempDir::new().unwrap();
    let core = open_core(state.path());
    let root = core
        .add_root(PathBuf::from("Z:/offline-share"), RootKind::Network)
        .unwrap();
    core.replace_root_snapshot(
        root.id,
        vec![DiscoveredItem::new(
            PathBuf::from("Z:/offline-share/reference.pdf"),
            ItemKind::File,
            500,
            100,
        )],
    )
    .unwrap();
    core.set_root_unavailable(root.id, "network disconnected".into())
        .unwrap();

    let visible = search(&core, "reference");
    assert_eq!(visible.results.len(), 1);
    assert!(!visible.results[0].item.available);

    let request = SearchRequest {
        query: "reference".into(),
        include_unavailable: false,
        ..SearchRequest::default()
    };
    assert!(
        core.search(&request, &CancellationToken::new())
            .unwrap()
            .results
            .is_empty()
    );
}

#[test]
fn repeated_unavailable_available_root_cycles_preserve_catalog_and_stable_ids() {
    let state = TempDir::new().unwrap();
    let volume = TempDir::new().unwrap();
    let mounted = volume.path().join("mounted");
    let detached = volume.path().join("detached");
    write(mounted.join("reference.pdf"), b"reference");

    let core = open_core(state.path());
    let root = core.add_root(mounted.clone(), RootKind::Network).unwrap();
    core.reconcile_root(root.id, &CancellationToken::new())
        .unwrap();
    let stable_id = search(&core, "reference").results[0].item.id;

    for cycle in 0..6 {
        fs::rename(&mounted, &detached).unwrap();
        let error = core
            .reconcile_root(root.id, &CancellationToken::new())
            .unwrap_err();
        assert!(matches!(error, CoreError::Filesystem { .. }));

        let offline = search(&core, "reference");
        assert_eq!(offline.results.len(), 1, "offline cycle {cycle}");
        assert_eq!(offline.results[0].item.id, stable_id);
        assert!(!offline.results[0].item.available);
        let hidden_request = SearchRequest {
            query: "reference".into(),
            include_unavailable: false,
            ..SearchRequest::default()
        };
        assert!(
            core.search(&hidden_request, &CancellationToken::new())
                .unwrap()
                .results
                .is_empty()
        );
        let offline_diagnostics = core.diagnostics().unwrap();
        assert_eq!(offline_diagnostics.unavailable_root_count, 1);
        assert_eq!(offline_diagnostics.item_count, 1);

        fs::rename(&detached, &mounted).unwrap();
        core.reconcile_root(root.id, &CancellationToken::new())
            .unwrap();
        let online = search(&core, "reference");
        assert_eq!(online.results.len(), 1, "online cycle {cycle}");
        assert_eq!(online.results[0].item.id, stable_id);
        assert!(online.results[0].item.available);
        let online_diagnostics = core.diagnostics().unwrap();
        assert_eq!(online_diagnostics.available_root_count, 1);
        assert_eq!(online_diagnostics.unavailable_root_count, 0);
        assert_eq!(online_diagnostics.item_count, 1);
    }

    core.flush().unwrap();
    drop(core);
    let reopened = open_core(state.path());
    let result = search(&reopened, "reference");
    assert_eq!(result.results.len(), 1);
    assert_eq!(result.results[0].item.id, stable_id);
    assert!(result.results[0].item.available);
}

#[test]
fn cancelled_reconcile_does_not_delete_previous_snapshot() {
    let state = TempDir::new().unwrap();
    let files = TempDir::new().unwrap();
    write(files.path().join("keep.txt"), b"keep");
    let core = open_core(state.path());
    let root = core
        .add_root(files.path().to_path_buf(), RootKind::Local)
        .unwrap();
    core.reconcile_root(root.id, &CancellationToken::new())
        .unwrap();

    fs::remove_file(files.path().join("keep.txt")).unwrap();
    write(files.path().join("new.txt"), b"new");
    let token = CancellationToken::new();
    token.cancel();
    let error = core.reconcile_root(root.id, &token).unwrap_err();
    assert!(matches!(error, CoreError::Cancelled));
    assert_eq!(search(&core, "keep").results.len(), 1);
    assert!(search(&core, "new").results.is_empty());
}

#[test]
fn repeated_cancel_then_restart_keeps_last_committed_snapshot() {
    let state = TempDir::new().unwrap();
    let files = TempDir::new().unwrap();
    write(files.path().join("committed.txt"), b"committed");

    let mut core = open_core(state.path());
    let root = core
        .add_root(files.path().to_path_buf(), RootKind::Local)
        .unwrap();
    core.reconcile_root(root.id, &CancellationToken::new())
        .unwrap();
    let committed_id = search(&core, "committed").results[0].item.id;

    for cycle in 0..5 {
        let pending_name = format!("pending-{cycle}.txt");
        write(files.path().join(&pending_name), pending_name.as_bytes());

        let token = CancellationToken::new();
        token.cancel();
        let error = core.reconcile_root(root.id, &token).unwrap_err();
        assert!(matches!(error, CoreError::Cancelled));
        assert_eq!(search(&core, "committed").results[0].item.id, committed_id);
        assert!(search(&core, &pending_name).results.is_empty());

        drop(core);
        core = open_core(state.path());
        assert!(
            core.list_roots()
                .iter()
                .any(|candidate| candidate.id == root.id)
        );
        assert_eq!(search(&core, "committed").results[0].item.id, committed_id);
        assert!(search(&core, &pending_name).results.is_empty());

        core.reconcile_root(root.id, &CancellationToken::new())
            .unwrap();
        assert_eq!(search(&core, &pending_name).results.len(), 1);
    }
}

#[test]
fn concurrent_search_can_be_cancelled() {
    let state = TempDir::new().unwrap();
    let core = Arc::new(open_core(state.path()));
    let root = core
        .add_root(PathBuf::from("X:/many"), RootKind::Local)
        .unwrap();
    let items = (0..30_000)
        .map(|i| {
            DiscoveredItem::new(
                PathBuf::from(format!(
                    "X:/many/folder_{:03}/scene_{i:05}_snowfall_edit.mov",
                    i % 200
                )),
                ItemKind::File,
                i as u64,
                i as i64,
            )
        })
        .collect();
    core.replace_root_snapshot(root.id, items).unwrap();

    let token = CancellationToken::new();
    let search_token = token.clone();
    let search_core = core.clone();
    let thread = thread::spawn(move || {
        let request = SearchRequest {
            query: "snwflldt".into(),
            candidate_limit: 30_000,
            ..SearchRequest::default()
        };
        search_core.search(&request, &search_token)
    });
    thread::sleep(Duration::from_millis(1));
    token.cancel();
    let result = thread.join().unwrap();
    assert!(matches!(result, Err(CoreError::Cancelled)) || result.is_ok());
    // Search may legitimately finish before the cancellation races in, but cancellation
    // must never corrupt the index or make the next query fail.
    assert!(!search(&core, "scene_00010").results.is_empty());
}

#[test]
fn concurrent_search_updates_and_history_activity_remain_consistent() {
    let state = TempDir::new().unwrap();
    let core = Arc::new(open_core(state.path()));
    let root = core
        .add_root(PathBuf::from("X:/concurrent"), RootKind::Local)
        .unwrap();
    core.replace_root_snapshot(
        root.id,
        vec![DiscoveredItem::new(
            PathBuf::from("X:/concurrent/anchor.mov"),
            ItemKind::File,
            1,
            100,
        )],
    )
    .unwrap();
    let anchor_id = search(&core, "anchor").results[0].item.id;

    let barrier = Arc::new(Barrier::new(3));
    let writer_core = core.clone();
    let writer_barrier = barrier.clone();
    let writer = thread::spawn(move || {
        writer_barrier.wait();
        for i in 0..128 {
            writer_core
                .upsert_items(
                    root.id,
                    vec![DiscoveredItem::new(
                        PathBuf::from(format!("X:/concurrent/live_item_{i:03}.mov")),
                        ItemKind::File,
                        i as u64,
                        1_800_000_000_000 + i as i64,
                    )],
                )
                .unwrap();
            if i % 8 == 0 {
                thread::yield_now();
            }
        }
    });

    let history_core = core.clone();
    let history_barrier = barrier.clone();
    let history = thread::spawn(move || {
        history_barrier.wait();
        for i in 0..200 {
            history_core
                .record_activation(anchor_id, 1_800_000_100_000 + i)
                .unwrap();
            if i % 8 == 0 {
                thread::yield_now();
            }
        }
    });

    let search_core = core.clone();
    let search_barrier = barrier.clone();
    let searcher = thread::spawn(move || {
        search_barrier.wait();
        let request = SearchRequest {
            query: "live_item".into(),
            limit: 32,
            candidate_limit: 512,
            ..SearchRequest::default()
        };
        for i in 0..300 {
            let response = search_core
                .search(&request, &CancellationToken::new())
                .unwrap();
            assert!(response.results.len() <= 32);
            if i % 8 == 0 {
                thread::yield_now();
            }
        }
    });

    writer.join().unwrap();
    history.join().unwrap();
    searcher.join().unwrap();

    assert_eq!(core.diagnostics().unwrap().item_count, 129);
    assert_eq!(search(&core, "live_item_127").results.len(), 1);
    let recommendation = core
        .recommendations(1_800_000_200_000, 10)
        .into_iter()
        .find(|candidate| candidate.item.id == anchor_id)
        .unwrap();
    assert_eq!(recommendation.activation_count, 200);
}

#[test]
fn missing_or_corrupt_index_recovers_without_losing_history() {
    let state = TempDir::new().unwrap();
    let files = TempDir::new().unwrap();
    write(files.path().join("recover-me.txt"), b"x");
    let root_id;
    {
        let core = open_core(state.path());
        let root = core
            .add_root(files.path().to_path_buf(), RootKind::Local)
            .unwrap();
        root_id = root.id;
        core.reconcile_root(root.id, &CancellationToken::new())
            .unwrap();
        let id = search(&core, "recover-me").results[0].item.id;
        core.record_activation(id, 1_800_000_000_000).unwrap();
        core.flush().unwrap();
    }

    // Break CURRENT so RocksDB must repair or fall back to a fresh index.
    fs::write(
        state.path().join("index/CURRENT"),
        b"MANIFEST-does-not-exist\n",
    )
    .unwrap();
    let recovered = open_core(state.path());
    let diagnostics = recovered.diagnostics().unwrap();
    assert_eq!(diagnostics.history_entries, 1);
    assert!(
        diagnostics.recovery_count >= 1,
        "successful physical catalog repair/fallback must be visible in diagnostics"
    );
    if !recovered.list_roots().iter().any(|r| r.id == root_id) {
        let root = recovered
            .add_root(files.path().to_path_buf(), RootKind::Local)
            .unwrap();
        recovered
            .reconcile_root(root.id, &CancellationToken::new())
            .unwrap();
    } else {
        recovered
            .reconcile_root(root_id, &CancellationToken::new())
            .unwrap();
    }
    assert_eq!(search(&recovered, "recover-me").results.len(), 1);
}

#[test]
fn fresh_catalog_after_unrecoverable_index_never_reuses_stale_history_ids() {
    let state = TempDir::new().unwrap();
    let files = TempDir::new().unwrap();
    write(files.path().join("historical.txt"), b"history");
    let historical_id;
    {
        let core = open_core(state.path());
        let root = core
            .add_root(files.path().to_path_buf(), RootKind::Local)
            .unwrap();
        core.reconcile_root(root.id, &CancellationToken::new())
            .unwrap();
        historical_id = search(&core, "historical").results[0].item.id;
        core.record_activation(historical_id, 1_800_000_000_000)
            .unwrap();
        core.flush().unwrap();
    }

    let index = state.path().join("index");
    fs::remove_dir_all(&index).unwrap();
    fs::write(&index, b"not-a-rocksdb-directory").unwrap();
    write(files.path().join("new-catalog-item.txt"), b"new");

    let recovered = open_core(state.path());
    assert_eq!(recovered.diagnostics().unwrap().history_entries, 1);
    let root = recovered
        .list_roots()
        .into_iter()
        .find(|root| root.path == files.path())
        .unwrap_or_else(|| {
            recovered
                .add_root(files.path().to_path_buf(), RootKind::Local)
                .unwrap()
        });
    recovered
        .reconcile_root(root.id, &CancellationToken::new())
        .unwrap();

    let new_id = search(&recovered, "new-catalog-item").results[0].item.id;
    assert_ne!(new_id, historical_id);
    assert!(recovered.recommendations(1_800_000_000_100, 10).is_empty());
}

#[test]
fn repeated_runtime_writes_keep_logs_cache_and_temp_artifacts_bounded() {
    let state = TempDir::new().unwrap();
    let mut config = CoreConfig::new(state.path().to_path_buf());
    config.log_max_bytes = 1_024;
    config.log_generations = 2;
    let core = Core::open(config).unwrap();

    let root = core
        .add_root(PathBuf::from("X:/retention"), RootKind::Local)
        .unwrap();
    core.replace_root_snapshot(
        root.id,
        vec![DiscoveredItem::new(
            PathBuf::from("X:/retention/anchor.txt"),
            ItemKind::File,
            1,
            100,
        )],
    )
    .unwrap();
    let anchor_id = search(&core, "anchor").results[0].item.id;
    core.record_activation(anchor_id, 1_800_000_000_000)
        .unwrap();
    core.flush().unwrap();

    // Simulate crash-left temporary files. The next normal write must reuse and consume
    // these fixed temp paths rather than accumulating unique artifacts.
    fs::write(
        state.path().join("cache/search-cache.bin.tmp"),
        b"stale-cache-temp",
    )
    .unwrap();
    fs::write(state.path().join("history.json.tmp"), b"stale-history-temp").unwrap();

    for i in 0..40 {
        core.record_activation(anchor_id, 1_800_000_001_000 + i)
            .unwrap();
        core.flush().unwrap();
    }

    // Root-add events exercise the configured application-log rotation through Core.
    for i in 0..180 {
        core.add_root(
            PathBuf::from(format!("X:/retention-log-root-{i:03}")),
            RootKind::Local,
        )
        .unwrap();
    }
    drop(core);

    let mut log_files: Vec<_> = fs::read_dir(state.path().join("logs"))
        .unwrap()
        .filter_map(|entry| entry.ok())
        .filter(|entry| entry.file_name().to_string_lossy().starts_with("core.log"))
        .collect();
    log_files.sort_by_key(|entry| entry.file_name());
    assert!(log_files.len() <= 3, "log files: {log_files:?}");

    let cache_files: Vec<_> = fs::read_dir(state.path().join("cache"))
        .unwrap()
        .filter_map(|entry| entry.ok())
        .map(|entry| entry.file_name().to_string_lossy().into_owned())
        .collect();
    assert_eq!(cache_files, vec!["search-cache.bin"]);
    assert!(!state.path().join("history.json.tmp").exists());
    let history_files = fs::read_dir(state.path())
        .unwrap()
        .filter_map(|entry| entry.ok())
        .filter(|entry| {
            entry
                .file_name()
                .to_string_lossy()
                .starts_with("history.json")
        })
        .count();
    assert!(history_files <= 2);
}

#[test]
fn repeated_fallback_recovery_keeps_corrupt_index_artifacts_bounded() {
    let state = TempDir::new().unwrap();
    {
        let core = open_core(state.path());
        core.flush().unwrap();
    }

    for _ in 0..5 {
        let index = state.path().join("index");
        if index.is_dir() {
            fs::remove_dir_all(&index).unwrap();
        } else if index.exists() {
            fs::remove_file(&index).unwrap();
        }
        fs::write(&index, b"not-a-rocksdb-directory").unwrap();

        {
            let recovered = open_core(state.path());
            assert!(index.is_dir());
            assert!(recovered.diagnostics().unwrap().recovery_count >= 1);
        }
        thread::sleep(Duration::from_millis(2));
    }

    let recovery_artifacts: Vec<_> = fs::read_dir(state.path())
        .unwrap()
        .filter_map(|entry| entry.ok())
        .filter(|entry| {
            entry
                .file_name()
                .to_string_lossy()
                .starts_with("index.corrupt.")
        })
        .collect();
    assert!(
        recovery_artifacts.len() <= 2,
        "recovery artifacts: {recovery_artifacts:?}"
    );
}

#[test]
fn large_catalog_search_is_bounded_and_deterministic() {
    let state = TempDir::new().unwrap();
    let core = open_core(state.path());
    let root = core
        .add_root(PathBuf::from("X:/large"), RootKind::Local)
        .unwrap();
    let items = (0..25_000)
        .map(|i| {
            DiscoveredItem::new(
                PathBuf::from(format!(
                    "X:/large/project_{:03}/shot_{i:05}_final_render.mov",
                    i % 250
                )),
                ItemKind::File,
                i as u64,
                i as i64,
            )
        })
        .collect();
    core.replace_root_snapshot(root.id, items).unwrap();
    let request = SearchRequest {
        query: "shot_24999".into(),
        limit: 20,
        candidate_limit: 2_000,
        ..SearchRequest::default()
    };
    let first = core.search(&request, &CancellationToken::new()).unwrap();
    let second = core.search(&request, &CancellationToken::new()).unwrap();
    assert_eq!(first.results.len(), 1);
    assert_eq!(first.results[0].item.name, "shot_24999_final_render.mov");
    assert!(first.considered_candidates <= 2_000);
    assert_eq!(
        first.results.iter().map(|r| r.item.id).collect::<Vec<_>>(),
        second.results.iter().map(|r| r.item.id).collect::<Vec<_>>()
    );
}

#[test]
fn specific_late_id_survives_tiny_interactive_candidate_budget() {
    let state = TempDir::new().unwrap();
    let core = open_core(state.path());
    let root = core
        .add_root(PathBuf::from("X:/specific-late"), RootKind::Local)
        .unwrap();
    let items = (0..20_000)
        .map(|i| {
            DiscoveredItem::new(
                PathBuf::from(format!("X:/specific-late/scene_{i:07}_snowfall_final.mov")),
                ItemKind::File,
                i as u64,
                i as i64,
            )
        })
        .collect();
    core.replace_root_snapshot(root.id, items).unwrap();
    let request = SearchRequest {
        query: "scene_0019999".into(),
        limit: 20,
        candidate_limit: 64,
        ..SearchRequest::default()
    };
    let result = core.search(&request, &CancellationToken::new()).unwrap();
    assert_eq!(
        result.results[0].item.name,
        "scene_0019999_snowfall_final.mov"
    );
    assert!(result.considered_candidates <= 64);
}

#[test]
fn late_history_and_context_candidates_are_prioritized_before_budget_cutoff() {
    let state = TempDir::new().unwrap();
    let core = open_core(state.path());
    let root = core
        .add_root(PathBuf::from("X:/priority-lanes"), RootKind::Local)
        .unwrap();
    let items = (0..20_000)
        .map(|i| {
            let folder = if i >= 19_900 { "current" } else { "other" };
            DiscoveredItem::new(
                PathBuf::from(format!("X:/priority-lanes/{folder}/scene_{i:07}_edit.mov")),
                ItemKind::File,
                i as u64,
                i as i64,
            )
        })
        .collect();
    core.replace_root_snapshot(root.id, items).unwrap();

    let late = search(&core, "scene_0019999").results[0].item.id;
    for i in 0..25 {
        core.record_activation(late, 1_800_000_000_000 + i).unwrap();
    }
    let history_request = SearchRequest {
        query: "scene".into(),
        limit: 10,
        candidate_limit: 64,
        ..SearchRequest::default()
    };
    let history_result = core
        .search(&history_request, &CancellationToken::new())
        .unwrap();
    assert_eq!(history_result.results[0].item.id, late);

    let context_request = SearchRequest {
        query: "scene".into(),
        limit: 10,
        candidate_limit: 64,
        context: SearchContext {
            current_folder: Some("X:/priority-lanes/current".into()),
            contextual: true,
        },
        ..SearchRequest::default()
    };
    let context_result = core
        .search(&context_request, &CancellationToken::new())
        .unwrap();
    assert_eq!(
        context_result.results[0].item.parent_path,
        "X:/priority-lanes/current"
    );
}

#[test]
fn size_filter_is_files_only_and_ignored_priority_is_hard_excluded() {
    let state = TempDir::new().unwrap();
    let core = open_core(state.path());
    let root = core
        .add_root(PathBuf::from("X:/filter"), RootKind::Local)
        .unwrap();
    let mut ignored = DiscoveredItem::new(
        PathBuf::from("X:/filter/ignored.mp4"),
        ItemKind::File,
        50 * 1024 * 1024,
        100,
    );
    ignored.priority = Priority::Ignored;
    core.replace_root_snapshot(
        root.id,
        vec![
            ignored,
            DiscoveredItem::new(
                PathBuf::from("X:/filter/normal.mp4"),
                ItemKind::File,
                15 * 1024 * 1024,
                100,
            ),
            DiscoveredItem::new(PathBuf::from("X:/filter/folder"), ItemKind::Folder, 0, 100),
        ],
    )
    .unwrap();
    let filtered = search(&core, "size:10");
    assert_eq!(filtered.results.len(), 1);
    assert_eq!(filtered.results[0].item.name, "normal.mp4");
    assert!(search(&core, "ignored").results.is_empty());
}

#[test]
fn same_generation_foreign_cache_never_replaces_catalog_authority() {
    let donor_state = TempDir::new().unwrap();
    let target_state = TempDir::new().unwrap();
    let mut fixtures = Vec::new();
    for (state, name) in [(&donor_state, "alpha"), (&target_state, "beta")] {
        let core = open_core(state.path());
        let root = core
            .add_root(state.path().join("files"), RootKind::Local)
            .unwrap();
        core.replace_root_snapshot(
            root.id,
            vec![DiscoveredItem::new(
                root.path.join(format!("{name}.txt")),
                ItemKind::File,
                10,
                100,
            )],
        )
        .unwrap();
        let item = search(&core, name).results[0].item.clone();
        core.record_activation(item.id, 1_800_000_000_000).unwrap();
        fixtures.push((core.catalog_generation().unwrap(), root, item));
        core.flush().unwrap();
    }
    assert_eq!(fixtures[0].0, fixtures[1].0);
    assert_eq!(fixtures[0].2.id, fixtures[1].2.id);
    let target_history = fs::read(target_state.path().join("history.json")).unwrap();
    fs::copy(
        donor_state.path().join("cache/search-cache.bin"),
        target_state.path().join("cache/search-cache.bin"),
    )
    .unwrap();

    {
        let (core, metrics) =
            Core::open_profiled(CoreConfig::new(target_state.path().to_path_buf())).unwrap();
        assert_ne!(
            metrics.cache_source,
            Some(StartupCacheSource::Snapshot),
            "a checksum-valid foreign cache with an equal generation must be rejected"
        );
        assert_eq!(metrics.rocksdb_items_loaded, 1);
        assert_eq!(core.catalog_generation().unwrap(), fixtures[1].0);
        assert_eq!(core.list_roots(), vec![fixtures[1].1.clone()]);
        assert_eq!(
            json!(search(&core, "beta").results[0].item),
            json!(fixtures[1].2)
        );
        assert!(search(&core, "alpha").results.is_empty());
        assert_eq!(
            json!(core.get_item(fixtures[1].2.id).unwrap()),
            json!(fixtures[1].2)
        );
        assert!(core.get_item_by_path(&fixtures[0].2.path).is_none());
        assert_eq!(core.diagnostics().unwrap().history_entries, 1);
        core.flush().unwrap();
    }
    assert_eq!(
        fs::read(target_state.path().join("history.json")).unwrap(),
        target_history
    );
    let (core, metrics) =
        Core::open_profiled(CoreConfig::new(target_state.path().to_path_buf())).unwrap();
    assert_eq!(metrics.cache_source, Some(StartupCacheSource::Snapshot));
    assert_eq!(metrics.rocksdb_items_loaded, 0);
    assert_eq!(
        json!(search(&core, "beta").results[0].item),
        json!(fixtures[1].2)
    );
}

#[test]
fn derived_cache_snapshot_can_be_missing_or_corrupt_without_losing_catalog() {
    let state = TempDir::new().unwrap();
    let root_id;
    {
        let core = open_core(state.path());
        let root = core
            .add_root(PathBuf::from("X:/snapshot-recovery"), RootKind::Local)
            .unwrap();
        root_id = root.id;
        core.replace_root_snapshot(
            root.id,
            vec![DiscoveredItem::new(
                PathBuf::from("X:/snapshot-recovery/authority.txt"),
                ItemKind::File,
                10,
                100,
            )],
        )
        .unwrap();
        core.flush().unwrap();
    }

    let snapshot = state.path().join("cache/search-cache.bin");
    assert!(snapshot.exists());
    let baseline = fs::read(&snapshot).unwrap();
    assert!(
        baseline.len() > 48,
        "fixture snapshot should contain a payload"
    );

    let mut invalid_variants = vec![
        Vec::new(),
        b"not-a-valid-derived-cache".to_vec(),
        baseline[..12].to_vec(),
        baseline[..24].to_vec(),
    ];
    let mut wrong_schema = baseline.clone();
    wrong_schema[8..12].copy_from_slice(&2u32.to_le_bytes());
    invalid_variants.push(wrong_schema);
    let mut previous_schema = baseline.clone();
    previous_schema[8..12].copy_from_slice(&5u32.to_le_bytes());
    invalid_variants.push(previous_schema);
    let mut future_schema = baseline.clone();
    future_schema[8..12].copy_from_slice(&u32::MAX.to_le_bytes());
    invalid_variants.push(future_schema);
    invalid_variants.push(baseline[..baseline.len() - 1].to_vec());
    let mut wrong_catalog_identity = baseline.clone();
    wrong_catalog_identity[20] ^= 0x5a;
    invalid_variants.push(wrong_catalog_identity);
    let mut wrong_payload_length = baseline.clone();
    let payload_length = u64::from_le_bytes(wrong_payload_length[36..44].try_into().unwrap());
    wrong_payload_length[36..44].copy_from_slice(&payload_length.saturating_add(1).to_le_bytes());
    invalid_variants.push(wrong_payload_length);
    let mut trailing_payload = baseline.clone();
    trailing_payload.push(0x5a);
    trailing_payload[36..44].copy_from_slice(&payload_length.saturating_add(1).to_le_bytes());
    let trailing_checksum = crc32fast::hash(&trailing_payload[48..]);
    trailing_payload[44..48].copy_from_slice(&trailing_checksum.to_le_bytes());
    invalid_variants.push(trailing_payload);
    let mut checksum_failure = baseline.clone();
    checksum_failure[48] ^= 0x5a;
    invalid_variants.push(checksum_failure);
    let mut hostile_sequence_length = baseline.clone();
    hostile_sequence_length[48..56].copy_from_slice(&u64::MAX.to_le_bytes());
    let hostile_checksum = crc32fast::hash(&hostile_sequence_length[48..]);
    hostile_sequence_length[44..48].copy_from_slice(&hostile_checksum.to_le_bytes());
    invalid_variants.push(hostile_sequence_length);

    for variant in invalid_variants {
        fs::write(&snapshot, variant).unwrap();
        let (recovered, metrics) =
            Core::open_profiled(CoreConfig::new(state.path().to_path_buf())).unwrap();
        assert_eq!(metrics.cache_source, Some(StartupCacheSource::Invalid));
        assert_eq!(search(&recovered, "authority").results.len(), 1);
        assert!(recovered.list_roots().iter().any(|root| root.id == root_id));
    }

    let mut stale_generation = baseline.clone();
    stale_generation[12..20].copy_from_slice(&u64::MAX.to_le_bytes());
    fs::write(&snapshot, stale_generation).unwrap();
    {
        let (recovered, metrics) =
            Core::open_profiled(CoreConfig::new(state.path().to_path_buf())).unwrap();
        assert_eq!(
            metrics.cache_source,
            Some(StartupCacheSource::StaleGeneration)
        );
        assert_eq!(metrics.stale_snapshot_generation, Some(u64::MAX));
        assert_eq!(search(&recovered, "authority").results.len(), 1);
    }

    fs::remove_file(&snapshot).unwrap();
    let (rebuilt, metrics) =
        Core::open_profiled(CoreConfig::new(state.path().to_path_buf())).unwrap();
    assert_eq!(metrics.cache_source, Some(StartupCacheSource::Missing));
    assert_eq!(search(&rebuilt, "authority").results.len(), 1);
    assert!(snapshot.exists());
}

#[test]
fn persisted_compact_cache_matches_rocksdb_rebuild_for_search_and_history_contracts() {
    let state = TempDir::new().unwrap();
    let local_root_id;
    let offline_root_id;
    let history_id;
    {
        let core = open_core(state.path());
        let local = core
            .add_root(PathBuf::from("X:/equivalence"), RootKind::Local)
            .unwrap();
        let offline = core
            .add_root(PathBuf::from("Z:/offline-equivalence"), RootKind::Network)
            .unwrap();
        local_root_id = local.id;
        offline_root_id = offline.id;

        let mut local_items = Vec::new();
        for i in 0..240 {
            let folder = if i % 3 == 0 { "current" } else { "archive" };
            let extension = if i % 5 == 0 { "pdf" } else { "mov" };
            let mut item = DiscoveredItem::new(
                PathBuf::from(format!(
                    "X:/equivalence/{folder}/Scene_{i:03}_café.{extension}"
                )),
                ItemKind::File,
                1_024 + i as u64 * 32_768,
                1_700_000_000_000 + i as i64 * 86_400_000,
            );
            if i == 17 {
                item.priority = Priority::High;
            } else if i == 18 {
                item.priority = Priority::Low;
            }
            local_items.push(item);
        }
        local_items.push(DiscoveredItem::new(
            PathBuf::from("X:/equivalence/current/Folder_Result"),
            ItemKind::Folder,
            0,
            1_700_000_000_000,
        ));
        core.replace_root_snapshot(local.id, local_items).unwrap();
        core.replace_root_snapshot(
            offline.id,
            vec![DiscoveredItem::new(
                PathBuf::from("Z:/offline-equivalence/Offline_Scene.mov"),
                ItemKind::File,
                42,
                1_700_000_000_000,
            )],
        )
        .unwrap();
        core.set_root_unavailable(offline.id, "offline fixture".into())
            .unwrap();

        history_id = search(&core, "scene_017").results[0].item.id;
        for offset in 0..4 {
            core.record_activation_with_source(
                history_id,
                1_800_000_000_000 + offset,
                ActivationSource::QuickSwitch,
            )
            .unwrap();
        }
        core.flush().unwrap();
    }

    let requests = vec![
        SearchRequest {
            query: "scene_017".into(),
            limit: 20,
            now_unix_ms: 1_800_000_001_000,
            ..SearchRequest::default()
        },
        SearchRequest {
            query: "scn17".into(),
            limit: 20,
            now_unix_ms: 1_800_000_001_000,
            ..SearchRequest::default()
        },
        SearchRequest {
            query: "ext:pdf scene".into(),
            limit: 30,
            now_unix_ms: 1_800_000_001_000,
            ..SearchRequest::default()
        },
        SearchRequest {
            query: "size:>1mb".into(),
            limit: 30,
            now_unix_ms: 1_800_000_001_000,
            ..SearchRequest::default()
        },
        SearchRequest {
            query: "scene".into(),
            limit: 20,
            candidate_limit: 37,
            context: SearchContext {
                current_folder: Some("X:/equivalence/current".into()),
                contextual: true,
            },
            now_unix_ms: 1_800_000_001_000,
            ..SearchRequest::default()
        },
        SearchRequest {
            query: "offline".into(),
            limit: 20,
            include_unavailable: false,
            root_ids: vec![offline_root_id],
            now_unix_ms: 1_800_000_001_000,
            ..SearchRequest::default()
        },
        SearchRequest {
            query: "offline".into(),
            limit: 20,
            include_unavailable: true,
            root_ids: vec![offline_root_id],
            now_unix_ms: 1_800_000_001_000,
            ..SearchRequest::default()
        },
        SearchRequest {
            query: "folder".into(),
            limit: 20,
            kinds: vec![ItemKind::Folder],
            root_ids: vec![local_root_id],
            now_unix_ms: 1_800_000_001_000,
            ..SearchRequest::default()
        },
        SearchRequest {
            query: String::new(),
            limit: 20,
            now_unix_ms: 1_800_000_001_000,
            ..SearchRequest::default()
        },
    ];

    let capture = |core: &Core| {
        json!({
            "searches": requests
                .iter()
                .map(|request| search_contract(core, request))
                .collect::<Vec<_>>(),
            "roots": core.list_roots(),
            "history": core.recommendations(1_800_000_001_000, 20),
            "history_item": core.get_item(history_id),
        })
    };

    let (persisted, persisted_metrics) =
        Core::open_profiled(CoreConfig::new(state.path().to_path_buf())).unwrap();
    assert_eq!(
        persisted_metrics.cache_source,
        Some(StartupCacheSource::Snapshot)
    );
    let persisted_contract = capture(&persisted);
    drop(persisted);

    fs::remove_file(state.path().join("cache/search-cache.bin")).unwrap();
    let (rebuilt, rebuilt_metrics) =
        Core::open_profiled(CoreConfig::new(state.path().to_path_buf())).unwrap();
    assert_eq!(
        rebuilt_metrics.cache_source,
        Some(StartupCacheSource::Missing)
    );
    let rebuilt_contract = capture(&rebuilt);
    assert_eq!(
        persisted_contract, rebuilt_contract,
        "persisted compact cache and canonical RocksDB rebuild changed public search/history behavior"
    );
}

#[cfg(feature = "test-fault-injection")]
#[test]
fn flush_cannot_publish_a_new_generation_with_a_stale_cache() {
    let state = TempDir::new().unwrap();
    let core = Arc::new(open_core(state.path()));
    let root = core
        .add_root(PathBuf::from("X:/checkpoint-gate"), RootKind::Local)
        .unwrap();
    let store_committed = Arc::new(Barrier::new(2));
    let resume_cache_update = Arc::new(Barrier::new(2));

    let writer_core = Arc::clone(&core);
    let writer_committed = Arc::clone(&store_committed);
    let writer_resume = Arc::clone(&resume_cache_update);
    let writer = thread::spawn(move || {
        writer_core.test_upsert_items_with_post_commit_hook(
            root.id,
            vec![DiscoveredItem::new(
                PathBuf::from("X:/checkpoint-gate/committed-before-cache.txt"),
                ItemKind::File,
                1,
                1,
            )],
            || {
                writer_committed.wait();
                writer_resume.wait();
            },
        )
    });
    store_committed.wait();

    let (flush_started_tx, flush_started_rx) = std::sync::mpsc::channel();
    let (flush_done_tx, flush_done_rx) = std::sync::mpsc::channel();
    let flush_core = Arc::clone(&core);
    let flusher = thread::spawn(move || {
        flush_started_tx.send(()).unwrap();
        let result = flush_core.flush();
        flush_done_tx.send(result).unwrap();
    });
    flush_started_rx
        .recv_timeout(Duration::from_secs(2))
        .unwrap();
    assert!(
        flush_done_rx
            .recv_timeout(Duration::from_millis(100))
            .is_err(),
        "flush crossed the catalog/store-to-cache commit gap"
    );

    resume_cache_update.wait();
    writer.join().unwrap().unwrap();
    flush_done_rx
        .recv_timeout(Duration::from_secs(5))
        .unwrap()
        .unwrap();
    flusher.join().unwrap();
    drop(core);

    let (reopened, metrics) = Core::open_profiled(CoreConfig::new(state.path().to_path_buf()))
        .expect("checkpointed core reopens");
    assert_eq!(metrics.cache_source, Some(StartupCacheSource::Snapshot));
    assert_eq!(search(&reopened, "committed-before-cache").results.len(), 1);
}

#[test]
fn representative_physical_tree_handles_unicode_compound_extensions_and_reconciliation() {
    let state = TempDir::new().unwrap();
    let files = TempDir::new().unwrap();
    for folder in 0..20 {
        for file in 0..50 {
            let extension = if file % 10 == 0 { "tar.gz" } else { "mov" };
            let name = format!("场景_{folder:02}_{file:02}_café.{extension}");
            write(
                files.path().join(format!("项目_{folder:02}")).join(name),
                &[file as u8; 32],
            );
        }
    }
    let core = open_core(state.path());
    let root = core
        .add_root(files.path().to_path_buf(), RootKind::Local)
        .unwrap();
    let report = core
        .reconcile_root(root.id, &CancellationToken::new())
        .unwrap();
    assert_eq!(report.discovered, 1_020);
    assert_eq!(search(&core, "café ext:tar.gz").results.len(), 50);

    let old = files.path().join("项目_19").join("场景_19_49_café.mov");
    let moved_dir = files.path().join("renamed");
    fs::create_dir_all(&moved_dir).unwrap();
    let moved = moved_dir.join("final_场景_café.mov");
    fs::rename(&old, &moved).unwrap();
    let deleted = files.path().join("项目_00").join("场景_00_01_café.mov");
    fs::remove_file(&deleted).unwrap();
    core.reconcile_paths(
        root.id,
        vec![old, moved, deleted],
        &CancellationToken::new(),
    )
    .unwrap();
    assert_eq!(
        search(&core, "final_场景").results[0].item.name,
        "final_场景_café.mov"
    );
    assert!(search(&core, "场景_00_01").results.is_empty());
}
