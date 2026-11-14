use crate::{
    error::{CoreError, Result},
    model::{ItemId, ItemRecord, RefreshPolicy, RootRecord, RootRecoveryRemovalReport},
};
use bincode::Options as _;
use fs2::FileExt;
use rocksdb::{
    ColumnFamilyDescriptor, DBWithThreadMode, FlushOptions, MultiThreaded, Options, WriteBatch,
};
use serde::{Deserialize, Serialize};
use std::{
    fs::{self, File, OpenOptions},
    path::{Path, PathBuf},
};
use uuid::Uuid;

const CF_ITEMS: &str = "items";
const CF_PATHS: &str = "paths";
const CF_ROOTS: &str = "roots";
const CF_META: &str = "meta";
const COLUMN_FAMILIES: [&str; 5] = ["default", CF_ITEMS, CF_PATHS, CF_ROOTS, CF_META];
const META_SCHEMA_VERSION: &[u8] = b"schema_version";
const META_NEXT_ID: &[u8] = b"next_id";
const META_RECOVERY: &[u8] = b"recovery";
const META_CATALOG_GENERATION: &[u8] = b"catalog_generation";
const META_CATALOG_IDENTITY: &[u8] = b"catalog_identity";

/// The persisted RocksDB record layout, independent of the FFI and derived cache formats.
pub const CATALOG_SCHEMA_VERSION: u32 = 1;

// These limits comfortably include long platform paths and metadata while preventing a
// damaged record or length prefix from becoming an unbounded allocation. Encoding stays
// compatible with bincode 1.x's original fixed-integer v1 representation.
const MAX_ITEM_RECORD_BYTES: usize = 1024 * 1024;
const MAX_ROOT_RECORD_BYTES: usize = 512 * 1024;
const MAX_META_RECORD_BYTES: usize = 64 * 1024;
const RECOVERY_DELETE_BATCH_ITEMS: usize = 10_000;

type Db = DBWithThreadMode<MultiThreaded>;

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct RecoveryMetadata {
    pub recovery_count: u64,
    pub last_recovery_path: Option<String>,
}

pub struct Store {
    db: Db,
    index_dir: PathBuf,
    _lock: File,
    _profile_lease: Option<ProfileLease>,
}

pub(crate) struct ProfileLease {
    _file: File,
}

impl std::fmt::Debug for Store {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("Store")
            .field("index_dir", &self.index_dir)
            .finish_non_exhaustive()
    }
}

impl Store {
    pub(crate) fn profile_lease_path(state_dir: &Path) -> PathBuf {
        let parent = state_dir
            .parent()
            .filter(|path| !path.as_os_str().is_empty())
            .unwrap_or_else(|| Path::new("."));
        let name = state_dir
            .file_name()
            .map(|name| name.to_string_lossy().into_owned())
            .unwrap_or_else(|| "state".to_string());
        parent.join(format!(".{name}.listary-profile.lock"))
    }

    pub(crate) fn acquire_shared_profile_lease(state_dir: &Path) -> Result<ProfileLease> {
        acquire_profile_lease(state_dir, false)
    }

    pub(crate) fn acquire_exclusive_profile_lease(state_dir: &Path) -> Result<ProfileLease> {
        acquire_profile_lease(state_dir, true)
    }

    pub(crate) fn open_with_profile_lease(
        state_dir: &Path,
        profile_lease: Option<ProfileLease>,
    ) -> Result<Self> {
        fs::create_dir_all(state_dir).map_err(|source| CoreError::Filesystem {
            path: state_dir.to_path_buf(),
            source,
        })?;
        let lock_path = state_dir.join("core.lock");
        let lock = OpenOptions::new()
            .read(true)
            .write(true)
            .create(true)
            .truncate(false)
            .open(&lock_path)
            .map_err(|source| CoreError::Filesystem {
                path: lock_path.clone(),
                source,
            })?;
        lock.try_lock_exclusive()
            .map_err(|_| CoreError::AlreadyOpen)?;

        let index_dir = state_dir.join("index");
        match open_db(&index_dir) {
            Ok(db) => Ok(Self {
                db,
                index_dir,
                _lock: lock,
                _profile_lease: profile_lease,
            }),
            Err(OpenDbError::Rejected(error)) => Err(error),
            Err(OpenDbError::Physical(first)) => {
                if index_dir.exists() {
                    let mut opts = base_options();
                    opts.create_if_missing(true);
                    let _ = Db::repair(&opts, &index_dir);
                    match open_db(&index_dir) {
                        Ok(db) => {
                            let store = Self {
                                db,
                                index_dir,
                                _lock: lock,
                                _profile_lease: profile_lease,
                            };
                            let mut recovery = store.recovery_metadata().unwrap_or_default();
                            recovery.recovery_count = recovery.recovery_count.saturating_add(1);
                            recovery.last_recovery_path = None;
                            store.put_meta(META_RECOVERY, &recovery)?;
                            return Ok(store);
                        }
                        // A repaired database still belongs to its recorded schema. Never
                        // turn an incompatibility into permission to discard that catalog.
                        Err(OpenDbError::Rejected(error)) => return Err(error),
                        Err(OpenDbError::Physical(_)) => {}
                    }
                }

                let backup = backup_corrupt_index(&index_dir)?;
                let db = open_db(&index_dir).map_err(|second| {
                    CoreError::Index(format!(
                        "could not open or recover index; first={first}; second={second}"
                    ))
                })?;
                let store = Self {
                    db,
                    index_dir,
                    _lock: lock,
                    _profile_lease: profile_lease,
                };
                let mut recovery = store.recovery_metadata().unwrap_or_default();
                recovery.recovery_count = recovery.recovery_count.saturating_add(1);
                recovery.last_recovery_path = backup.map(|p| p.to_string_lossy().into_owned());
                store.put_meta(META_RECOVERY, &recovery)?;
                cleanup_old_recoveries(state_dir, 2);
                Ok(store)
            }
        }
    }

    pub fn load_items(&self) -> Result<Vec<ItemRecord>> {
        let mut out = Vec::new();
        visit_records(&self.db, CF_ITEMS, |key, value| {
            out.push(decode_item(key, value)?);
            Ok(())
        })?;
        Ok(out)
    }

    pub fn load_roots(&self) -> Result<Vec<RootRecord>> {
        let mut out = Vec::new();
        visit_records(&self.db, CF_ROOTS, |key, value| {
            out.push(decode_root(key, value)?);
            Ok(())
        })?;
        Ok(out)
    }

    pub(crate) fn recover_remove_root(
        &self,
        root_id: Uuid,
        expected_path: &Path,
    ) -> Result<RootRecoveryRemovalReport> {
        let mut root = self
            .load_roots()?
            .into_iter()
            .find(|root| root.id == root_id)
            .ok_or_else(|| CoreError::RootNotFound(root_id.to_string()))?;
        if root.path != expected_path {
            return Err(CoreError::InvalidRequest(format!(
                "root path does not match recovery request: expected {}, actual {}",
                expected_path.display(),
                root.path.display()
            )));
        }

        let catalog_generation_before = self.catalog_generation()?;
        root.enabled = false;
        root.refresh_policy = RefreshPolicy::Never;
        root.last_error = Some("root removal recovery in progress".into());
        self.put_root(&root)?;
        let catalog_generation_after_disable = self.catalog_generation()?;

        let deleted_items = self.delete_root_items_bounded(root_id, RECOVERY_DELETE_BATCH_ITEMS)?;

        let roots_cf = self.cf(CF_ROOTS)?;
        let meta_cf = self.cf(CF_META)?;
        let catalog_generation_after_remove = self.catalog_generation()?.saturating_add(1);
        let mut batch = WriteBatch::default();
        batch.delete_cf(&roots_cf, root_id.as_bytes());
        batch.put_cf(
            &meta_cf,
            META_CATALOG_GENERATION,
            bincode::serialize(&catalog_generation_after_remove)?,
        );
        self.db.write(batch)?;
        self.flush()?;

        Ok(RootRecoveryRemovalReport {
            root_id,
            root_path: root.path,
            deleted_items,
            catalog_generation_before,
            catalog_generation_after_disable,
            catalog_generation_after_remove,
        })
    }

    fn delete_root_items_bounded(&self, root_id: Uuid, batch_limit: usize) -> Result<u64> {
        let items_cf = self.cf(CF_ITEMS)?;
        let paths_cf = self.cf(CF_PATHS)?;
        let batch_limit = batch_limit.max(1);
        let mut deleted_items = 0_u64;
        let mut resume_after = None::<Vec<u8>>;

        loop {
            let mut deletions = Vec::<(Vec<u8>, Vec<u8>)>::with_capacity(batch_limit);
            let mut last_seen = None::<Vec<u8>>;
            let mut reached_end = true;
            {
                let mut iterator = self.db.raw_iterator_cf(&items_cf);
                if let Some(resume_key) = resume_after.as_deref() {
                    iterator.seek(resume_key);
                    if iterator.valid() && iterator.key() == Some(resume_key) {
                        iterator.next();
                    }
                } else {
                    iterator.seek_to_first();
                }

                while iterator.valid() {
                    let (Some(key), Some(value)) = (iterator.key(), iterator.value()) else {
                        return Err(CoreError::Index(
                            "catalog iterator lost an item during root recovery".into(),
                        ));
                    };
                    last_seen = Some(key.to_vec());
                    let item = decode_item(key, value)?;
                    if item.root_id == root_id {
                        deletions.push((key.to_vec(), item.path.as_bytes().to_vec()));
                        if deletions.len() >= batch_limit {
                            reached_end = false;
                            break;
                        }
                    }
                    iterator.next();
                }
                iterator.status()?;
            }

            if !deletions.is_empty() {
                let mut batch = WriteBatch::default();
                for (item_key, path_key) in &deletions {
                    batch.delete_cf(&items_cf, item_key);
                    batch.delete_cf(&paths_cf, path_key);
                }
                self.db.write(batch)?;
                deleted_items = deleted_items.saturating_add(deletions.len() as u64);
            }

            if reached_end {
                return Ok(deleted_items);
            }
            resume_after = last_seen;
        }
    }

    pub fn put_root(&self, root: &RootRecord) -> Result<()> {
        let cf = self.cf(CF_ROOTS)?;
        let meta_cf = self.cf(CF_META)?;
        let next_generation = self.catalog_generation()?.saturating_add(1);
        let mut batch = WriteBatch::default();
        batch.put_cf(&cf, root.id.as_bytes(), encode_root(root)?);
        batch.put_cf(
            &meta_cf,
            META_CATALOG_GENERATION,
            bincode::serialize(&next_generation)?,
        );
        self.db.write(batch)?;
        Ok(())
    }

    pub fn next_id(&self) -> Result<ItemId> {
        Ok(self.get_meta::<ItemId>(META_NEXT_ID)?.unwrap_or(1).max(1))
    }

    pub fn catalog_generation(&self) -> Result<u64> {
        Ok(self.get_meta::<u64>(META_CATALOG_GENERATION)?.unwrap_or(0))
    }

    pub fn catalog_identity(&self) -> Result<Uuid> {
        let cf = self.cf(CF_META)?;
        let Some(value) = self.db.get_pinned_cf(&cf, META_CATALOG_IDENTITY)? else {
            return Err(CoreError::Index(
                "catalog identity is missing; preserve the index and reopen it through the catalog adoption path".into(),
            ));
        };
        validate_catalog_identity(&value)
    }

    pub fn write_snapshot(
        &self,
        root_id: Uuid,
        items: &[ItemRecord],
        removed: &[ItemRecord],
        next_id: ItemId,
    ) -> Result<()> {
        self.write_snapshot_inner(root_id, items, removed, next_id, None)
    }

    pub fn write_snapshot_and_root(
        &self,
        root: &RootRecord,
        items: &[ItemRecord],
        removed: &[ItemRecord],
        next_id: ItemId,
    ) -> Result<()> {
        self.write_snapshot_inner(root.id, items, removed, next_id, Some(root))
    }

    fn write_snapshot_inner(
        &self,
        root_id: Uuid,
        items: &[ItemRecord],
        removed: &[ItemRecord],
        next_id: ItemId,
        root_update: Option<&RootRecord>,
    ) -> Result<()> {
        let items_cf = self.cf(CF_ITEMS)?;
        let paths_cf = self.cf(CF_PATHS)?;
        let meta_cf = self.cf(CF_META)?;
        let next_generation = self.catalog_generation()?.saturating_add(1);
        let mut batch = WriteBatch::default();
        for old in removed {
            batch.delete_cf(&items_cf, id_key(old.id));
            batch.delete_cf(&paths_cf, old.path.as_bytes());
        }
        for item in items {
            batch.put_cf(&items_cf, id_key(item.id), encode_item(item)?);
            batch.put_cf(&paths_cf, item.path.as_bytes(), id_key(item.id));
        }
        if let Some(root) = root_update {
            let roots_cf = self.cf(CF_ROOTS)?;
            batch.put_cf(&roots_cf, root.id.as_bytes(), encode_root(root)?);
        }
        batch.put_cf(&meta_cf, META_NEXT_ID, bincode::serialize(&next_id)?);
        batch.put_cf(
            &meta_cf,
            META_CATALOG_GENERATION,
            bincode::serialize(&next_generation)?,
        );
        crate::test_fault::hit("store_before_snapshot_commit");
        self.db.write(batch)?;
        crate::test_fault::hit("store_after_snapshot_commit");
        // Root id is intentionally part of the API contract even though every item already
        // carries it; keep the parameter to make cross-root snapshot mistakes obvious.
        let _ = root_id;
        Ok(())
    }

    pub fn delete_root_snapshot(
        &self,
        root_id: Uuid,
        removed: &[ItemRecord],
        next_id: ItemId,
    ) -> Result<()> {
        let items_cf = self.cf(CF_ITEMS)?;
        let paths_cf = self.cf(CF_PATHS)?;
        let roots_cf = self.cf(CF_ROOTS)?;
        let meta_cf = self.cf(CF_META)?;
        let next_generation = self.catalog_generation()?.saturating_add(1);
        let mut batch = WriteBatch::default();
        for old in removed {
            batch.delete_cf(&items_cf, id_key(old.id));
            batch.delete_cf(&paths_cf, old.path.as_bytes());
        }
        batch.delete_cf(&roots_cf, root_id.as_bytes());
        batch.put_cf(&meta_cf, META_NEXT_ID, bincode::serialize(&next_id)?);
        batch.put_cf(
            &meta_cf,
            META_CATALOG_GENERATION,
            bincode::serialize(&next_generation)?,
        );
        crate::test_fault::hit("store_before_root_delete_commit");
        self.db.write(batch)?;
        crate::test_fault::hit("store_after_root_delete_commit");
        Ok(())
    }

    pub fn put_items(&self, items: &[ItemRecord], next_id: Option<ItemId>) -> Result<()> {
        let items_cf = self.cf(CF_ITEMS)?;
        let paths_cf = self.cf(CF_PATHS)?;
        let meta_cf = self.cf(CF_META)?;
        let next_generation = self.catalog_generation()?.saturating_add(1);
        let mut batch = WriteBatch::default();
        for item in items {
            batch.put_cf(&items_cf, id_key(item.id), encode_item(item)?);
            batch.put_cf(&paths_cf, item.path.as_bytes(), id_key(item.id));
        }
        batch.put_cf(
            &meta_cf,
            META_CATALOG_GENERATION,
            bincode::serialize(&next_generation)?,
        );
        if let Some(next_id) = next_id {
            batch.put_cf(&meta_cf, META_NEXT_ID, bincode::serialize(&next_id)?);
        }
        crate::test_fault::hit("store_before_put_items_commit");
        self.db.write(batch)?;
        crate::test_fault::hit("store_after_put_items_commit");
        Ok(())
    }

    pub fn put_root_and_items(&self, root: &RootRecord, items: &[ItemRecord]) -> Result<()> {
        let roots_cf = self.cf(CF_ROOTS)?;
        let meta_cf = self.cf(CF_META)?;
        let next_generation = self.catalog_generation()?.saturating_add(1);
        let mut batch = WriteBatch::default();
        batch.put_cf(&roots_cf, root.id.as_bytes(), encode_root(root)?);

        if !items.is_empty() {
            let items_cf = self.cf(CF_ITEMS)?;
            let paths_cf = self.cf(CF_PATHS)?;
            for item in items {
                batch.put_cf(&items_cf, id_key(item.id), encode_item(item)?);
                batch.put_cf(&paths_cf, item.path.as_bytes(), id_key(item.id));
            }
        }
        batch.put_cf(
            &meta_cf,
            META_CATALOG_GENERATION,
            bincode::serialize(&next_generation)?,
        );

        crate::test_fault::hit("store_before_root_items_commit");
        self.db.write(batch)?;
        crate::test_fault::hit("store_after_root_items_commit");
        Ok(())
    }

    pub fn recovery_metadata(&self) -> Result<RecoveryMetadata> {
        Ok(self
            .get_meta::<RecoveryMetadata>(META_RECOVERY)?
            .unwrap_or_default())
    }

    pub fn index_bytes(&self) -> u64 {
        dir_size(&self.index_dir).unwrap_or(0)
    }

    pub fn flush(&self) -> Result<()> {
        let items = self.cf(CF_ITEMS)?;
        let paths = self.cf(CF_PATHS)?;
        let roots = self.cf(CF_ROOTS)?;
        let meta = self.cf(CF_META)?;
        let options = FlushOptions::default();
        self.db
            .flush_cfs_opt(&[&items, &paths, &roots, &meta], &options)?;
        Ok(())
    }

    fn cf(&self, name: &str) -> Result<std::sync::Arc<rocksdb::BoundColumnFamily<'_>>> {
        self.db
            .cf_handle(name)
            .ok_or_else(|| CoreError::Index(format!("missing column family: {name}")))
    }

    fn put_meta<T: Serialize>(&self, key: &[u8], value: &T) -> Result<()> {
        let cf = self.cf(CF_META)?;
        self.db.put_cf(
            &cf,
            key,
            encode_record(value, MAX_META_RECORD_BYTES, "metadata")?,
        )?;
        Ok(())
    }

    fn get_meta<T: for<'de> Deserialize<'de>>(&self, key: &[u8]) -> Result<Option<T>> {
        let cf = self.cf(CF_META)?;
        let Some(value) = self.db.get_pinned_cf(&cf, key)? else {
            return Ok(None);
        };
        Ok(Some(decode_record(
            &value,
            MAX_META_RECORD_BYTES,
            meta_context(key),
        )?))
    }
}

fn acquire_profile_lease(state_dir: &Path, exclusive: bool) -> Result<ProfileLease> {
    let parent = state_dir
        .parent()
        .filter(|path| !path.as_os_str().is_empty())
        .unwrap_or_else(|| Path::new("."));
    fs::create_dir_all(parent).map_err(|source| CoreError::Filesystem {
        path: parent.to_path_buf(),
        source,
    })?;
    let path = Store::profile_lease_path(state_dir);
    let file = OpenOptions::new()
        .read(true)
        .write(true)
        .create(true)
        .truncate(false)
        .open(&path)
        .map_err(|source| CoreError::Filesystem {
            path: path.clone(),
            source,
        })?;
    let lock = if exclusive {
        FileExt::try_lock_exclusive(&file)
    } else {
        FileExt::try_lock_shared(&file)
    };
    lock.map_err(|_| CoreError::AlreadyOpen)?;
    Ok(ProfileLease { _file: file })
}

fn base_options() -> Options {
    let mut opts = Options::default();
    opts.set_max_log_file_size(5 * 1024 * 1024);
    opts.set_keep_log_file_num(3);
    opts.set_recycle_log_file_num(2);
    opts.set_write_buffer_size(64 * 1024 * 1024);
    opts.set_max_write_buffer_number(2);
    opts
}

#[derive(Debug, thiserror::Error)]
enum OpenDbError {
    #[error(transparent)]
    Physical(#[from] rocksdb::Error),
    #[error(transparent)]
    Rejected(#[from] CoreError),
}

fn open_db(path: &Path) -> std::result::Result<Db, OpenDbError> {
    let mut opts = base_options();
    let exists = path.try_exists().map_err(|source| CoreError::Filesystem {
        path: path.to_path_buf(),
        source,
    })?;
    let empty_directory = if path.is_dir() {
        fs::read_dir(path)
            .map_err(|source| CoreError::Filesystem {
                path: path.to_path_buf(),
                source,
            })?
            .next()
            .is_none()
    } else {
        false
    };
    let new_database = !exists || empty_directory;
    let mut adopt_legacy = false;
    let mut adopt_identity = false;
    if new_database {
        opts.create_if_missing(true);
        opts.create_missing_column_families(true);
    } else {
        // List and inspect using read-only access first. Writable open may create a
        // WAL/manifest/OPTIONS generation, even when the application then rejects
        // the schema. Incompatible catalogs must not be touched or repaired.
        let mut actual = Db::list_cf(&opts, path)?;
        actual.sort_unstable();
        let mut expected = COLUMN_FAMILIES.map(str::to_owned);
        expected.sort_unstable();
        if actual != expected {
            return Err(CoreError::Index(
                "unsupported catalog column-family layout; expected exactly default/items/paths/roots/meta; preserve the index and use a compatible application or restore a backup (automatic repair was not attempted)".into(),
            ).into());
        }
        let read_only = Db::open_cf_for_read_only(&opts, path, COLUMN_FAMILIES, false)?;
        let meta = read_only.cf_handle(CF_META).ok_or_else(|| {
            CoreError::Index("catalog metadata column family is unavailable".into())
        })?;
        if let Some(bytes) = read_only.get_pinned_cf(&meta, META_SCHEMA_VERSION)? {
            let version: u32 = decode_record(&bytes, 4, "schema version")?;
            if version != CATALOG_SCHEMA_VERSION {
                return Err(CoreError::Index(format!(
                    "unsupported catalog schema version {version}; this application supports {CATALOG_SCHEMA_VERSION}; preserve the index and use a compatible application or restore a backup (automatic repair was not attempted)"
                )).into());
            }
        } else {
            // The original catalog predates a schema marker but has the same v1
            // layout. Validate every record before adopting, without retaining a
            // second catalog in memory or mutating a rejected database.
            validate_legacy_catalog(&read_only)?;
            adopt_legacy = true;
        }
        validate_metadata(&read_only)?;
        if let Some(bytes) = read_only.get_pinned_cf(&meta, META_CATALOG_IDENTITY)? {
            validate_catalog_identity(&bytes)?;
        } else {
            if !adopt_legacy {
                // A versioned catalog that predates identity binding may be adopted only
                // after every authoritative record has been validated read-only. This
                // prevents a malformed catalog from gaining a fresh trusted identity.
                validate_legacy_catalog(&read_only)?;
            }
            adopt_identity = true;
        }
    }
    let cfs = COLUMN_FAMILIES
        .into_iter()
        .map(|name| ColumnFamilyDescriptor::new(name, Options::default()));
    let db = Db::open_cf_descriptors(&opts, path, cfs)?;
    if new_database || adopt_legacy || adopt_identity {
        let meta = db.cf_handle(CF_META).ok_or_else(|| {
            CoreError::Index("catalog metadata column family is unavailable".into())
        })?;
        let mut write_options = rocksdb::WriteOptions::default();
        write_options.set_sync(true);
        let mut batch = WriteBatch::default();
        if new_database || adopt_legacy {
            batch.put_cf(
                &meta,
                META_SCHEMA_VERSION,
                CATALOG_SCHEMA_VERSION.to_le_bytes(),
            );
        }
        if new_database || adopt_legacy || adopt_identity {
            let identity = Uuid::new_v4();
            batch.put_cf(&meta, META_CATALOG_IDENTITY, identity.as_bytes());
        }
        db.write_opt(batch, &write_options).map_err(|error| {
            CoreError::Index(format!(
                "could not persist catalog schema/identity metadata; check index storage and retry: {error}"
            ))
        })?;
    }
    Ok(db)
}

fn decode_record<T: for<'de> Deserialize<'de>>(
    bytes: &[u8],
    maximum: usize,
    context: &str,
) -> Result<T> {
    if bytes.len() > maximum {
        return Err(CoreError::Serialization(format!(
            "catalog {context} record exceeds {maximum} bytes; preserve the index and restore a valid backup"
        )));
    }
    bincode::DefaultOptions::new()
        .with_fixint_encoding()
        .with_limit(bytes.len() as u64)
        .reject_trailing_bytes()
        .deserialize(bytes)
        .map_err(|error| {
            CoreError::Serialization(format!(
                "invalid catalog {context} record: {error}; preserve the index and restore a valid backup"
            ))
        })
}

fn encode_record<T: Serialize>(value: &T, maximum: usize, context: &str) -> Result<Vec<u8>> {
    if bincode::serialized_size(value)? > maximum as u64 {
        return Err(CoreError::Serialization(format!(
            "catalog {context} record exceeds {maximum} bytes; reduce its metadata before retrying"
        )));
    }
    Ok(bincode::serialize(value)?)
}

fn encode_item(item: &ItemRecord) -> Result<Vec<u8>> {
    encode_record(item, MAX_ITEM_RECORD_BYTES, "item")
}

fn encode_root(root: &RootRecord) -> Result<Vec<u8>> {
    encode_record(root, MAX_ROOT_RECORD_BYTES, "root")
}

fn decode_item(key: &[u8], bytes: &[u8]) -> Result<ItemRecord> {
    let item: ItemRecord = decode_record(bytes, MAX_ITEM_RECORD_BYTES, "item")?;
    if key != id_key(item.id) || item.id == 0 {
        return Err(CoreError::Index(
            "catalog item key does not match its positive record ID; preserve the index and restore a valid backup".into(),
        ));
    }
    Ok(item)
}

fn decode_root(key: &[u8], bytes: &[u8]) -> Result<RootRecord> {
    let root: RootRecord = decode_record(bytes, MAX_ROOT_RECORD_BYTES, "root")?;
    if key != root.id.as_bytes() {
        return Err(CoreError::Index(
            "catalog root key does not match its record UUID; preserve the index and restore a valid backup".into(),
        ));
    }
    Ok(root)
}

fn meta_context(key: &[u8]) -> &'static str {
    match key {
        META_SCHEMA_VERSION => "schema version",
        META_NEXT_ID => "next item ID",
        META_CATALOG_GENERATION => "generation",
        META_CATALOG_IDENTITY => "catalog identity",
        META_RECOVERY => "recovery",
        _ => "metadata",
    }
}

fn validate_catalog_identity(bytes: &[u8]) -> Result<Uuid> {
    if bytes.len() != 16 {
        return Err(CoreError::Index(
            "invalid catalog identity; expected one RFC4122 random UUID; preserve the index and restore a valid backup".into(),
        ));
    }
    let identity = Uuid::from_slice(bytes).map_err(|_| {
        CoreError::Index(
            "invalid catalog identity; expected one RFC4122 random UUID; preserve the index and restore a valid backup".into(),
        )
    })?;
    if identity.get_version() != Some(uuid::Version::Random)
        || identity.get_variant() != uuid::Variant::RFC4122
    {
        return Err(CoreError::Index(
            "invalid catalog identity; expected one RFC4122 random UUID; preserve the index and restore a valid backup".into(),
        ));
    }
    Ok(identity)
}

fn validate_metadata(db: &Db) -> Result<()> {
    visit_records(db, CF_META, |key, value| {
        let context = meta_context(key);
        match key {
            META_SCHEMA_VERSION => {
                let _: u32 = decode_record(value, 4, context)?;
            }
            META_NEXT_ID => {
                let _: ItemId = decode_record(value, 4, context)?;
            }
            META_CATALOG_GENERATION => {
                let _: u64 = decode_record(value, 8, context)?;
            }
            META_CATALOG_IDENTITY => {
                validate_catalog_identity(value)?;
            }
            META_RECOVERY => {
                let _: RecoveryMetadata = decode_record(value, MAX_META_RECORD_BYTES, context)?;
            }
            _ => {
                return Err(CoreError::Index(
                    "catalog contains unsupported metadata; preserve the index and use a compatible application".into(),
                ));
            }
        }
        Ok(())
    })
}

fn validate_legacy_catalog(db: &Db) -> Result<()> {
    visit_records(db, "default", |_, _| {
        Err(CoreError::Index(
            "unversioned catalog contains an unsupported default-column record; preserve the index and use a compatible application".into(),
        ))
    })?;
    visit_records(db, CF_ROOTS, |key, value| {
        decode_root(key, value).map(|_| ())
    })?;
    visit_records(db, CF_ITEMS, |key, value| {
        decode_item(key, value).map(|_| ())
    })?;
    visit_records(db, CF_PATHS, |key, value| {
        if key.is_empty()
            || key.len() > MAX_ITEM_RECORD_BYTES
            || std::str::from_utf8(key).is_err()
            || value.len() != std::mem::size_of::<ItemId>()
            || value == id_key(0)
        {
            return Err(CoreError::Index(
                "invalid catalog path-to-item record; preserve the index and restore a valid backup".into(),
            ));
        }
        Ok(())
    })
}

fn visit_records(
    db: &Db,
    name: &str,
    mut visit: impl FnMut(&[u8], &[u8]) -> Result<()>,
) -> Result<()> {
    let cf = db
        .cf_handle(name)
        .ok_or_else(|| CoreError::Index(format!("missing column family: {name}")))?;
    // Raw iterator values are borrowed; enforce size bounds before copying or
    // deserializing an attacker-controlled value into Rust-owned allocations.
    let mut iterator = db.raw_iterator_cf(&cf);
    iterator.seek_to_first();
    while iterator.valid() {
        let (Some(key), Some(value)) = (iterator.key(), iterator.value()) else {
            return Err(CoreError::Index(format!(
                "catalog iterator lost a {name} record; preserve the index and retry"
            )));
        };
        visit(key, value)?;
        iterator.next();
    }
    iterator.status()?;
    Ok(())
}

fn id_key(id: ItemId) -> [u8; 4] {
    id.to_be_bytes()
}

fn backup_corrupt_index(index_dir: &Path) -> Result<Option<PathBuf>> {
    if !index_dir.exists() {
        return Ok(None);
    }
    let parent = index_dir.parent().unwrap_or_else(|| Path::new("."));
    let stamp = chrono::Utc::now().format("%Y%m%dT%H%M%S%3fZ");
    let backup = parent.join(format!("index.corrupt.{stamp}"));
    fs::rename(index_dir, &backup).map_err(|source| CoreError::Filesystem {
        path: index_dir.to_path_buf(),
        source,
    })?;
    Ok(Some(backup))
}

fn cleanup_old_recoveries(state_dir: &Path, keep: usize) {
    let Ok(read) = fs::read_dir(state_dir) else {
        return;
    };
    let mut dirs: Vec<_> = read
        .filter_map(|entry| entry.ok())
        .filter(|entry| {
            entry
                .file_name()
                .to_string_lossy()
                .starts_with("index.corrupt.")
        })
        .collect();
    dirs.sort_by_key(|entry| entry.file_name());
    let remove_count = dirs.len().saturating_sub(keep);
    for entry in dirs.into_iter().take(remove_count) {
        let path = entry.path();
        match entry.file_type() {
            Ok(file_type) if file_type.is_dir() => {
                let _ = fs::remove_dir_all(path);
            }
            Ok(_) => {
                let _ = fs::remove_file(path);
            }
            Err(_) => {
                if fs::remove_dir_all(&path).is_err() {
                    let _ = fs::remove_file(path);
                }
            }
        }
    }
}

fn dir_size(path: &Path) -> std::io::Result<u64> {
    if !path.exists() {
        return Ok(0);
    }
    let mut size = 0u64;
    for entry in walkdir::WalkDir::new(path) {
        let entry = match entry {
            Ok(v) => v,
            Err(_) => continue,
        };
        if entry.file_type().is_file() {
            size = size.saturating_add(entry.metadata()?.len());
        }
    }
    Ok(size)
}
