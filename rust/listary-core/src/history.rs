use crate::{
    error::{CoreError, Result},
    model::{ActivationSource, ItemId, Recommendation},
    text::normalize,
};
use serde::{Deserialize, Serialize};
use std::{
    collections::{HashMap, HashSet},
    fs,
    path::{Path, PathBuf},
};

pub(crate) mod durable_json {
    use crate::error::{CoreError, Result};
    use serde::{Deserialize, Serialize, de::DeserializeOwned};
    use std::{
        fs::{self, File, OpenOptions},
        io::{ErrorKind, Read, Write},
        path::Path,
        time::{SystemTime, UNIX_EPOCH},
    };

    const MAX_DURABLE_JSON_BYTES: u64 = 64 * 1024 * 1024;
    const MAX_RECOVERY_CANDIDATES: usize = 3;

    #[derive(Debug, Deserialize)]
    struct SchemaHeader {
        schema_version: u64,
    }

    #[derive(Debug)]
    pub(super) enum LoadState<T> {
        Missing,
        Corrupt,
        UnsupportedFuture {
            version: u64,
        },
        Valid {
            state: T,
            modified: SystemTime,
            schema_version: u64,
        },
    }

    pub(crate) fn open<T, V>(
        primary: &Path,
        backup: &Path,
        temp: &Path,
        schema_version: u32,
        label: &str,
        validate: V,
    ) -> Result<T>
    where
        T: Default + DeserializeOwned + Serialize,
        V: Fn(T) -> Option<T>,
    {
        open_with_minimum(
            primary,
            backup,
            temp,
            schema_version,
            schema_version,
            label,
            validate,
        )
    }

    pub(crate) fn open_with_minimum<T, V>(
        primary: &Path,
        backup: &Path,
        temp: &Path,
        minimum_schema_version: u32,
        schema_version: u32,
        label: &str,
        validate: V,
    ) -> Result<T>
    where
        T: Default + DeserializeOwned + Serialize,
        V: Fn(T) -> Option<T>,
    {
        let recovery = primary.with_extension("json.recovery");
        let backup_stage = primary.with_extension("json.recovery-backup");

        let primary_missing =
            match load_candidate(primary, minimum_schema_version, schema_version, &validate)? {
                LoadState::Valid { state, .. } => {
                    remove_stale(&[temp, &recovery, &backup_stage]);
                    return Ok(state);
                }
                LoadState::UnsupportedFuture { version } => {
                    return Err(unsupported_schema(label, version, schema_version));
                }
                LoadState::Missing => true,
                LoadState::Corrupt => false,
            };

        let candidates = [
            (temp.to_path_buf(), 0u8, 0u8, false),
            (backup.to_path_buf(), 1u8, 0u8, true),
            (recovery.clone(), 1u8, 1u8, false),
        ];
        debug_assert!(candidates.len() <= MAX_RECOVERY_CANDIDATES);

        let mut any_recovery_present = false;
        let mut backup_valid = false;
        let mut selected: Option<(u8, SystemTime, u8, T)> = None;
        for (candidate, committed, tie_breaker, is_backup) in candidates {
            match load_candidate(
                &candidate,
                minimum_schema_version,
                schema_version,
                &validate,
            )? {
                LoadState::Missing => {}
                LoadState::Corrupt => any_recovery_present = true,
                LoadState::UnsupportedFuture { version } => {
                    return Err(unsupported_schema(label, version, schema_version));
                }
                LoadState::Valid {
                    state, modified, ..
                } => {
                    any_recovery_present = true;
                    backup_valid |= is_backup;
                    let replace = selected.as_ref().is_none_or(
                        |(best_committed, best_modified, best_tie_breaker, _)| {
                            (committed, modified, tie_breaker)
                                > (*best_committed, *best_modified, *best_tie_breaker)
                        },
                    );
                    if replace {
                        selected = Some((committed, modified, tie_breaker, state));
                    }
                }
            }
        }

        let Some((_, _, _, state)) = selected else {
            if primary_missing && !any_recovery_present {
                remove_stale(&[&recovery, &backup_stage]);
                return Ok(T::default());
            }
            return Err(CoreError::Serialization(format!(
                "{label} settings are corrupt or invalid and no valid recovery generation exists; restore the file from a known-good backup"
            )));
        };

        repair_primary(
            primary,
            backup,
            temp,
            &recovery,
            &backup_stage,
            backup_valid,
            &state,
        )?;
        Ok(state)
    }

    pub(super) fn load_candidate<T, V>(
        path: &Path,
        minimum_schema_version: u32,
        schema_version: u32,
        validate: &V,
    ) -> Result<LoadState<T>>
    where
        T: DeserializeOwned,
        V: Fn(T) -> Option<T>,
    {
        let mut file = match File::open(path) {
            Ok(file) => file,
            Err(source) if source.kind() == ErrorKind::NotFound => return Ok(LoadState::Missing),
            Err(source) => {
                return Err(CoreError::Filesystem {
                    path: path.to_path_buf(),
                    source,
                });
            }
        };
        let metadata = file.metadata().map_err(|source| CoreError::Filesystem {
            path: path.to_path_buf(),
            source,
        })?;
        let modified = metadata.modified().unwrap_or(UNIX_EPOCH);
        if !metadata.is_file() || metadata.len() > MAX_DURABLE_JSON_BYTES {
            return Ok(LoadState::Corrupt);
        }

        let mut bytes = Vec::new();
        Read::by_ref(&mut file)
            .take(MAX_DURABLE_JSON_BYTES + 1)
            .read_to_end(&mut bytes)
            .map_err(|source| CoreError::Filesystem {
                path: path.to_path_buf(),
                source,
            })?;
        if bytes.len() as u64 > MAX_DURABLE_JSON_BYTES {
            return Ok(LoadState::Corrupt);
        }
        let Ok(header) = serde_json::from_slice::<SchemaHeader>(&bytes) else {
            return Ok(LoadState::Corrupt);
        };
        if header.schema_version > u64::from(schema_version) {
            return Ok(LoadState::UnsupportedFuture {
                version: header.schema_version,
            });
        }
        if header.schema_version < u64::from(minimum_schema_version) {
            return Ok(LoadState::Corrupt);
        }
        let Ok(state) = serde_json::from_slice::<T>(&bytes) else {
            return Ok(LoadState::Corrupt);
        };
        let Some(state) = validate(state) else {
            return Ok(LoadState::Corrupt);
        };
        Ok(LoadState::Valid {
            state,
            modified,
            schema_version: header.schema_version,
        })
    }

    pub(super) fn unsupported_schema(label: &str, found: u64, supported: u32) -> CoreError {
        CoreError::Serialization(format!(
            "{label} settings use schema version {found}, but this build supports version {supported}; upgrade the application before opening this state directory"
        ))
    }

    fn repair_primary<T: Serialize>(
        primary: &Path,
        backup: &Path,
        temp: &Path,
        recovery: &Path,
        backup_stage: &Path,
        backup_valid: bool,
        state: &T,
    ) -> Result<()> {
        let bytes = serde_json::to_vec_pretty(state)?;
        write_synced(recovery, &bytes)?;

        if !backup_valid {
            write_synced(backup_stage, &bytes)?;
            publish_stage(backup_stage, backup)?;
        }
        publish_stage(recovery, primary)?;
        sync_parent(primary)?;
        remove_stale(&[temp, recovery, backup_stage]);
        Ok(())
    }

    pub(super) fn write_synced(path: &Path, bytes: &[u8]) -> Result<()> {
        let mut file = OpenOptions::new()
            .create(true)
            .truncate(true)
            .write(true)
            .open(path)
            .map_err(|source| CoreError::Filesystem {
                path: path.to_path_buf(),
                source,
            })?;
        file.write_all(bytes)
            .and_then(|()| file.sync_all())
            .map_err(|source| CoreError::Filesystem {
                path: path.to_path_buf(),
                source,
            })
    }

    #[cfg(windows)]
    pub(super) fn publish_stage(stage: &Path, target: &Path) -> Result<()> {
        use std::os::windows::ffi::OsStrExt;

        #[link(name = "Kernel32")]
        unsafe extern "system" {
            #[link_name = "MoveFileExW"]
            fn move_file_ex_w(
                existing_file_name: *const u16,
                new_file_name: *const u16,
                flags: u32,
            ) -> i32;
        }

        const MOVEFILE_REPLACE_EXISTING: u32 = 0x1;
        const MOVEFILE_WRITE_THROUGH: u32 = 0x8;
        let stage_wide: Vec<u16> = stage.as_os_str().encode_wide().chain(Some(0)).collect();
        let target_wide: Vec<u16> = target.as_os_str().encode_wide().chain(Some(0)).collect();
        // SAFETY: both buffers are owned, NUL-terminated UTF-16 strings and
        // remain alive for the duration of the synchronous Win32 call.
        let published = unsafe {
            move_file_ex_w(
                stage_wide.as_ptr(),
                target_wide.as_ptr(),
                MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH,
            )
        };
        if published == 0 {
            return Err(CoreError::Filesystem {
                path: target.to_path_buf(),
                source: std::io::Error::last_os_error(),
            });
        }
        Ok(())
    }

    #[cfg(not(windows))]
    pub(super) fn publish_stage(stage: &Path, target: &Path) -> Result<()> {
        fs::rename(stage, target).map_err(|source| CoreError::Filesystem {
            path: target.to_path_buf(),
            source,
        })
    }

    #[cfg(unix)]
    pub(super) fn sync_parent(path: &Path) -> Result<()> {
        let parent = path.parent().unwrap_or_else(|| Path::new("."));
        File::open(parent)
            .and_then(|directory| directory.sync_all())
            .map_err(|source| CoreError::Filesystem {
                path: parent.to_path_buf(),
                source,
            })
    }

    #[cfg(not(unix))]
    pub(super) fn sync_parent(_path: &Path) -> Result<()> {
        Ok(())
    }

    pub(super) fn remove_stale(paths: &[&Path]) {
        for path in paths {
            match fs::remove_file(path) {
                Ok(()) => {}
                Err(source) if source.kind() == ErrorKind::NotFound => {}
                Err(_) => {}
            }
        }
    }
}

const HISTORY_SCHEMA_VERSION: u32 = 2;
const MAX_HISTORY_ENTRIES: usize = 20_000;
const MAX_HIDDEN_IDS: usize = 5_000;
const MAX_EXCLUDED_PREFIXES: usize = 1_000;

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize, Default)]
pub struct HistoryEntry {
    pub activation_count: u32,
    pub last_activated_unix_ms: i64,
    #[serde(default)]
    pub last_source: ActivationSource,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
struct HistoryFile {
    schema_version: u32,
    // This reservation survives entry pruning, portable replacement and recovery.
    // Catalog reconstruction must never attach old history to a reused item ID.
    #[serde(default)]
    item_id_high_water: Option<ItemId>,
    entries: HashMap<ItemId, HistoryEntry>,
    hidden_ids: HashSet<ItemId>,
    excluded_path_prefixes: Vec<String>,
}

impl Default for HistoryFile {
    fn default() -> Self {
        Self {
            schema_version: HISTORY_SCHEMA_VERSION,
            item_id_high_water: None,
            entries: HashMap::new(),
            hidden_ids: HashSet::new(),
            excluded_path_prefixes: Vec::new(),
        }
    }
}

#[derive(Debug)]
pub struct HistoryStore {
    path: PathBuf,
    state: HistoryFile,
}

impl HistoryStore {
    pub fn open(state_dir: &Path) -> Result<Self> {
        fs::create_dir_all(state_dir).map_err(|source| CoreError::Filesystem {
            path: state_dir.to_path_buf(),
            source,
        })?;
        let path = state_dir.join("history.json");
        let state = open_history_file(&path)?;
        Ok(Self { path, state })
    }

    pub fn len(&self) -> usize {
        self.state.entries.len()
    }

    pub(crate) fn max_referenced_item_id(&self) -> Option<ItemId> {
        self.state.item_id_high_water
    }

    pub(crate) fn portable_snapshot(
        &self,
    ) -> (Vec<(ItemId, HistoryEntry)>, Vec<ItemId>, Vec<String>) {
        let mut entries: Vec<_> = self
            .state
            .entries
            .iter()
            .map(|(id, entry)| (*id, entry.clone()))
            .collect();
        entries.sort_by_key(|(id, _)| *id);
        let mut hidden_ids: Vec<_> = self.state.hidden_ids.iter().copied().collect();
        hidden_ids.sort_unstable();
        (
            entries,
            hidden_ids,
            self.state.excluded_path_prefixes.clone(),
        )
    }

    pub(crate) fn excluded_path_prefixes_snapshot(&self) -> Vec<String> {
        self.state.excluded_path_prefixes.clone()
    }

    pub(crate) fn remove_items(&mut self, item_ids: &HashSet<ItemId>) -> Result<()> {
        if item_ids.is_empty() {
            return Ok(());
        }
        let mut next = self.state.clone();
        let before_entries = next.entries.len();
        let before_hidden = next.hidden_ids.len();
        next.entries.retain(|id, _| !item_ids.contains(id));
        next.hidden_ids.retain(|id| !item_ids.contains(id));
        if next.entries.len() == before_entries && next.hidden_ids.len() == before_hidden {
            return Ok(());
        }
        persist_history_file(&self.path, &self.state, &next)?;
        self.state = next;
        Ok(())
    }

    pub(crate) fn replace_portable_state(
        &mut self,
        entries: HashMap<ItemId, HistoryEntry>,
        hidden_ids: HashSet<ItemId>,
        excluded_path_prefixes: Vec<String>,
    ) -> Result<()> {
        if entries.len() > MAX_HISTORY_ENTRIES {
            return Err(CoreError::InvalidRequest(format!(
                "too many portable history entries; maximum is {MAX_HISTORY_ENTRIES}"
            )));
        }
        if hidden_ids.len() > MAX_HIDDEN_IDS {
            return Err(CoreError::InvalidRequest(format!(
                "too many portable hidden items; maximum is {MAX_HIDDEN_IDS}"
            )));
        }
        let mut normalized_prefixes = Vec::new();
        let mut seen_prefixes = HashSet::new();
        for prefix in excluded_path_prefixes {
            let prefix = normalize_path_prefix(&prefix);
            if prefix.is_empty() {
                continue;
            }
            if seen_prefixes.insert(prefix.clone()) {
                normalized_prefixes.push(prefix);
            }
        }
        if normalized_prefixes.len() > MAX_EXCLUDED_PREFIXES {
            return Err(CoreError::InvalidRequest(format!(
                "too many portable excluded path prefixes; maximum is {MAX_EXCLUDED_PREFIXES}"
            )));
        }

        let next = HistoryFile {
            schema_version: HISTORY_SCHEMA_VERSION,
            item_id_high_water: self
                .state
                .item_id_high_water
                .max(entries.keys().chain(hidden_ids.iter()).copied().max()),
            entries,
            hidden_ids,
            excluded_path_prefixes: normalized_prefixes,
        };
        persist_history_file(&self.path, &self.state, &next)?;
        self.state = next;
        Ok(())
    }

    pub fn record_activation_with_source(
        &mut self,
        item_id: ItemId,
        now_unix_ms: i64,
        source: ActivationSource,
    ) -> Result<()> {
        let mut next = self.state.clone();
        next.item_id_high_water = next.item_id_high_water.max(Some(item_id));
        let entry = next.entries.entry(item_id).or_default();
        entry.activation_count = entry.activation_count.saturating_add(1);
        entry.last_activated_unix_ms = now_unix_ms;
        entry.last_source = source;
        Self::prune(&mut next);
        persist_history_file(&self.path, &self.state, &next)?;
        self.state = next;
        Ok(())
    }

    pub fn hide(&mut self, item_id: ItemId) -> Result<()> {
        let mut next = self.state.clone();
        next.item_id_high_water = next.item_id_high_water.max(Some(item_id));
        next.hidden_ids.insert(item_id);
        while next.hidden_ids.len() > MAX_HIDDEN_IDS {
            if let Some(id) = next.hidden_ids.iter().next().copied() {
                next.hidden_ids.remove(&id);
            }
        }
        persist_history_file(&self.path, &self.state, &next)?;
        self.state = next;
        Ok(())
    }

    pub fn exclude_path_prefix(&mut self, prefix: String) -> Result<()> {
        let mut next = self.state.clone();
        let prefix = normalize_path_prefix(&prefix);
        if !prefix.is_empty() && !next.excluded_path_prefixes.contains(&prefix) {
            next.excluded_path_prefixes.push(prefix);
        }
        if next.excluded_path_prefixes.len() > MAX_EXCLUDED_PREFIXES {
            let drop_count = next.excluded_path_prefixes.len() - MAX_EXCLUDED_PREFIXES;
            next.excluded_path_prefixes.drain(0..drop_count);
        }
        persist_history_file(&self.path, &self.state, &next)?;
        self.state = next;
        Ok(())
    }

    pub fn is_hidden(&self, item_id: ItemId) -> bool {
        self.state.hidden_ids.contains(&item_id)
    }

    pub fn is_path_excluded(&self, path: &str) -> bool {
        let path = normalize_path_prefix(path);
        normalized_path_is_excluded(&path, &self.state.excluded_path_prefixes)
    }

    pub fn history_boost(&self, item_id: ItemId, now_unix_ms: i64) -> (i32, i32) {
        let Some(entry) = self.state.entries.get(&item_id) else {
            return (0, 0);
        };
        let frequency = ((entry.activation_count.min(50) as f64).ln_1p() * 90.0) as i32;
        let age_ms = now_unix_ms
            .saturating_sub(entry.last_activated_unix_ms)
            .max(0);
        let day_ms = 86_400_000i64;
        let days = age_ms as f64 / day_ms as f64;
        let recency = (220.0 * (-days / 14.0).exp()).round() as i32;
        (frequency.min(350), recency.clamp(0, 220))
    }

    pub(crate) fn recommendation_for_item(
        &self,
        item: crate::model::ItemRecord,
        now_unix_ms: i64,
    ) -> Recommendation {
        let entry = self.state.entries.get(&item.id);
        let (history, recency) = self.history_boost(item.id, now_unix_ms);
        Recommendation {
            score: i64::from(history + recency + item.priority.boost())
                + i64::from(entry.is_some_and(|entry| entry.last_activated_unix_ms > 0)),
            activation_count: entry.map_or(0, |entry| entry.activation_count),
            last_activated_unix_ms: entry.map_or(0, |entry| entry.last_activated_unix_ms),
            last_source: entry.map(|entry| entry.last_source).unwrap_or_default(),
            item,
        }
    }

    pub fn priority_item_ids(&self, now_unix_ms: i64, limit: usize) -> Vec<ItemId> {
        let mut scored: Vec<_> = self
            .state
            .entries
            .iter()
            .map(|(id, entry)| {
                let (history, recency) = self.history_boost(*id, now_unix_ms);
                (*id, history + recency, entry.last_activated_unix_ms)
            })
            .collect();
        scored.sort_unstable_by(|a, b| {
            b.1.cmp(&a.1)
                .then_with(|| b.2.cmp(&a.2))
                .then_with(|| a.0.cmp(&b.0))
        });
        scored.into_iter().take(limit).map(|row| row.0).collect()
    }

    pub fn recommendations_matching<F, P>(
        &self,
        now_unix_ms: i64,
        limit: usize,
        mut lookup: F,
        mut matches: P,
    ) -> Vec<Recommendation>
    where
        F: FnMut(ItemId) -> Option<crate::model::ItemRecord>,
        P: FnMut(&crate::model::ItemRecord) -> bool,
    {
        let mut out: Vec<_> = self
            .state
            .entries
            .iter()
            .filter(|(id, _)| !self.is_hidden(**id))
            .filter_map(|(id, entry)| {
                let item = lookup(*id)?;
                if self.is_path_excluded(&item.path) || !matches(&item) {
                    return None;
                }
                let mut recommendation = self.recommendation_for_item(item, now_unix_ms);
                recommendation.activation_count = entry.activation_count;
                recommendation.last_activated_unix_ms = entry.last_activated_unix_ms;
                recommendation.last_source = entry.last_source;
                Some(recommendation)
            })
            .collect();
        out.sort_by(|a, b| {
            b.score
                .cmp(&a.score)
                .then_with(|| b.last_activated_unix_ms.cmp(&a.last_activated_unix_ms))
                .then_with(|| a.item.path.cmp(&b.item.path))
        });
        out.truncate(limit);
        out
    }

    fn prune(state: &mut HistoryFile) {
        if state.entries.len() <= MAX_HISTORY_ENTRIES {
            return;
        }
        let mut by_oldest: Vec<_> = state
            .entries
            .iter()
            .map(|(id, e)| (*id, e.last_activated_unix_ms))
            .collect();
        by_oldest.sort_by_key(|(_, ts)| *ts);
        let remove_count = state.entries.len() - MAX_HISTORY_ENTRIES;
        for (id, _) in by_oldest.into_iter().take(remove_count) {
            state.entries.remove(&id);
        }
    }
}

fn open_history_file(path: &Path) -> Result<HistoryFile> {
    use durable_json::LoadState;

    let backup = path.with_extension("json.bak");
    let temp = path.with_extension("json.tmp");
    let recovery = path.with_extension("json.recovery");
    let backup_stage = path.with_extension("json.recovery-backup");
    let candidates = [
        (path, 2u8, 0u8),
        (backup.as_path(), 1, 0),
        (temp.as_path(), 0, 0),
        (recovery.as_path(), 1, 1),
        (backup_stage.as_path(), 0, 1),
    ];
    let mut selected = None;
    let mut primary_generation = None;
    let mut backup_generation = None;
    let mut reservations = HashMap::new();
    let mut high_water = None;
    let mut any_present = false;

    // Read all bounded generations before cleanup, including abandoned writes.
    // Only their ID reservations merge; a valid primary keeps its own content.
    for (candidate, committed, tie_breaker) in candidates {
        match durable_json::load_candidate(
            candidate,
            1,
            HISTORY_SCHEMA_VERSION,
            &validate_history_file,
        )? {
            LoadState::Missing => {}
            LoadState::Corrupt => any_present = true,
            LoadState::UnsupportedFuture { version } => {
                return Err(durable_json::unsupported_schema(
                    "history",
                    version,
                    HISTORY_SCHEMA_VERSION,
                ));
            }
            LoadState::Valid {
                state,
                modified,
                schema_version,
            } => {
                any_present = true;
                high_water = high_water.max(state.item_id_high_water);
                reservations.insert(candidate, state.item_id_high_water);
                if candidate == path {
                    primary_generation = Some((schema_version, state.item_id_high_water));
                } else if candidate == backup {
                    backup_generation = Some((schema_version, state.clone()));
                }
                let rank = (committed, modified, tie_breaker);
                if selected
                    .as_ref()
                    .is_none_or(|(best_rank, _)| rank > *best_rank)
                {
                    selected = Some((rank, state));
                }
            }
        }
    }
    let Some((_, mut state)) = selected else {
        if !any_present {
            return Ok(HistoryFile::default());
        }
        return Err(CoreError::Serialization(
            "history settings are corrupt or invalid and no valid recovery generation exists; restore the file from a known-good backup".into(),
        ));
    };
    state.item_id_high_water = high_water;
    let current_version = u64::from(HISTORY_SCHEMA_VERSION);
    if primary_generation != Some((current_version, high_water)) {
        // Never truncate the sole surviving reservation while migrating it.
        // At least one of the three staging paths is absent, lower, or redundant.
        let reserve_copies = reservations
            .values()
            .filter(|value| **value == high_water)
            .count();
        let stage = [recovery.as_path(), temp.as_path(), backup_stage.as_path()]
            .into_iter()
            .find(|candidate| {
                reserve_copies > 1 || reservations.get(candidate) != Some(&high_water)
            })
            .ok_or_else(|| {
                CoreError::Serialization("history recovery has no safe staging path".into())
            })?;
        durable_json::write_synced(stage, &serde_json::to_vec_pretty(&state)?)?;
        crate::test_fault::hit("history_after_reservation_stage");
        durable_json::publish_stage(stage, path)?;
        durable_json::sync_parent(path)?;
        crate::test_fault::hit("history_after_reservation_primary");
    }
    if backup_generation
        .as_ref()
        .map(|(version, backup)| (*version, backup.item_id_high_water))
        != Some((current_version, high_water))
    {
        let mut backup_state = backup_generation.map_or_else(|| state.clone(), |(_, state)| state);
        backup_state.item_id_high_water = high_water;
        durable_json::write_synced(&backup_stage, &serde_json::to_vec_pretty(&backup_state)?)?;
        durable_json::publish_stage(&backup_stage, &backup)?;
        durable_json::sync_parent(path)?;
    }
    durable_json::remove_stale(&[&temp, &recovery, &backup_stage]);
    Ok(state)
}

fn persist_history_file(path: &Path, previous: &HistoryFile, state: &HistoryFile) -> Result<()> {
    let bytes = serde_json::to_vec_pretty(state)?;
    let temp = path.with_extension("json.tmp");
    let backup = path.with_extension("json.bak");
    let backup_stage = path.with_extension("json.recovery-backup");
    durable_json::write_synced(&temp, &bytes)?;
    crate::test_fault::hit("history_after_temp_write");
    let primary_exists = path.try_exists().map_err(|source| CoreError::Filesystem {
        path: path.to_path_buf(),
        source,
    })?;
    let mut backup_state = if primary_exists { previous } else { state }.clone();
    backup_state.item_id_high_water = backup_state
        .item_id_high_water
        .max(state.item_id_high_water);
    durable_json::write_synced(&backup_stage, &serde_json::to_vec_pretty(&backup_state)?)?;
    durable_json::publish_stage(&backup_stage, &backup)?;
    durable_json::sync_parent(path)?;
    crate::test_fault::hit("history_after_backup_copy");
    if primary_exists {
        fs::remove_file(path).map_err(|source| CoreError::Filesystem {
            path: path.to_path_buf(),
            source,
        })?;
    }
    crate::test_fault::hit("history_after_target_remove");
    durable_json::publish_stage(&temp, path)?;
    durable_json::sync_parent(path)?;
    Ok(())
}

fn validate_history_file(mut state: HistoryFile) -> Option<HistoryFile> {
    if !(1..=HISTORY_SCHEMA_VERSION).contains(&state.schema_version)
        || state.entries.len() > MAX_HISTORY_ENTRIES
        || state.hidden_ids.len() > MAX_HIDDEN_IDS
        || state.excluded_path_prefixes.len() > MAX_EXCLUDED_PREFIXES
    {
        return None;
    }
    let referenced = state
        .entries
        .keys()
        .chain(state.hidden_ids.iter())
        .copied()
        .max();
    if state.schema_version == 1 {
        state.schema_version = HISTORY_SCHEMA_VERSION;
        state.item_id_high_water = state.item_id_high_water.max(referenced);
    } else if state.item_id_high_water < referenced {
        return None;
    }
    Some(state)
}

pub(crate) fn normalize_path_prefix(path: &str) -> String {
    normalize(path.trim_end_matches(['/', '\\']))
}

pub(crate) fn normalized_path_is_excluded(path: &str, excluded_prefixes: &[String]) -> bool {
    excluded_prefixes.iter().any(|prefix| {
        path == prefix
            || path
                .strip_prefix(prefix)
                .is_some_and(|rest| rest.starts_with('/') || rest.starts_with('\\'))
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::TempDir;

    fn history_with(item_id: ItemId, count: u32) -> HistoryFile {
        HistoryFile {
            schema_version: HISTORY_SCHEMA_VERSION,
            item_id_high_water: Some(item_id),
            entries: HashMap::from([(
                item_id,
                HistoryEntry {
                    activation_count: count,
                    last_activated_unix_ms: 100,
                    last_source: ActivationSource::Launcher,
                },
            )]),
            hidden_ids: HashSet::new(),
            excluded_path_prefixes: Vec::new(),
        }
    }

    #[test]
    fn failed_activation_persistence_does_not_mutate_memory_or_double_count_retry() {
        let state = TempDir::new().unwrap();
        let mut store = HistoryStore::open(state.path()).unwrap();
        let temp = state.path().join("history.json.tmp");
        fs::create_dir(&temp).unwrap();

        assert!(
            store
                .record_activation_with_source(7, 100, ActivationSource::Launcher)
                .is_err()
        );
        assert_eq!(store.len(), 0);
        assert!(!store.state.entries.contains_key(&7));
        assert_eq!(store.max_referenced_item_id(), None);

        fs::remove_dir(&temp).unwrap();
        store
            .record_activation_with_source(7, 200, ActivationSource::FileSearch)
            .unwrap();
        assert_eq!(store.state.entries[&7].activation_count, 1);
        drop(store);

        let reopened = HistoryStore::open(state.path()).unwrap();
        assert_eq!(reopened.state.entries[&7].activation_count, 1);
        assert_eq!(reopened.state.entries[&7].last_activated_unix_ms, 200);
        assert_eq!(
            reopened.state.entries[&7].last_source,
            ActivationSource::FileSearch
        );
        assert_eq!(reopened.max_referenced_item_id(), Some(7));
    }

    fn write_legacy(path: &Path, item_id: ItemId, hidden_id: ItemId) {
        let mut state = serde_json::to_value(history_with(item_id, 1)).unwrap();
        state["schema_version"] = serde_json::json!(1);
        state["hidden_ids"] = serde_json::json!([hidden_id]);
        state.as_object_mut().unwrap().remove("item_id_high_water");
        fs::write(path, serde_json::to_vec_pretty(&state).unwrap()).unwrap();
    }

    fn read_history(path: &Path) -> HistoryFile {
        serde_json::from_slice(&fs::read(path).unwrap()).unwrap()
    }

    #[test]
    fn legacy_migration_reserves_every_generation_without_replacing_primary_content() {
        for highest_generation in ["bak", "tmp", "recovery", "recovery-backup"] {
            let dir = TempDir::new().unwrap();
            let primary = dir.path().join("history.json");
            let backup = primary.with_extension("json.bak");
            write_legacy(&primary, 10, 20);
            write_legacy(&backup, 100, 200);
            write_legacy(
                &primary.with_extension(format!("json.{highest_generation}")),
                500,
                900,
            );

            let store = HistoryStore::open(dir.path()).unwrap();
            assert_eq!(store.max_referenced_item_id(), Some(900));
            assert_eq!(
                store.state.entries.keys().copied().collect::<Vec<_>>(),
                [10]
            );
            assert_eq!(store.state.hidden_ids, HashSet::from([20]));
            let persisted = read_history(&primary);
            let recovered = read_history(&backup);
            assert_eq!(persisted.schema_version, HISTORY_SCHEMA_VERSION);
            assert_eq!(persisted.item_id_high_water, Some(900));
            assert_eq!(recovered.schema_version, HISTORY_SCHEMA_VERSION);
            assert_eq!(recovered.item_id_high_water, Some(900));
            let backup_item = if highest_generation == "bak" {
                500
            } else {
                100
            };
            assert!(recovered.entries.contains_key(&backup_item));
            for extension in ["json.tmp", "json.recovery", "json.recovery-backup"] {
                assert!(!primary.with_extension(extension).exists());
            }
            drop(store);
            assert_eq!(
                HistoryStore::open(dir.path())
                    .unwrap()
                    .max_referenced_item_id(),
                Some(900)
            );
        }
    }

    #[test]
    fn replacement_removal_and_pruning_keep_the_high_water_reservation() {
        let dir = TempDir::new().unwrap();
        let mut store = HistoryStore::open(dir.path()).unwrap();
        store
            .record_activation_with_source(900, 100, ActivationSource::Launcher)
            .unwrap();
        store.hide(1000).unwrap();
        store
            .replace_portable_state(history_with(5, 1).entries, HashSet::new(), vec![])
            .unwrap();
        assert_eq!(store.max_referenced_item_id(), Some(1000));
        assert!(!store.is_hidden(1000));
        assert!(!store.state.entries.contains_key(&900));
        store.remove_items(&HashSet::from([5])).unwrap();
        assert_eq!(store.max_referenced_item_id(), Some(1000));
        assert_eq!(store.len(), 0);
        drop(store);
        let store = HistoryStore::open(dir.path()).unwrap();
        assert_eq!(store.max_referenced_item_id(), Some(1000));
        assert_eq!(
            read_history(&dir.path().join("history.json.bak")).item_id_high_water,
            Some(1000)
        );

        let prune_dir = TempDir::new().unwrap();
        let mut state = HistoryFile::default();
        for id in 1..=MAX_HISTORY_ENTRIES as ItemId {
            state.entries.insert(
                id,
                HistoryEntry {
                    last_activated_unix_ms: 100,
                    ..HistoryEntry::default()
                },
            );
        }
        state.item_id_high_water = Some(MAX_HISTORY_ENTRIES as ItemId);
        let mut store = HistoryStore {
            path: prune_dir.path().join("history.json"),
            state,
        };
        let pruned_id = MAX_HISTORY_ENTRIES as ItemId + 1;
        store
            .record_activation_with_source(pruned_id, -1, ActivationSource::Launcher)
            .unwrap();
        assert_eq!(store.len(), MAX_HISTORY_ENTRIES);
        assert!(!store.state.entries.contains_key(&pruned_id));
        assert_eq!(store.max_referenced_item_id(), Some(pruned_id));
        drop(store);
        assert_eq!(
            HistoryStore::open(prune_dir.path())
                .unwrap()
                .max_referenced_item_id(),
            Some(pruned_id)
        );
    }

    #[test]
    fn older_backup_content_retains_newer_reservations_after_primary_corruption() {
        let dir = TempDir::new().unwrap();
        let mut store = HistoryStore::open(dir.path()).unwrap();
        store
            .record_activation_with_source(100, 100, ActivationSource::Launcher)
            .unwrap();
        store
            .record_activation_with_source(900, 200, ActivationSource::FileSearch)
            .unwrap();
        let backup = read_history(&dir.path().join("history.json.bak"));
        assert!(backup.entries.contains_key(&100));
        assert!(!backup.entries.contains_key(&900));
        assert_eq!(backup.item_id_high_water, Some(900));
        drop(store);
        fs::write(dir.path().join("history.json"), b"corrupt").unwrap();
        let mut recovered = HistoryStore::open(dir.path()).unwrap();
        assert_eq!(recovered.max_referenced_item_id(), Some(900));
        assert!(!recovered.state.entries.contains_key(&900));
        recovered
            .replace_portable_state(HashMap::new(), HashSet::new(), vec![])
            .unwrap();
        drop(recovered);
        fs::write(dir.path().join("history.json"), b"corrupt-again").unwrap();
        assert_eq!(
            HistoryStore::open(dir.path())
                .unwrap()
                .max_referenced_item_id(),
            Some(900)
        );
    }

    #[test]
    fn failed_backup_publication_preserves_memory_and_primary_then_reserves_staged_ids() {
        let dir = TempDir::new().unwrap();
        let mut store = HistoryStore::open(dir.path()).unwrap();
        store
            .record_activation_with_source(100, 100, ActivationSource::Launcher)
            .unwrap();
        let primary = dir.path().join("history.json");
        let original = fs::read(&primary).unwrap();
        let backup = dir.path().join("history.json.bak");
        fs::remove_file(&backup).unwrap();
        fs::create_dir(&backup).unwrap();
        assert!(
            store
                .record_activation_with_source(900, 200, ActivationSource::Launcher)
                .is_err()
        );
        assert_eq!(store.max_referenced_item_id(), Some(100));
        assert!(!store.state.entries.contains_key(&900));
        assert_eq!(fs::read(&primary).unwrap(), original);
        assert!(store.hide(1100).is_err());
        assert_eq!(store.max_referenced_item_id(), Some(100));
        assert!(!store.is_hidden(1100));
        drop(store);
        fs::remove_dir(&backup).unwrap();
        let recovered = HistoryStore::open(dir.path()).unwrap();
        assert_eq!(recovered.max_referenced_item_id(), Some(1100));
        assert!(recovered.state.entries.contains_key(&100));
        assert!(!recovered.is_hidden(1100));
    }

    #[test]
    fn future_history_generation_blocks_open_before_any_migration_or_cleanup() {
        for extension in [
            "json",
            "json.bak",
            "json.tmp",
            "json.recovery",
            "json.recovery-backup",
        ] {
            let dir = TempDir::new().unwrap();
            let primary = dir.path().join("history.json");
            write_legacy(&primary, 10, 20);
            let future_path = primary.with_extension(extension);
            let future = serde_json::to_vec_pretty(&HistoryFile {
                schema_version: HISTORY_SCHEMA_VERSION + 1,
                ..history_with(900, 1)
            })
            .unwrap();
            fs::write(&future_path, &future).unwrap();
            let original = fs::read(&primary).unwrap();
            let error = HistoryStore::open(dir.path()).unwrap_err();
            assert!(error.to_string().contains("upgrade the application"));
            assert_eq!(fs::read(&primary).unwrap(), original);
            assert_eq!(fs::read(&future_path).unwrap(), future);
        }
    }

    #[test]
    fn history_validation_rejects_oversized_legacy_and_inconsistent_reservation() {
        let mut oversized = history_with(10, 1);
        oversized.schema_version = 1;
        oversized.hidden_ids = (0..=MAX_HIDDEN_IDS as ItemId).collect();
        assert!(validate_history_file(oversized).is_none());
        let inconsistent = HistoryFile {
            item_id_high_water: Some(9),
            ..history_with(10, 1)
        };
        assert!(validate_history_file(inconsistent).is_none());
    }

    #[cfg(feature = "test-fault-injection")]
    #[test]
    fn history_reservation_survives_interrupted_writes_and_migration() {
        for failpoint in [
            "history_after_temp_write",
            "history_after_backup_copy",
            "history_after_target_remove",
            "history_after_reservation_stage",
            "history_after_reservation_primary",
        ] {
            let dir = TempDir::new().unwrap();
            let primary = dir.path().join("history.json");
            let migration = failpoint.starts_with("history_after_reservation_");
            if migration {
                write_legacy(&primary, 100, 200);
                write_legacy(&primary.with_extension("json.bak"), 500, 600);
                write_legacy(&primary.with_extension("json.recovery"), 900, 1200);
            } else {
                let mut store = HistoryStore::open(dir.path()).unwrap();
                store
                    .record_activation_with_source(100, 100, ActivationSource::Launcher)
                    .unwrap();
            }
            let marker = dir.path().join("fault.marker");
            let output = std::process::Command::new(std::env::current_exe().unwrap())
                .args([
                    "--exact",
                    "history::tests::history_reservation_fault_child",
                    "--nocapture",
                ])
                .env("LISTARY_HISTORY_TEST_STATE_DIR", dir.path())
                .env("LISTARY_CORE_TEST_FAILPOINT", failpoint)
                .env("LISTARY_CORE_TEST_FAILPOINT_MARKER", &marker)
                .output()
                .unwrap();
            assert_eq!(
                output.status.code(),
                Some(86),
                "{failpoint}: {}",
                String::from_utf8_lossy(&output.stderr)
            );
            assert_eq!(fs::read_to_string(marker).unwrap(), failpoint);
            let recovered = HistoryStore::open(dir.path()).unwrap();
            let expected = Some(if migration { 1200 } else { 900 });
            assert_eq!(recovered.max_referenced_item_id(), expected, "{failpoint}");
            assert!(recovered.state.entries.contains_key(&100));
            assert!(!recovered.state.entries.contains_key(&900));
            assert_eq!(
                read_history(&primary.with_extension("json.bak")).item_id_high_water,
                expected
            );
        }
    }

    #[cfg(feature = "test-fault-injection")]
    #[test]
    fn history_reservation_fault_child() {
        let Some(dir) = std::env::var_os("LISTARY_HISTORY_TEST_STATE_DIR") else {
            return;
        };
        let mut store = HistoryStore::open(Path::new(&dir)).unwrap();
        store
            .record_activation_with_source(900, 200, ActivationSource::Launcher)
            .unwrap();
        panic!("history reservation failpoint was not reached");
    }

    #[test]
    fn durable_history_recovery_is_typed_and_preserves_evidence() {
        let future_state = TempDir::new().unwrap();
        let future_primary = future_state.path().join("history.json");
        let future_backup = future_state.path().join("history.json.bak");
        let future_temp = future_state.path().join("history.json.tmp");
        let valid = serde_json::to_vec_pretty(&history_with(11, 1)).unwrap();
        let future = serde_json::to_vec_pretty(&HistoryFile {
            schema_version: HISTORY_SCHEMA_VERSION + 1,
            ..history_with(11, 9)
        })
        .unwrap();
        fs::write(&future_primary, &future).unwrap();
        fs::write(&future_backup, &valid).unwrap();
        fs::write(&future_temp, &valid).unwrap();
        let error = HistoryStore::open(future_state.path()).unwrap_err();
        assert!(error.to_string().contains("upgrade the application"));
        assert_eq!(fs::read(&future_primary).unwrap(), future);
        assert_eq!(fs::read(&future_backup).unwrap(), valid);
        assert_eq!(fs::read(&future_temp).unwrap(), valid);

        let recovered_state = TempDir::new().unwrap();
        let recovered_primary = recovered_state.path().join("history.json");
        let recovered_backup = recovered_state.path().join("history.json.bak");
        fs::write(&recovered_primary, b"{broken-primary").unwrap();
        fs::write(&recovered_backup, &valid).unwrap();
        let recovered = HistoryStore::open(recovered_state.path()).unwrap();
        assert_eq!(recovered.state.entries[&11].activation_count, 1);
        assert_eq!(fs::read(&recovered_backup).unwrap(), valid);
        assert!(
            validate_history_file(
                serde_json::from_slice(&fs::read(&recovered_primary).unwrap()).unwrap()
            )
            .is_some()
        );

        let generation_state = TempDir::new().unwrap();
        let generation_primary = generation_state.path().join("history.json");
        let generation_backup = generation_state.path().join("history.json.bak");
        let generation_recovery = generation_state.path().join("history.json.recovery");
        let newest = serde_json::to_vec_pretty(&history_with(11, 2)).unwrap();
        fs::write(&generation_primary, b"{broken-primary").unwrap();
        fs::write(&generation_backup, &valid).unwrap();
        fs::write(&generation_recovery, &newest).unwrap();
        let recovered = HistoryStore::open(generation_state.path()).unwrap();
        assert_eq!(recovered.state.entries[&11].activation_count, 2);
        assert_eq!(fs::read(&generation_backup).unwrap(), valid);

        let corrupt_state = TempDir::new().unwrap();
        let corrupt_primary = corrupt_state.path().join("history.json");
        let corrupt_backup = corrupt_state.path().join("history.json.bak");
        let corrupt_temp = corrupt_state.path().join("history.json.tmp");
        fs::write(&corrupt_primary, b"primary-corrupt").unwrap();
        fs::write(&corrupt_backup, b"backup-corrupt").unwrap();
        fs::write(&corrupt_temp, b"temp-corrupt").unwrap();
        assert!(HistoryStore::open(corrupt_state.path()).is_err());
        assert_eq!(fs::read(&corrupt_primary).unwrap(), b"primary-corrupt");
        assert_eq!(fs::read(&corrupt_backup).unwrap(), b"backup-corrupt");
        assert_eq!(fs::read(&corrupt_temp).unwrap(), b"temp-corrupt");
        assert!(!corrupt_state.path().join("history.json.recovery").exists());
    }
}
