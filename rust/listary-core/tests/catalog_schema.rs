use listary_core::{
    CORE_SCHEMA_VERSION, CancellationToken, Core, CoreConfig, CoreError, DiscoveredItem, ItemKind,
    ItemRecord, Priority, RootKind, RootRecord, SearchRequest, StartupCacheSource,
};
use rocksdb::{DB, Options};
use std::{collections::BTreeMap, fs, path::PathBuf};
use tempfile::TempDir;

const COLUMN_FAMILIES: [&str; 5] = ["default", "items", "paths", "roots", "meta"];
const SCHEMA_KEY: &[u8] = b"schema_version";
const IDENTITY_KEY: &[u8] = b"catalog_identity";

fn raw_database(state: &TempDir, columns: &[&str]) -> DB {
    let mut options = Options::default();
    options.create_if_missing(true);
    options.create_missing_column_families(true);
    DB::open_cf(&options, state.path().join("index"), columns).unwrap()
}

fn open_core(state: &TempDir) -> Result<Core, CoreError> {
    Core::open(CoreConfig::new(state.path().to_path_buf()))
}

fn fixture(state: &TempDir) -> (RootRecord, ItemRecord) {
    let root = RootRecord::new(state.path().join("files"), RootKind::Local);
    let item = ItemRecord::from_path_metadata(
        1,
        root.id,
        &root.path.join("authority.txt"),
        ItemKind::File,
        10,
        123,
        Priority::Normal,
    );
    let db = raw_database(state, &COLUMN_FAMILIES);
    db.put_cf(
        &db.cf_handle("roots").unwrap(),
        root.id.as_bytes(),
        bincode::serialize(&root).unwrap(),
    )
    .unwrap();
    db.put_cf(
        &db.cf_handle("items").unwrap(),
        item.id.to_be_bytes(),
        bincode::serialize(&item).unwrap(),
    )
    .unwrap();
    db.put_cf(
        &db.cf_handle("paths").unwrap(),
        item.path.as_bytes(),
        item.id.to_be_bytes(),
    )
    .unwrap();
    let meta = db.cf_handle("meta").unwrap();
    db.put_cf(&meta, b"next_id", 2_u32.to_le_bytes()).unwrap();
    db.put_cf(&meta, b"catalog_generation", 45_u64.to_le_bytes())
        .unwrap();
    (root, item)
}

fn index_bytes(state: &TempDir) -> BTreeMap<PathBuf, Vec<u8>> {
    let index = state.path().join("index");
    walkdir::WalkDir::new(&index)
        .into_iter()
        .map(Result::unwrap)
        .filter(|entry| entry.file_type().is_file())
        .map(|entry| {
            (
                entry.path().strip_prefix(&index).unwrap().to_path_buf(),
                fs::read(entry.path()).unwrap(),
            )
        })
        .collect()
}

fn assert_no_recovery(state: &TempDir) {
    assert!(
        fs::read_dir(state.path()).unwrap().all(|entry| {
            !entry
                .unwrap()
                .file_name()
                .to_string_lossy()
                .starts_with("index.corrupt.")
        }),
        "schema/record rejection must never quarantine and recreate the catalog"
    );
}

fn assert_rejected_untouched(state: &TempDir, expected: &str) {
    let before = index_bytes(state);
    let error = open_core(state).expect_err("incompatible catalog must fail closed");
    assert!(
        matches!(error, CoreError::Index(_) | CoreError::Serialization(_)),
        "unexpected error: {error}"
    );
    assert!(
        error.to_string().contains(expected),
        "expected {expected:?} in {error}"
    );
    assert_eq!(index_bytes(state), before, "rejection mutated index files");
    assert_no_recovery(state);
}

#[test]
fn new_catalog_records_the_real_durable_schema() {
    let state = TempDir::new().unwrap();
    {
        let core = open_core(&state).unwrap();
        assert_eq!(
            core.diagnostics().unwrap().schema_version,
            CORE_SCHEMA_VERSION
        );
    }
    let db = raw_database(&state, &COLUMN_FAMILIES);
    assert_eq!(
        db.get_cf(&db.cf_handle("meta").unwrap(), SCHEMA_KEY)
            .unwrap()
            .unwrap(),
        CORE_SCHEMA_VERSION.to_le_bytes()
    );
}

#[test]
fn catalog_identity_is_persisted_and_stable_across_reopen_and_mutation() {
    let state = TempDir::new().unwrap();
    let identity;
    {
        drop(open_core(&state).unwrap());
        let db = raw_database(&state, &COLUMN_FAMILIES);
        identity = db
            .get_cf(&db.cf_handle("meta").unwrap(), IDENTITY_KEY)
            .unwrap()
            .expect("new catalog must have a durable identity");
        let parsed = uuid::Uuid::from_slice(&identity).unwrap();
        assert_eq!(parsed.get_version(), Some(uuid::Version::Random));
        assert_eq!(parsed.get_variant(), uuid::Variant::RFC4122);
    }
    {
        let core = open_core(&state).unwrap();
        core.add_root(state.path().join("files"), RootKind::Local)
            .unwrap();
        core.flush().unwrap();
    }
    {
        let (core, metrics) =
            Core::open_profiled(CoreConfig::new(state.path().to_path_buf())).unwrap();
        assert_eq!(metrics.cache_source, Some(StartupCacheSource::Snapshot));
        assert_eq!(metrics.rocksdb_items_loaded, 0);
        assert_eq!(core.catalog_generation().unwrap(), 1);
    }
    let db = raw_database(&state, &COLUMN_FAMILIES);
    assert_eq!(
        db.get_cf(&db.cf_handle("meta").unwrap(), IDENTITY_KEY)
            .unwrap()
            .unwrap(),
        identity
    );
}

#[test]
fn missing_catalog_identity_adopts_validated_records_without_trusting_existing_cache() {
    for unversioned in [false, true] {
        let state = TempDir::new().unwrap();
        let (root, item) = fixture(&state);
        {
            let core = open_core(&state).unwrap();
            core.record_activation(item.id, 1_800_000_000_000).unwrap();
            core.flush().unwrap();
        }
        let history = fs::read(state.path().join("history.json")).unwrap();
        {
            let db = raw_database(&state, &COLUMN_FAMILIES);
            let meta = db.cf_handle("meta").unwrap();
            db.delete_cf(&meta, IDENTITY_KEY).unwrap();
            if unversioned {
                db.delete_cf(&meta, SCHEMA_KEY).unwrap();
            }
        }
        {
            let (core, metrics) =
                Core::open_profiled(CoreConfig::new(state.path().to_path_buf())).unwrap();
            assert_ne!(metrics.cache_source, Some(StartupCacheSource::Snapshot));
            assert_eq!(metrics.rocksdb_items_loaded, 1);
            assert_eq!(core.catalog_generation().unwrap(), 45);
            assert_eq!(core.list_roots(), vec![root.clone()]);
            assert_eq!(core.get_item(item.id).unwrap().path, item.path);
            assert_eq!(core.diagnostics().unwrap().history_entries, 1);
            core.flush().unwrap();
        }
        assert_eq!(
            fs::read(state.path().join("history.json")).unwrap(),
            history
        );
        let (_, metrics) =
            Core::open_profiled(CoreConfig::new(state.path().to_path_buf())).unwrap();
        assert_eq!(metrics.cache_source, Some(StartupCacheSource::Snapshot));
        assert_no_recovery(&state);
    }
}

#[test]
fn malformed_catalog_identity_is_rejected_before_writes_or_repair() {
    for value in [
        vec![],
        vec![0; 15],
        vec![0; 16],
        vec![0xff; 16],
        vec![1; 17],
        vec![0; 70_000],
    ] {
        let state = TempDir::new().unwrap();
        fixture(&state);
        {
            let db = raw_database(&state, &COLUMN_FAMILIES);
            let meta = db.cf_handle("meta").unwrap();
            db.put_cf(&meta, SCHEMA_KEY, 1_u32.to_le_bytes()).unwrap();
            db.put_cf(&meta, IDENTITY_KEY, value).unwrap();
        }
        assert_rejected_untouched(&state, "catalog identity");
    }
}

#[test]
fn missing_identity_does_not_adopt_a_malformed_versioned_catalog() {
    let state = TempDir::new().unwrap();
    let (_, item) = fixture(&state);
    {
        let db = raw_database(&state, &COLUMN_FAMILIES);
        let meta = db.cf_handle("meta").unwrap();
        db.put_cf(&meta, SCHEMA_KEY, 1_u32.to_le_bytes()).unwrap();
        let mut malformed = bincode::serialize(&item).unwrap();
        malformed.push(0);
        db.put_cf(
            &db.cf_handle("items").unwrap(),
            item.id.to_be_bytes(),
            malformed,
        )
        .unwrap();
    }
    assert_rejected_untouched(&state, "catalog item record");
}

#[test]
fn healthy_legacy_catalog_adopts_v1_without_rewriting_records_or_generation() {
    let state = TempDir::new().unwrap();
    let (root, item) = fixture(&state);
    {
        let core = open_core(&state).unwrap();
        assert_eq!(core.list_roots(), vec![root.clone()]);
        assert_eq!(core.diagnostics().unwrap().catalog_generation, 45);
        let result = core
            .search(
                &SearchRequest {
                    query: "authority".into(),
                    ..SearchRequest::default()
                },
                &CancellationToken::new(),
            )
            .unwrap();
        assert_eq!(result.results.len(), 1);
        assert_eq!(result.results[0].item.id, item.id);
    }
    let db = raw_database(&state, &COLUMN_FAMILIES);
    assert_eq!(
        db.get_cf(&db.cf_handle("meta").unwrap(), SCHEMA_KEY)
            .unwrap()
            .unwrap(),
        1_u32.to_le_bytes()
    );
    assert_eq!(
        db.get_cf(&db.cf_handle("items").unwrap(), item.id.to_be_bytes())
            .unwrap()
            .unwrap(),
        bincode::serialize(&item).unwrap()
    );
    assert_eq!(
        db.get_cf(&db.cf_handle("roots").unwrap(), root.id.as_bytes())
            .unwrap()
            .unwrap(),
        bincode::serialize(&root).unwrap()
    );
    assert_no_recovery(&state);
}

#[test]
fn unsupported_schema_never_opens_writable_repairs_or_recreates() {
    for version in [0_u32, CORE_SCHEMA_VERSION + 1, u32::MAX] {
        let state = TempDir::new().unwrap();
        fixture(&state);
        {
            let db = raw_database(&state, &COLUMN_FAMILIES);
            db.put_cf(
                &db.cf_handle("meta").unwrap(),
                SCHEMA_KEY,
                version.to_le_bytes(),
            )
            .unwrap();
        }
        assert_rejected_untouched(&state, "unsupported catalog schema version");
    }
}

#[test]
fn malformed_or_trailing_schema_bytes_never_trigger_recovery() {
    for value in [vec![], vec![1, 0, 0], vec![1, 0, 0, 0, 0], vec![0; 70_000]] {
        let state = TempDir::new().unwrap();
        fixture(&state);
        {
            let db = raw_database(&state, &COLUMN_FAMILIES);
            db.put_cf(&db.cf_handle("meta").unwrap(), SCHEMA_KEY, value)
                .unwrap();
        }
        assert_rejected_untouched(&state, "catalog schema version record");
    }
}

#[test]
fn missing_column_families_are_not_silently_created() {
    for missing in ["items", "roots", "paths", "meta"] {
        let state = TempDir::new().unwrap();
        let columns: Vec<_> = COLUMN_FAMILIES
            .iter()
            .copied()
            .filter(|name| *name != missing)
            .collect();
        drop(raw_database(&state, &columns));
        assert_rejected_untouched(&state, "unsupported catalog column-family layout");
    }
}

#[test]
fn unknown_column_family_is_not_repaired_away() {
    let state = TempDir::new().unwrap();
    let mut columns = COLUMN_FAMILIES.to_vec();
    columns.push("future-records");
    {
        let db = raw_database(&state, &columns);
        db.put_cf(&db.cf_handle("future-records").unwrap(), b"keep", b"future")
            .unwrap();
    }
    assert_rejected_untouched(&state, "unsupported catalog column-family layout");
}

#[test]
fn legacy_items_reject_truncation_trailing_oversize_and_hostile_string_lengths() {
    for corruption in 0..4 {
        let state = TempDir::new().unwrap();
        let (_, item) = fixture(&state);
        let mut value = bincode::serialize(&item).unwrap();
        match corruption {
            0 => value.truncate(value.len() - 1),
            1 => value.push(0),
            2 => value.resize(1024 * 1024 + 1, 0),
            // v1: u32 ItemId, length-prefixed UUID bytes, then the first string.
            3 => value[28..36].copy_from_slice(&u64::MAX.to_le_bytes()),
            _ => unreachable!(),
        }
        {
            let db = raw_database(&state, &COLUMN_FAMILIES);
            db.put_cf(
                &db.cf_handle("items").unwrap(),
                item.id.to_be_bytes(),
                value,
            )
            .unwrap();
        }
        assert_rejected_untouched(&state, "catalog item record");
    }
}

#[test]
fn legacy_roots_reject_truncation_trailing_oversize_and_hostile_path_lengths() {
    for corruption in 0..4 {
        let state = TempDir::new().unwrap();
        let (root, _) = fixture(&state);
        let mut value = bincode::serialize(&root).unwrap();
        match corruption {
            0 => value.truncate(value.len() - 1),
            1 => value.push(0),
            2 => value.resize(512 * 1024 + 1, 0),
            // v1: length-prefixed UUID bytes, then PathBuf's UTF-8 string.
            3 => value[24..32].copy_from_slice(&u64::MAX.to_le_bytes()),
            _ => unreachable!(),
        }
        {
            let db = raw_database(&state, &COLUMN_FAMILIES);
            db.put_cf(&db.cf_handle("roots").unwrap(), root.id.as_bytes(), value)
                .unwrap();
        }
        assert_rejected_untouched(&state, "catalog root record");
    }
}

#[test]
fn legacy_keys_must_match_item_and_root_record_identity() {
    for root_case in [false, true] {
        let state = TempDir::new().unwrap();
        let (root, item) = fixture(&state);
        {
            let db = raw_database(&state, &COLUMN_FAMILIES);
            if root_case {
                db.put_cf(
                    &db.cf_handle("roots").unwrap(),
                    [0_u8; 16],
                    bincode::serialize(&root).unwrap(),
                )
                .unwrap();
            } else {
                db.put_cf(
                    &db.cf_handle("items").unwrap(),
                    2_u32.to_be_bytes(),
                    bincode::serialize(&item).unwrap(),
                )
                .unwrap();
            }
        }
        assert_rejected_untouched(&state, "key does not match");
    }
}

#[test]
fn invalid_metadata_is_bounded_and_fails_before_writable_open() {
    let cases: Vec<(&[u8], Vec<u8>)> = vec![
        (b"next_id", vec![1, 0, 0]),
        (b"catalog_generation", vec![0; 9]),
        (b"recovery", vec![0; 64 * 1024 + 1]),
        (b"recovery", vec![0; 8]),
        (b"recovery", {
            let mut value = vec![0; 8]; // recovery_count
            value.push(1); // Some(last_recovery_path)
            value.extend_from_slice(&u64::MAX.to_le_bytes());
            value
        }),
    ];
    for (key, value) in cases {
        let state = TempDir::new().unwrap();
        fixture(&state);
        {
            let db = raw_database(&state, &COLUMN_FAMILIES);
            let meta = db.cf_handle("meta").unwrap();
            db.put_cf(&meta, SCHEMA_KEY, 1_u32.to_le_bytes()).unwrap();
            db.put_cf(&meta, key, value).unwrap();
        }
        assert_rejected_untouched(&state, "catalog");
    }
}

#[test]
fn versioned_catalog_record_loads_remain_strict() {
    for root_case in [false, true] {
        let state = TempDir::new().unwrap();
        let (root, item) = fixture(&state);
        {
            let db = raw_database(&state, &COLUMN_FAMILIES);
            db.put_cf(
                &db.cf_handle("meta").unwrap(),
                SCHEMA_KEY,
                1_u32.to_le_bytes(),
            )
            .unwrap();
            let (column, key, mut value) = if root_case {
                (
                    "roots",
                    root.id.as_bytes().to_vec(),
                    bincode::serialize(&root).unwrap(),
                )
            } else {
                (
                    "items",
                    item.id.to_be_bytes().to_vec(),
                    bincode::serialize(&item).unwrap(),
                )
            };
            value.push(0);
            db.put_cf(&db.cf_handle(column).unwrap(), key, value)
                .unwrap();
        }
        assert!(matches!(
            open_core(&state),
            Err(CoreError::Serialization(_))
        ));
        assert_no_recovery(&state);
    }
}

#[test]
fn oversized_item_write_is_rejected_without_partial_catalog_mutation() {
    let state = TempDir::new().unwrap();
    let core = open_core(&state).unwrap();
    let root = core
        .add_root(state.path().join("files"), RootKind::Local)
        .unwrap();
    let generation = core.diagnostics().unwrap().catalog_generation;
    let mut oversized = DiscoveredItem::new(root.path.join("large.txt"), ItemKind::File, 1, 1);
    oversized.preview_hint = Some("a".repeat(1024 * 1024));
    let valid = DiscoveredItem::new(root.path.join("valid.txt"), ItemKind::File, 1, 1);
    assert!(matches!(
        core.upsert_items(root.id, vec![valid, oversized]),
        Err(CoreError::Serialization(_))
    ));
    assert_eq!(core.diagnostics().unwrap().catalog_generation, generation);
    drop(core);
    let db = raw_database(&state, &COLUMN_FAMILIES);
    assert!(
        db.iterator_cf(
            &db.cf_handle("items").unwrap(),
            rocksdb::IteratorMode::Start
        )
        .next()
        .is_none()
    );
}
